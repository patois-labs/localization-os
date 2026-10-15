"""projects: project rename + delete (spec-rename-delete)

Documented in: docs/architecture/content-and-projects.md
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import get_session
from app.kernel import (
    access,
    audit,
    connected_projects,
    content,
    languages,
    lifecycle,
    pair_providers,
    security,
)
from app.kernel import (
    segmentation_profiles as segprof,
)
from app.kernel import (
    templates as template_service,
)
from app.models import (
    Project,
    ProjectStatus,
)
from app.web.deps import (
    _actor_email,
    _admin_block,
    _ai_block,
    _error_page,
    _ip,
    _missing_block,
    _missing_or,
    _my_customer_id,
    _my_org_id,
    _visibility_block,
    templates,
)
from app.web.form_errors import redirect_with_error

router = APIRouter()


@router.post("/projects/author.json")
def author_project_json(
    request: Request,
    description: str = Form(...),
    provider: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_session),
):
    """Promptable setup (SPEC-promptable-setup A2): the "Describe it" section of
    the new-project dialog POSTs here via fetch(); the returned VALIDATED draft is
    dropped into the existing form fields client-side, and the human then submits
    the untouched POST /projects. STATELESS -- nothing is persisted here.

    RBAC is _ai_block (admins + linguists, same predicate as the workflow
    author.json / add-step assistant); errors are the SAME clean messages the
    service produced, returned as JSON with the service's status hint (403 gate /
    400 bad ref / 502 provider-or-parse), never a 500/traceback."""
    _me, blocked = _ai_block(request)
    if blocked:
        return JSONResponse(
            {"ok": False, "error": "AI setup is limited to admins and linguists."}, status_code=403
        )
    from app.kernel import setup_author

    try:
        draft = setup_author.author_project_setup(
            session,
            description=description,
            provider=(provider or None),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            bound_customer_id=_my_customer_id(request),
            model=(model or None),
        )
    except setup_author.SetupAuthorError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    return JSONResponse(
        {
            "ok": True,
            "draft": draft["fields"],
            "warnings": draft["warnings"],
            "model_notes": draft["model_notes"],
        }
    )


@router.get("/projects/templates/{template_id}/prefill.json")
def project_template_prefill_json(
    template_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """New-project dialog "start from a template" (template-prefill feature): a
    read-only JSON seam returning a draft-shaped payload -- the SAME {draft,
    warnings} shape /projects/author.json's promptable-setup draft uses -- so the
    dialog's existing applyDraft()/renderWarnings() JS fills the form without a
    parallel fill path. STATELESS; nothing is persisted here (mirrors author.json).

    Visibility is delegated to templates.prefill_draft, which checks the SAME
    org + R2-3 customer-bound scope apply_template/delete_template use -- byte-
    identical to the set of templates projects_index already lists for this
    caller. A foreign/cross-org/foreign-customer id is refused with a plain 404
    (never enumerable), same posture as _visibility_block elsewhere.

    No extra RBAC beyond the ordinary session: template APPLICATION itself
    (POST /projects/from-template) is only can_write-gated, matching the
    projects-page "Create from template" card, which is not can_tune_ai-gated
    either -- this read-only prefill follows the same bar."""
    from app.kernel import templates as _tpl_service

    try:
        result = _tpl_service.prefill_draft(
            session,
            template_id,
            actor=security.session_user(request),
            org_id=_my_org_id(request),
            bound_customer_id=_my_customer_id(request),
        )
    except _tpl_service.TemplateError:
        return JSONResponse({"ok": False, "error": "Template not found."}, status_code=404)
    return JSONResponse({"ok": True, "draft": result["draft"], "warnings": result["warnings"]})


@router.post("/projects/{project_id}/languages")
def project_apply_language_set(
    project_id: int,
    request: Request,
    language_set_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Apply a saved Language Set to a plain (non-connector) project: add a
    TargetLocale row for every locale in the set that the project doesn't
    already have. Additive merge only — never touches existing TargetLocale
    rows (or their segments/translations), and re-applying is idempotent."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Applying a language set requires a write role.")
    if (
        security.auth_required()
        and not security.can_manage(me)
        and connected_projects.is_bound(session, project_id)
    ):
        return _error_page(request, 403, connected_projects.LOCALE_ADD_MANAGER_ONLY)
    ls = languages.get(language_set_id)
    if not ls:
        return RedirectResponse(f"/projects/{project_id}?err=langset", status_code=303)
    added = content.apply_language_set_to_project(
        session, project_id, ls, actor=_actor_email(request)
    )
    audit.log(
        actor=_actor_email(request),
        action="project.languages_apply",
        target=f"project {project_id} set {ls.id} ({ls.name})",
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    return RedirectResponse(f"/projects/{project_id}?langs_added={added}", status_code=303)


@router.post("/projects/{project_id}/add-locale")
def project_add_locale(
    project_id: int,
    request: Request,
    locale: list[str] = Form([]),
    session: Session = Depends(get_session),
):
    """Add target locale(s) to a living project from the searchable picker (P2,
    item 4). Additive + idempotent; validates each code against the BCP-47 catalog
    (a disabled-but-valid code is accepted). Seeds segments for each new locale on
    already-parsed documents and reopens a COMPLETED/CLOSED project (P1 hook).
    The picker posts one `locale` checkbox per pick; a single value also works."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Adding a locale requires a write role.")
    if (
        security.auth_required()
        and not security.can_manage(me)
        and connected_projects.is_bound(session, project_id)
    ):
        return _error_page(request, 403, connected_projects.LOCALE_ADD_MANAGER_ONLY)
    from urllib.parse import quote

    codes = [x for x in locale if (x or "").strip()]
    added = 0
    try:
        for code in codes:
            # review B5: the actor rides down to the key-value materialization, so
            # the `value`/`created` entries it appends name who added the locale.
            if content.add_target_locale(session, project_id, code, actor=_actor_email(request)):
                added += 1
                audit.log(
                    actor=_actor_email(request),
                    action="project.add_locale",
                    target=f"project {project_id} {code.strip()}",
                    ip=_ip(request),
                    customer_id=project.customer_id,
                )
    except content.InvalidLocaleError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/projects/{project_id}?locale_added={added}", status_code=303)


@router.get("/projects/{project_id}/remove-locale/{locale}")
def project_remove_locale_confirm(
    project_id: int, locale: str, request: Request, session: Session = Depends(get_session)
):
    """Typed-confirmation page for removing a target locale (the inverse of add-locale).
    Shows the itemized pre-counts of what will be DELETED (segments + editor state, LQA,
    pair-provider override) and what is PRESERVED (TM, glossary, run history), or the
    last-locale blocker. Gated by project visibility AND a write role (matching the
    add-locale mutation it reverses); the destructive safety is the typed confirm."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Removing a locale requires a write role.")
    preview = content.remove_locale_preview(session, project_id, locale)
    if preview is None:
        from urllib.parse import quote

        return RedirectResponse(
            f"/projects/{project_id}?err=" + quote(f"Locale {locale} is not on this project."),
            status_code=303,
        )
    return templates.TemplateResponse(
        request,
        "project_remove_locale.html",
        {"request": request, "preview": preview, "err": request.query_params.get("err")},
    )


@router.post("/projects/{project_id}/remove-locale")
def project_remove_locale(
    project_id: int,
    request: Request,
    locale: str = Form(...),
    confirm_locale: str = Form(""),
    session: Session = Depends(get_session),
):
    """Remove a target locale (inverse of add-locale). Requires typing the EXACT locale
    code to confirm (the destructive-op precedent, proportional to project-delete's
    typed-name confirm). Fails closed on the last-locale rule (re-renders the confirm
    page with the blocker). Project-visibility + write-role gated."""
    from urllib.parse import quote

    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Removing a locale requires a write role.")
    preview = content.remove_locale_preview(session, project_id, locale)
    if preview is None:
        return RedirectResponse(
            f"/projects/{project_id}?err=" + quote(f"Locale {locale} is not on this project."),
            status_code=303,
        )
    # Typed confirmation must match the locale (compared case-insensitively on the
    # normalized code, so 'es_es' confirms 'es-ES').
    from app.kernel import languages_catalog

    # A connected project's language is only STOPPED (nothing is deleted), so the page's own
    # button is the confirmation and no code needs typing.
    if preview.get("mode") != "stop" and languages_catalog.normalize(
        confirm_locale.strip()
    ) != languages_catalog.normalize(preview["locale"]):
        return redirect_with_error(
            f"/projects/{project_id}/remove-locale/{quote(locale)}", "locale_code_mismatch"
        )
    try:
        content.remove_target_locale(
            session,
            project_id,
            locale,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except content.LocaleRemovalBlocked as exc:
        return RedirectResponse(
            f"/projects/{project_id}/remove-locale/{quote(locale)}?err=" + quote(str(exc)),
            status_code=303,
        )
    return RedirectResponse(
        f"/projects/{project_id}?locale_removed=" + quote(preview["locale"]), status_code=303
    )


# --- project lifecycle (status) ---
@router.post("/projects/{project_id}/status")
def project_set_status(
    project_id: int,
    request: Request,
    status: str = Form(...),
    session: Session = Depends(get_session),
):
    """Manual status transition (P1). Gated by project visibility AND a write role
    (translator+). Cancel / Close / Reopen buttons on the project page post here."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Changing project status requires a write role.")
    target = (status or "").strip().lower()
    if target not in ProjectStatus.ALL:
        return RedirectResponse(f"/projects/{project_id}?err=status", status_code=303)
    if lifecycle.transition_error(project.status, target):
        # #580: outside the allowed graph (same rule as /api and the CLI): the existing
        # status-error flash, nothing written.
        return RedirectResponse(f"/projects/{project_id}?err=status", status_code=303)
    lifecycle.set_status(session, project_id, target, actor=_actor_email(request), ip=_ip(request))
    return RedirectResponse(f"/projects/{project_id}?status_set={target}", status_code=303)


# --- project due date (Feature A) ---
@router.post("/projects/{project_id}/due")
def project_set_due(
    project_id: int,
    request: Request,
    due_date: str = Form(""),
    session: Session = Depends(get_session),
):
    """Set or clear a project's optional due date (Feature A). Blank = clear it.
    Gated by project visibility AND a write role (translator+), like the status
    route. A bad date redirects with a clean error (never a 500)."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Changing the due date requires a write role.")
    from urllib.parse import quote

    try:
        content.set_project_due(
            session, project_id, due_date, actor=_actor_email(request), ip=_ip(request)
        )
    except content.DueDateError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


# --- project content type (SPEC-content-types §8.5) ---
@router.post("/projects/{project_id}/content-type")
def project_set_content_type(
    project_id: int,
    request: Request,
    content_type_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Set or clear a project's content type post-creation (SPEC-content-types §8.5).
    Blank = clear (back to the shared "General" bucket). Gated by project visibility
    AND a write role (translator+), like the status/due routes — assigning a content
    type is a project SETTING (the ContentType CRUD itself is can_tune_ai, elsewhere).
    A bad/foreign id redirects with a clean error (never a 500). Existing TM rows keep
    the content type they were WRITTEN under — the settings UI warns of this."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Changing the content type requires a write role.")
    from urllib.parse import quote

    try:
        content.set_project_content_type(
            session, project_id, content_type_id, actor=_actor_email(request), ip=_ip(request)
        )
    except content.InvalidContentTypeError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


# --- assign a customer to a customer-LESS project (NULL -> set only) ---
@router.post("/projects/{project_id}/set-customer")
def project_set_customer(
    project_id: int,
    request: Request,
    customer_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Assign a customer to a project that has NONE (2026-07-14 — the legacy
    customer-less remediation banner). Assignment is from-NULL-only: the service
    refuses changing an already-set customer (general reassignment has TM/glossary
    provenance implications). Gated by project visibility AND a write role, like the
    other project-setting routes. A missing/archived/foreign customer redirects with a
    clean error, never a 500."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Assigning a customer requires a write role.")
    from urllib.parse import quote

    try:
        content.assign_project_customer(
            session,
            project_id,
            customer_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except (content.ProjectCustomerAssignError, content.ArchivedCustomerError) as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


# --- project rename + delete (SPEC-rename-delete) ---
@router.post("/projects/{project_id}/rename")
def project_rename(
    project_id: int,
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    """Rename a project (SPEC-rename-delete). Gated by project visibility AND a write
    role (translator+), matching the neighbouring project-setting routes. A blank name
    redirects with a clean error, never a 500."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Renaming a project requires a write role.")
    from urllib.parse import quote

    try:
        content.rename_project(
            session,
            project_id,
            name,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except ValueError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


@router.get("/projects/{project_id}/delete")
def project_delete_confirm(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    """The typed-confirmation page for a project delete (SPEC-rename-delete): shows the
    itemized pre-counts of what will be destroyed (or the blockers that fail it closed).
    Admin-only (every new delete is strictly is_admin), and project-visibility gated."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    preview = content.delete_project_preview(session, project_id)
    return templates.TemplateResponse(
        request,
        "project_delete.html",
        {"request": request, "preview": preview, "err": request.query_params.get("err")},
    )


@router.post("/projects/{project_id}/delete")
def project_delete(
    project_id: int,
    request: Request,
    confirm_name: str = Form(""),
    session: Session = Depends(get_session),
    privacy_erasure: bool = Form(False),
):
    """Full-cascade project delete (SPEC-rename-delete). Admin-only; requires typing the
    EXACT project name (typed confirmation). Fails closed on Connection/RoutingRule
    referrers (re-renders the confirm page with the blocker message). Project-visibility
    gated so a foreign-org/customer id is not-found before anything runs. Privacy erasure
    (#429) also deletes Lab snapshots copied from the project's documents."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    if (confirm_name or "").strip() != project.name:
        return redirect_with_error(f"/projects/{project_id}/delete", "delete_name_mismatch")
    try:
        counts = content.delete_project(
            session,
            project_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
            privacy_erasure=privacy_erasure is True,
        )
    except content.ProjectDeleteBlocked as exc:
        return RedirectResponse(
            f"/projects/{project_id}/delete?err=" + quote(str(exc)), status_code=303
        )
    pending = (counts or {}).get("file_pending", 0)
    suffix = f"&file_pending={pending}" if pending else ""
    preserved = (counts or {}).get("lab_datasets_preserved", 0)
    if preserved:
        suffix += f"&lab_datasets_preserved={preserved}"
    return RedirectResponse(f"/projects?deleted=1{suffix}", status_code=303)


@router.post("/projects/{project_id}/archive")
def project_archive(project_id: int, request: Request, session: Session = Depends(get_session)):
    """Archive (retire) a project (residual sweep: archive verbs). Admin-only (same
    posture as delete + the Customer archive precedent). Drops it from the default
    project list + pickers and blocks new runs; documents/segments/TM/history are
    preserved and it is reversible. Project-visibility + org gated. Idempotent-safe."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)  # R4: 404 a missing id
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    try:
        result = content.archive_project(
            session,
            project_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except content.ProjectArchiveError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    if result is None:  # missing / cross-org -> 404, never a false success (L3)
        return _missing_block(request, session)
    return RedirectResponse("/projects?archived_done=1", status_code=303)


@router.post("/projects/{project_id}/unarchive")
def project_unarchive(project_id: int, request: Request, session: Session = Depends(get_session)):
    """Unarchive (reactivate) a project (residual sweep). Admin-only. Restores it to the
    default list + pickers and makes it runnable again. Idempotent-safe."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)  # R4: 404 a missing id
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    try:
        result = content.unarchive_project(
            session,
            project_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except content.ProjectArchiveError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    if result is None:  # missing / cross-org -> 404, never a false success (L3)
        return _missing_block(request, session)
    return RedirectResponse(f"/projects/{project_id}?unarchived=1", status_code=303)


@router.post("/projects/from-template")
def project_from_template(
    request: Request,
    template_id: str = Form(...),
    name: str = Form(...),
    customer_id: str = Form(""),
    connected: str = Form(""),
    session: Session = Depends(get_session),
):
    """Instantiate a project from a ProjectTemplate (P7 — the owner's "choose it
    next time and it auto-fills" ask, done server-side in one step). Dangling
    template refs degrade to warnings surfaced on the new project page.

    2026-07-14: a project requires a customer. `customer_id` (blank = use the
    template's own customer, for a customer-scoped template) is passed to the service
    with require_customer=True — so a GLOBAL template applied with no customer chosen
    fails cleanly (TemplateError) instead of creating a customer-less project. A
    customer-scoped template ignores a matching pick and rejects a conflicting one."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Creating projects requires a write role.")
    from urllib.parse import quote

    try:
        proj, warnings = template_service.apply_template(
            session,
            int(template_id or 0),
            name,
            customer_id=(customer_id or None),
            created_by=_actor_email(request),
            creator=me,
            actor=me,
            # R2-3 (HIGH 2): a customer-bound caller may only apply a global or
            # own-customer template; the service refuses (TemplateError) a
            # foreign customer-scoped one rather than silently planting the new
            # project in that customer's tenant. No-op for internal staff.
            bound_customer_id=access.current_customer_id(me),
            require_customer=True,
            # Connected projects: the "Connected project" box creates an EMPTY connected project
            # from the template. The template never binds or schedules a source by itself.
            intake=("connected" if connected in ("1", "on", "true") else "upload"),
        )
    except pair_providers.MockEngineAdminOnlyError as exc:
        # #577: a template that pins the offline Mock engine needs an administrator.
        return _error_page(request, 403, str(exc))
    except (template_service.TemplateError, ValueError) as exc:
        return RedirectResponse("/projects?err=" + quote(str(exc)) + "#new", status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="project.from_template",
        target=f"template {template_id} -> project {proj.id}",
        ip=_ip(request),
        customer_id=getattr(proj, "customer_id", None),
    )
    suffix = ("?tpl_warn=" + quote("; ".join(warnings))) if warnings else ""
    anchor = "#source" if getattr(proj, "intake", "upload") == "connected" else ""
    return RedirectResponse(f"/projects/{proj.id}{suffix}{anchor}", status_code=303)


@router.post("/projects/templates/from-draft")
def project_template_from_draft(
    request: Request,
    name: str = Form(...),
    source_locale: str = Form("en-US"),
    target_locales: list[str] = Form([]),
    default_provider: str = Form("claude"),
    customer_id: str = Form(""),
    scope: str = Form("global"),
    description: str = Form(""),
    default_workflow_id: str = Form(""),
    segmentation_profile_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save a promptable-setup DRAFT as a ProjectTemplate without a project ever
    existing (SPEC-promptable-setup A2 -- "template creation by prompt"). Gated
    like the other project writes (can_write) plus the R2-3 customer ring: a
    customer-bound caller's template is confined to their OWN customer (never a
    foreign or global one). The service re-validates every field and skip+warns
    dangling refs (apply_template's tolerance)."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Saving templates requires a write role.")
    from urllib.parse import quote

    # R2-3: a bound caller may only create a template scoped to their own customer
    # (the draft's customer was already confined to the visible ring, but a forged
    # POST must not route around it). No-op for internal staff (bound is None).
    bound = _my_customer_id(request)
    cust = customer_id
    scope_val = scope
    if bound is not None:
        cust = str(bound)
        scope_val = "customer"
    targets = ", ".join(x for x in target_locales if (x or "").strip())
    try:
        tpl, warnings = template_service.create_template_from_fields(
            session,
            name,
            source_locale=source_locale,
            target_locales=targets,
            default_provider=default_provider,
            customer_id=(cust or None),
            scope=scope_val,
            description=description,
            default_workflow_id=(default_workflow_id or None),
            segmentation_profile_id=(segmentation_profile_id or None),
            actor=_actor_email(request),
            org_id=_my_org_id(request),
            # HIGH-1/MED-1: the service re-resolves customer_id against the caller's
            # visible ring (org + this bound customer); a forged foreign/nonexistent
            # id drops to a global template with a warning, never a 500 or a leak.
            bound_customer_id=bound,
        )
    except template_service.TemplateError as exc:
        return RedirectResponse("/projects?err=" + quote(str(exc)) + "#new", status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="template.create",
        target=f"template {tpl.id} ({tpl.name}) from draft",
        ip=_ip(request),
        customer_id=tpl.customer_id,
    )
    suffix = ("?tpl_warn=" + quote("; ".join(warnings))) if warnings else "?saved=1"
    return RedirectResponse("/projects" + suffix + "#templates", status_code=303)


@router.post("/projects/{project_id}/save-template")
def project_save_template(
    project_id: int,
    request: Request,
    name: str = Form(...),
    scope: str = Form("global"),
    description: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save this project's setup as a template (P7), global or scoped to the
    project's customer. Gated like other project writes."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Saving templates requires a write role.")
    from urllib.parse import quote

    try:
        tpl = template_service.save_as_template(
            session,
            project_id,
            name,
            scope=scope,
            description=description,
            actor=_actor_email(request),
            org_id=_my_org_id(request),
            # #488 item 5: a customer-bound caller publishes only into its own customer
            # (never a global template every customer sees). None for internal staff.
            bound_customer_id=_my_customer_id(request),
        )
    except template_service.TemplateError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="template.create",
        target=f"template {tpl.id} ({tpl.name}) from project {project_id}",
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


@router.post("/templates/{template_id}/delete")
def template_delete(template_id: int, request: Request):
    """Delete a template (a snapshot — nothing references it). Manage-role gated
    (#1: a plain translator must not delete any global/other-customer template);
    the service also org-scopes the lookup so a cross-org id is not-found."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(request, 403, "Deleting templates requires a manager role.")
    # R2-3 (permanent route-sweep finding, defense in depth): the service only
    # org-scopes the lookup — a CUSTOMER-scoped template isn't ring-gated against
    # a caller bound to a DIFFERENT customer (global templates, customer_id None,
    # stay reachable everywhere). A bound account can never hold manage role
    # today, so this never fires via a real account, but the by-id route sweep in
    # tests/verify_rename_delete_projects.py holds every by-id route to the rule
    # regardless of current role reachability, so this stays defense in depth.
    tpl = template_service.get_template(template_id, org_id=_my_org_id(request))
    my_cid = _my_customer_id(request)
    if (
        tpl is not None
        and my_cid is not None
        and tpl.customer_id is not None
        and tpl.customer_id != my_cid
    ):
        return _error_page(request, 404, "Not found.")
    try:
        deleted = template_service.delete_template(template_id, actor=me)
    except template_service.TemplateError as exc:
        return _error_page(request, 403, str(exc))
    if deleted:
        audit.log(
            actor=_actor_email(request),
            action="template.delete",
            target=f"template {template_id}",
            ip=_ip(request),
        )
    return RedirectResponse("/projects#templates", status_code=303)


@router.post("/projects/{project_id}/segmentation")
def project_set_segmentation(
    project_id: int,
    request: Request,
    segmentation_profile_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Set or clear a project's segmentation-profile override (P6 — the owner's
    "untick default segmentation" pick). Blank = back to the inherit chain. Gated
    like the status route: project visibility + a write role. Only affects FUTURE
    ingests — existing documents keep their stamped seg_engine (TM safety)."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Changing segmentation requires a write role.")
    try:
        segprof.assign_to_project(session, project_id, segmentation_profile_id)
    except segprof.SegProfileError as exc:
        from urllib.parse import quote

        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="project.segmentation",
        target=f"project {project_id} -> profile "
        f"{(segmentation_profile_id or '').strip() or '(inherit)'}",
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


@router.post("/projects/{project_id}/pair-providers")
def project_set_pair_providers(
    project_id: int,
    request: Request,
    provider_mode: str = Form("uniform"),
    pair_target: list[str] = Form([]),
    pair_provider: list[str] = Form([]),
    session: Session = Depends(get_session),
):
    """Set a project's per-pair provider mode + mapping (Phase 2 edit path).

    Same shared service the create flow uses (pair_providers.apply_mode) — no
    route-local logic. Gated like the segmentation route: project visibility + a
    write role. UNIFORM clears any stored overrides; PER_PAIR validates + replaces
    the pair rows. Only affects FUTURE translate runs (never rewrites done work)."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Changing providers requires a write role.")
    src = project.source_locale
    pair_strs = [
        f"{src}:{tgt}={prov}"
        for tgt, prov in zip(pair_target, pair_provider, strict=False)
        if (tgt or "").strip() and (prov or "").strip()
    ]
    try:
        mapping = pair_providers.parse_pair_mapping(pair_strs)
        pair_providers.apply_mode(
            session, project, provider_mode, mapping, org_id=_my_org_id(request), user=me
        )
    except pair_providers.MockEngineAdminOnlyError as exc:
        # #577: pinning a pair to the offline Mock engine is admin-only.
        return _error_page(request, 403, str(exc))
    except pair_providers.PairProviderError as exc:
        from urllib.parse import quote

        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="project.pair_providers",
        target=f"project {project_id} -> mode "
        f"{pair_providers.normalize_mode(provider_mode)} "
        f"({len(mapping)} pair override(s))",
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


# AI opt-out (DPAI-08). Kept LAST in this module: app/web/projects.py includes this
# router in index slices, and the final slice [19:23] carries it (and #690 after it).
@router.post("/projects/{project_id}/ai-opt-out")
def project_ai_opt_out(
    project_id: int,
    request: Request,
    ai_opt_out: str = Form(""),
    session: Session = Depends(get_session),
):
    """AI opt-out: switch AI off (ai_opt_out=1) or back on (0) for this
    project. Manager or admin. Project-visibility + org gated. Audited by the service."""
    from urllib.parse import quote

    from app.kernel import ai_optout

    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(
            request, 403, "Only an admin or manager can switch AI on or off for a project."
        )
    value = ai_optout.parse_switch(ai_opt_out)
    if value is None:
        return RedirectResponse(
            f"/projects/{project_id}?err=" + quote("Choose whether AI is on or off."),
            status_code=303,
        )
    result = ai_optout.set_project(
        session,
        project_id,
        value,
        actor=_actor_email(request),
        ip=_ip(request),
        org_id=_my_org_id(request),
    )
    if result is None:
        return _missing_block(request, session)
    return RedirectResponse(
        f"/projects/{project_id}?ai={'off' if value else 'on'}#ai-use", status_code=303
    )


# Translate comments (#690). After the AI opt-out route, inside the same final slice
# app/web/projects.py includes ([19:23]).
@router.post("/projects/{project_id}/translate-comments")
def project_translate_comments(
    project_id: int,
    request: Request,
    translate_comments: str = Form(""),
    session: Session = Depends(get_session),
):
    """Translate comments (#690): offer Office comments and annotations for translation
    (translate_comments=1) or leave them in the source language (0). Manager or admin.
    Project-visibility + org gated. Audited by the service."""
    from urllib.parse import quote

    from app.kernel import comment_option

    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(
            request, 403, "Only an admin or manager can change comment translation for a project."
        )
    value = comment_option.parse_switch(translate_comments)
    if value is None:
        return RedirectResponse(
            f"/projects/{project_id}?err=" + quote("Choose whether comments are translated."),
            status_code=303,
        )
    comment_option.set_for(project, value, actor=_actor_email(request), ip=_ip(request))
    return RedirectResponse(
        f"/projects/{project_id}?comments={'on' if value else 'off'}#comments-option",
        status_code=303,
    )
