"""/api routes: projects 1

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    access,
    ai_optout,
    audit,
    comment_option,
    connected_projects,
    content,
    content_types,
    glossary,
    languages,
    lifecycle,
    pair_providers,
    security,
    segmentation_profiles,
    tm,
)
from app.kernel import custom_fields as cf_service
from app.kernel import ids as ids
from app.kernel import keys as keys_svc
from app.models import (
    Customer,
    Document,
    Project,
    TargetLocale,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _creator,
    _cust_of,
    _customer_in_org,
    _gate,
    _int_id,
    _json,
    _json_object,
    _langset_in_org,
    _list_truncate,
    _list_window,
    _manage_block,
    _org_list,
    _project_in_org,
    _scoped_customers,
    _write_block,
)

router = APIRouter()


def _tm_source_visible(s: Session, source_pid, org_id, ident=None):
    """The SOURCE project a `project tm-sources add` may reference, or None (404).

    Deliberately DIFFERENT from _project_in_org (R1a, the B1/H1/H2 lesson applied to
    the asymmetric TM-attach rule): a TM source may be the caller's own customer OR a
    customer-LESS house project (access.cross_project_scope_allows lets a house project
    feed a customer's project, never the reverse). So a customer-BOUND caller must be
    able to reach a house source _project_in_org would wrongly 404. This funnel is what
    keeps the source id non-enumerable while honouring that asymmetry:

      * missing / another ORG                       -> None (404, non-enumerable);
      * a BOUND caller + a source under ANOTHER customer -> None (404: a bound token
        must never learn another customer's project exists — the within-org oracle);
      * a house (customer_id NULL) or own-customer source -> returned; the service's
        cross_project_scope_allows then makes the final same-customer-or-house call
        (and 400s a staff caller's cross-customer source, which staff may see anyway).
    """
    src = s.get(Project, source_pid) if source_pid is not None else None
    if src is None:
        return None
    if org_id is not None and getattr(src, "org_id", None) != org_id:
        return None
    bound = _cust_of(ident)
    if bound is not None and getattr(src, "customer_id", None) not in (None, bound):
        return None
    if bound is None and not access.can_view_project(s, (ident or {}).get("user"), src):
        return None
    return src


def _scoped_projects(ident, org_id):
    """Apply current web visibility and the identity's outer rings before pagination."""
    stmt = _org_list(select(Project), Project, org_id)
    cust = _cust_of(ident)
    if cust is not None:
        stmt = stmt.where(Project.customer_id == cust)
    with Session(engine) as session:
        allowed = access.visible_project_ids(session, (ident or {}).get("user"))
    if allowed is not None:
        stmt = stmt.where(Project.id.in_(allowed))
    return stmt


def _count(s: Session, stmt) -> int:
    """Row count of a `select(Project)` statement (no window)."""
    from sqlalchemy import func

    return int(s.exec(select(func.count()).select_from(stmt.order_by(None).subquery())).one())


# ============================================================================
# Projects
# ============================================================================
@router.get("/api/projects")
def api_projects_list(
    request: Request,
    limit: str | None = Query(None, description="max rows; default and hard cap 1000"),
    offset: str | None = Query(None, description="rows to skip (paging)"),
    status: str = Query(
        "",
        description="ProjectStatus filter; '' / 'default' hides "
        "done/closed/cancelled, 'all' shows all",
    ),
    archived: str = Query("", description="archive filter: '' / active, all, only"),
    intake: str = Query(
        "",
        description="intake filter: '' (all), 'connected' (content comes from a connected "
        "system) or 'upload'",
    ),
):
    """List the caller's scoped projects. `?archived=all|only` widens/narrows the
    archive filter (default: active only). `?status=` filters by ProjectStatus in SQL
    (blank/`default` hides done/closed/cancelled, `all` shows everything, a specific
    status filters to it; an unknown status is a 400). BOUNDED (AU-7): optional `limit`
    (default and hard cap 1000) + `offset` page the result in SQL; the response carries
    `truncated` (more rows exist past this page), `limit` and `offset`. `hidden` counts
    the in-scope projects the DEFAULT archive/status filters left out (`archived`,
    `finished`), so a short list is never mistaken for a narrower scope than
    GET /api/projects/{id} allows (#607)."""
    from app.models import ProjectStatus

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    # AU-7: bound the result (was every scoped project, unbounded). Optional
    # limit/offset pushed into SQL; a generous 1000 cap + `truncated` flag otherwise.
    limit, offset, err = _list_window(limit, offset)
    if err:
        return err
    # M4: `status` is a SERVER-SIDE filter now, so the --remote CLI arm no longer
    # windows-then-filters-client-side (which diverged from the local arm past the cap).
    status = status.strip().lower()
    if status not in ("", "default", "all") and status not in ProjectStatus.ALL:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown status {!r} (choose one of: {}, all, default).".format(
                    status, ", ".join(ProjectStatus.ALL)
                ),
            },
            status_code=400,
        )
    intake = intake.strip().lower()
    if intake not in ("", "connected", "upload"):
        return JSONResponse(
            {"ok": False, "error": f"unknown intake {intake!r} (choose connected or upload)."},
            status_code=400,
        )
    with Session(engine) as s:
        stmt = _scoped_projects(ident, org_id)
        base_stmt = stmt
        if intake:
            stmt = stmt.where(Project.intake == intake)
        # Archive (residual sweep): a list feeding pickers hides ARCHIVED projects by
        # default (parity with the web /projects list). `?archived=all` shows both,
        # `?archived=only` just the archived (the retire/restore view).
        archived = archived.strip().lower()
        if archived == "only":
            stmt = stmt.where(Project.archived_at.is_not(None))
        elif archived != "all":
            stmt = content.project_active_only(stmt)  # shipped seam (M4)
        if status in ("", "default"):
            stmt = stmt.where(Project.status.notin_(ProjectStatus.HIDDEN_BY_DEFAULT))
        elif status != "all":
            stmt = stmt.where(Project.status == status)
        # Custom project fields: `?field.<key>=<expr>` filters and `?sort=field.<key>`
        # (&dir=desc). Unknown keys and bad expressions are a clean 400, never ignored.
        cf_defs = cf_service.list_defs(s, org_id, _cust_of(ident))
        cf_by_key = cf_service.group_by_key(cf_defs)
        for qname, qraw in request.query_params.multi_items():
            if not qname.startswith("field.") or not (qraw or "").strip():
                continue
            fdef = cf_by_key.get(qname[6:].lower())
            if fdef is None:
                return JSONResponse(
                    {"ok": False, "error": f"unknown custom field {qname[6:]!r}."}, status_code=400
                )
            try:
                stmt = stmt.where(Project.id.in_(cf_service.project_ids_matching(s, fdef, qraw)))
            except cf_service.FieldError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        sort_raw = (request.query_params.get("sort") or "").strip()
        sort_def = None
        if sort_raw:
            sort_def = (
                cf_by_key.get(sort_raw[6:].lower()) if sort_raw.startswith("field.") else None
            )
            if sort_def is None:
                return JSONResponse(
                    {"ok": False, "error": f"cannot sort by {sort_raw!r} (use field.<key>)."},
                    status_code=400,
                )
        # L8: (created_at, id) so the LIMIT/OFFSET window is deterministic on Postgres
        # (a bare created_at can tie; CI smokes Postgres, whose scan order is unpinned).
        if sort_def is not None:
            stmt = cf_service.apply_sort(
                stmt.order_by(None),
                sort_def,
                descending=(request.query_params.get("dir") or "").lower() == "desc",
            )
        else:
            stmt = stmt.order_by(Project.created_at.desc(), Project.id.desc())
        rows = s.exec(stmt.offset(offset).limit(limit + 1)).all()
        projects, truncated = _list_truncate(list(rows), limit)
        # #607: the default archive/status filters are the ONLY reason this list can be
        # shorter than what GET /api/projects/{id} allows the same key (scope itself is
        # one shared authority). Say how many in-scope projects the defaults hid.
        hidden = {"archived": 0, "finished": 0}
        if archived not in ("all", "only"):
            arch_q = base_stmt.where(Project.archived_at.is_not(None))
            if status not in ("", "default", "all"):
                arch_q = arch_q.where(Project.status == status)  # only what THIS query hid
            hidden["archived"] = _count(s, arch_q)
        if status in ("", "default"):
            fin = base_stmt.where(Project.status.in_(ProjectStatus.HIDDEN_BY_DEFAULT))
            if archived not in ("all", "only"):
                fin = content.project_active_only(fin)
            hidden["finished"] = _count(s, fin)
        cust_by_id = {c.id: c.name for c in s.exec(_scoped_customers(ident, org_id)).all()}
        cf_vals = cf_service.payload_for_projects(s, projects, org_id, _cust_of(ident))
        src_labels = connected_projects.source_labels(s, [p.id for p in projects])
        payload = [
            {
                "id": p.id,
                "name": p.name,
                "source_locale": p.source_locale,
                "archived": p.archived_at is not None,
                "status": p.status,
                # Connected projects: where the content comes from, and for a connected
                # project its source (kind, label; "Not connected yet" until bound).
                "intake": p.intake,
                **({"source": src_labels[p.id]} if p.id in src_labels else {}),
                "customer": cust_by_id.get(p.customer_id, "") if p.customer_id else "",
                # Custom project fields {key: value}; empty when none is set.
                "custom_fields": cf_vals.get(p.id, {}),
            }
            for p in projects
        ]
    return JSONResponse(
        {
            "ok": True,
            "projects": payload,
            "truncated": truncated,
            "limit": limit,
            "offset": offset,
            "hidden": hidden,
        }
    )


@router.post("/api/projects")
async def api_projects_create(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects create is a write action
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    # Pass the RAW name so the service can REFUSE a non-string (a list -> its repr was
    # BUG-A13); a blank/whitespace STRING keeps its friendly "blank" 400 here.
    raw_name = body.get("name")
    if raw_name is None or (isinstance(raw_name, str) and not raw_name.strip()):
        return JSONResponse({"ok": False, "error": "project name is blank."}, status_code=400)
    # Issue #55 (remote arm): an OMITTED provider inherits the install's CONFIGURED
    # default_provider (the server applies stored Settings at startup, so
    # config.DEFAULT_PROVIDER is the effective default), matching the web new-project
    # dialog + the in-process CLI. `loc-tms --remote project create` forwards
    # provider=None, so the old hardcoded "claude" literal stamped claude on the
    # PROD-FACING remote path regardless of the stored default. An explicit provider is
    # respected; out-of-box "claude" resolves to "claude" (byte-identical, and
    # project_create's ("" | "claude") customer-default inheritance still fires).
    from app import config

    _provider = str(body.get("provider") or config.DEFAULT_PROVIDER)
    cf_plan: list = []
    with Session(engine) as s:
        # #580: a named customer must exist in the caller's own org BEFORE anything is
        # written. An unknown id and another org's id answer byte-identically (404, the
        # shared not-found body), so the response is not an existence oracle. A customer-
        # BOUND caller is skipped here on purpose: the service's own R2-3 guard answers it
        # (a foreign id is 403, its own customer is force-stamped).
        _named = _int_id(body.get("customer_id"))
        if (
            _named is not None
            and _cust_of(ident) is None
            and _customer_in_org(s, _named, org_id, ident) is None
        ):
            return JSONResponse(_NOT_FOUND, status_code=404)
        # Custom project fields: validate BEFORE anything is written (a bad value or a
        # missing required field never leaves a half-made project), but AFTER the customer
        # scope check above (scope first, validate second). The customer scope is the
        # caller's own when bound, else the body's.
        if body.get("custom_fields") is not None or cf_service.list_defs(
            s, org_id, _cust_of(ident)
        ):
            _cf_cust = _cust_of(ident)
            if _cf_cust is None:
                _cf_cust = _int_id(body.get("customer_id"))
            try:
                cf_plan = cf_service.validate_for_create(
                    s, org_id, _cf_cust, body.get("custom_fields")
                )
            except cf_service.FieldError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        try:
            project = content.project_create(
                s,
                raw_name,
                source_locale=str(body.get("source_locale") or "en-US"),
                # Pass targets THROUGH as given (a JSON list is the natural /api shape;
                # a comma/space string is the form/CLI shape). project_create accepts
                # either — str()-coercing a list here turned ["fr-FR"] into the repr
                # "['fr-FR']" and 400'd a valid locale (fr-FR aside, 2026-08-08 audit).
                target_locales=(
                    body.get("targets")
                    if body.get("targets") is not None
                    else body.get("target_locales")
                )
                or "",
                customer_id=body.get("customer_id"),
                default_provider=_provider,
                created_by=ident["actor"],
                creator=_creator(ident),
                # Phase 2: per-pair provider mode + mapping. provider_mode is
                # 'uniform' (default) or 'per_pair'; pair_providers is a list of
                # "SRC:TGT=PROVIDER" strings OR a {"SRC:TGT": provider} dict — the
                # shared service parses either. A bad mapping is a clean 400.
                provider_mode=body.get("provider_mode"),
                pair_providers=body.get("pair_providers"),
                org_id=org_id,
                # Feature A: optional due date (blank/absent = none). A bad date is a
                # clean 400 (DueDateError), never a 500.
                due_date=body.get("due_date"),
                # SPEC-content-types: optional content type (blank/absent = General /
                # customer default). A bad/foreign id is a clean 400
                # (InvalidContentTypeError), never a 500.
                content_type_id=body.get("content_type_id"),
                # SPEC-key-based-projects 2.1: absent = "document", so every pre-Batch-4
                # caller is byte-identical. The service validates the value and re-checks
                # the module kill switch, so a forged body cannot mint a key project
                # while the feature is off.
                project_type=body.get("project_type"),
                # Connected projects: "connected" or absent (= upload). A bad value, or a
                # key_based shape, is a clean 400 (connected_projects.IntakeError).
                intake=body.get("intake"),
                # R2-3 (Finding E): a customer-bound lsu_ key inherits the exact same
                # confinement as its owner's web session — it can only create into
                # its own customer (foreign customer_id refused, blank force-stamped).
                bound_customer_id=_cust_of(ident),
                # 2026-07-14: /api create is a USER-FACING seam — a customer is
                # required. Missing customer_id -> MissingCustomerError -> clean 400
                # (a bound key's own customer is force-stamped first, so it satisfies
                # the gate without an explicit customer_id).
                require_customer=True,
                # #580: a same-named active project of this customer is a 409, not a twin.
                reject_duplicate=True,
            )
        except ids.InvalidNameError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except content.MockEngineAdminOnlyError as exc:
            # #577: pinning a project to the offline Mock engine is admin-only.
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except content.DuplicateNameError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "existing_id": exc.existing_id}, status_code=409
            )
        except content.UnknownCustomerError:
            return JSONResponse(_NOT_FOUND, status_code=404)
        except (
            content.InvalidLocaleError,
            content.DueDateError,
            content.PairProviderError,
            content.InvalidContentTypeError,
            content.MissingCustomerError,
            content.InvalidProjectTypeError,
            content.ArchivedCustomerError,
            connected_projects.IntakeError,
        ) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except content.ForeignCustomerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        locales = [
            tl.locale
            for tl in s.exec(
                select(TargetLocale).where(TargetLocale.project_id == project.id)
            ).all()
        ]
        cf_changes: list = []
        if cf_plan:
            try:
                cf_changes = cf_service.apply_plan(s, project, cf_plan, actor=ident["actor"])
            except cf_service.FieldError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {
            "ok": True,
            "id": project.id,
            "name": project.name,
            "source_locale": project.source_locale,
            "target_locales": locales,
            # SPEC-key-based-projects 2.1: echoed back so a caller can see which
            # type it actually got (and so `project create --type` can report it).
            "project_type": project.project_type,
            # Connected projects: echoed back, with where to connect the source.
            "intake": project.intake,
            **(
                {"setup_url": connected_projects.setup_url(project.id)}
                if connected_projects.is_connected(project)
                else {}
            ),
            # Phase 2: echo back the persisted mode + per-pair overrides.
            "provider_mode": project.provider_mode,
            "pair_providers": pair_providers.mapping_for_project(s, project.id),
            "due_date": (project.due_date.strftime("%Y-%m-%d") if project.due_date else None),
        }
        # #608: the active-pairs advisory (owner's P2 confinement), same rule as the in-process
        # CLI: WARN, never refuse. The targets created that fall outside the customer's
        # configured active pairs ride back so a remote client (the `--remote` CLI) can say so;
        # `all_targets: true` in the body silences it (the CLI's --all-targets). [] = nothing
        # outside, or the customer has no pairs configured (no confinement).
        off: list[str] = []
        if project.customer_id and not body.get("all_targets"):
            from app.kernel import customers as customer_service

            off = customer_service.targets_outside_pairs(
                customer_service.get(s, project.customer_id),
                locales,
                source_locale=project.source_locale,
            )
        out["targets_outside_pairs"] = off
        out["custom_fields"] = cf_service.project_values(s, project)
        project_customer = project.customer_id
    if cf_changes:
        audit.log(
            actor=ident["actor"],
            action="field.set",
            target=f"project {out['id']}",
            detail=cf_service.audit_detail(cf_changes),
            ip=deps._ip(request),
            customer_id=project_customer,
        )
    return JSONResponse(out)


@router.get("/api/projects/{pid}")
def api_projects_show(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        locales = content.active_target_locales(s, project.id)
        stopped_locales = content.stopped_target_locales(s, project.id)
        docs = s.exec(
            select(Document)
            .where(
                Document.project_id == project.id,
                # SPEC-key-based-projects 2.5: internal key documents stay invisible.
                Document.fmt != keys_svc.KEY_DOC_FMT,
            )
            .order_by(Document.created_at.desc())
        ).all()
        _doc_status = lifecycle.document_status_map(s, [d.id for d in docs])
        customer = s.get(Customer, project.customer_id) if project.customer_id else None
        # Issue #17/#19 (remote read-back parity): content type + segmentation
        # profile, mirroring the in-process `project show` CLI path so --remote
        # never drops fields the local path has.
        ct = content_types.get(s, project.content_type_id) if project.content_type_id else None
        seg_resolved = segmentation_profiles.resolve_profile(s, project)
        out = {
            "ok": True,
            "id": project.id,
            "name": project.name,
            "source_locale": project.source_locale,
            "archived": project.archived_at is not None,  # L11: expose on detail
            # AI opt-out (DPAI-08): the effective value plus which level set it.
            **ai_optout.status(s, project),
            # #690: the per-project "translate comments" option (Office comments).
            **comment_option.status(project),
            "customer_id": project.customer_id,
            "customer": customer.name if customer else "",
            "default_provider": project.default_provider,
            "target_locales": locales,
            # Connected projects: where the content comes from (upload | connected).
            "intake": project.intake,
            # Connected projects: languages that were stopped (kept, no longer worked on).
            "stopped_locales": stopped_locales,
            # Phase 2: the per-pair provider mode + the stored overrides.
            "provider_mode": project.provider_mode,
            "pair_providers": pair_providers.mapping_for_project(s, project.id),
            # Feature A: the due date (YYYY-MM-DD) or null.
            "due_date": (project.due_date.strftime("%Y-%m-%d") if project.due_date else None),
            "content_type_id": project.content_type_id,
            "content_type_name": ct.name if ct else None,
            "segmentation_profile_id": project.segmentation_profile_id,
            "segmentation_profile_resolved_id": seg_resolved.id if seg_resolved else None,
            "segmentation_profile_resolved_name": seg_resolved.name if seg_resolved else None,
            "documents": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "fmt": d.fmt,
                    "mode": d.mode,
                    "status": _doc_status.get(d.id, "new"),
                }
                for d in docs
            ],
            "tm_count": tm.count(s, project.id),
            "ft_tm_count": tm.ft_count(s, project.id),
            "glossary_count": glossary.count(s, project.id),
            # Custom project fields {key: value} (a bound caller only sees the global
            # fields and its own customer's, the same ring as the definitions).
            "custom_fields": cf_service.payload_for_projects(
                s, [project], org_id, _cust_of(ident)
            ).get(project.id, {}),
        }
        # CV-2: the project money budget state, MANAGER+ only under the login wall
        # (parity with the web project-page meter at projects.py:1104 + the CV-4 cost
        # gate — spend $ figures are not viewer-visible). Auth-off single-tenant INCLUDES
        # it (the same explicit fallback the web meter uses). Under-ranked keys omit the
        # field (no stub); a project with no budget row reports has_budget=False.
        if (not security.auth_required()) or security.can_manage((ident or {}).get("user")):
            from app.kernel import project_budgets

            out["budget"] = project_budgets.budget_state(s, project.id)
    return JSONResponse(out)


# --- per-project money budgets (CV-2) ---------------------------------------
@router.get("/api/projects/{pid}/budget")
def api_project_budget_get(pid: int, request: Request):
    """Read a project's monthly USD budget + this month's spend (mirrors
    `admin budget project list` for one project). admin (budget management is
    admin-only, matching department budgets); org-ringed via _project_in_org."""
    from app.kernel import project_budgets

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        state = project_budgets.budget_state(s, project.id)
    return JSONResponse({"ok": True, "project_id": pid, **state})


@router.post("/api/projects/{pid}/budget")
async def api_project_budget_set(pid: int, request: Request):
    """Create/update a project's monthly USD cap (mirrors `admin budget project set`).
    admin, org-stamped. Body: monthly_usd_limit (<= 0 = unlimited)."""
    from app.kernel import project_budgets

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        # The project-visibility RING FIRST (tenant-ring fix): a foreign / non-existent
        # by-id project is 404 BEFORE any body parsing/validation, so a customer-bound
        # caller can never probe resource reachability via a 400 on a project they cannot
        # see. Mirrors the other by-id project routes' 404-first posture.
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        body = await _json(request)
        raw = body.get("monthly_usd_limit")
        if raw is None:
            raw = body.get("limit")
        try:
            limit = float(raw)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "monthly_usd_limit must be a number."}, status_code=400
            )
        try:
            row, created = project_budgets.upsert_project_budget(
                s, project.id, limit, actor=ident.get("actor", "")
            )
        except project_budgets.BudgetLimitError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "id": row.id,
                "project_id": row.project_id,
                "monthly_usd_limit": row.monthly_usd_limit,
                "created": created,
            }
        )


@router.post("/api/projects/{pid}/budget/delete")
def api_project_budget_delete(pid: int, request: Request):
    """Delete a project's budget (mirrors `admin budget project delete`). admin,
    org-ringed (a cross-org / missing project is 404)."""
    from app.kernel import project_budgets

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        row = project_budgets.get_project_budget(s, project.id)
        if row is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        project_budgets.delete_project_budget(s, row.id, actor=ident.get("actor", ""))
    return JSONResponse({"ok": True, "project_id": pid, "deleted": True})


@router.get("/api/projects/{pid}/leverage")
def api_projects_leverage(pid: int, request: Request):
    """CV-1: the 7-band WORD-based TM-leverage breakdown for a project (repetitions/
    context/exact/fuzzy hi-mid-lo/new) — the real vendor-cost model surfaced project-wide.
    A READ endpoint (no write-role gate, like the other project reads); the caller's org/
    customer confinement rides on _project_in_org. On-demand (a dedicated route, not folded
    into GET /api/projects/{id}) so its O(n) segment scan is paid only when asked for."""
    from app.kernel import vendor_cost

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        estimate = vendor_cost.project_leverage(s, project.id)
        rows = vendor_cost.estimate_band_rows(estimate)
        # M6: a key-based project's catalogue values live in internal KEY documents, which
        # this (like every project surface) excludes — so an empty/partial result is not
        # "no content". Surface it as a note rather than silently zeroing.
        note = None
        if vendor_cost.project_has_key_docs(s, project.id):
            note = (
                "this project has key-based content; leverage covers only its "
                "non-key documents (key catalogue values are excluded)."
            )
    return JSONResponse(
        {
            "ok": True,
            "project_id": project.id,
            "total_words": estimate.get("total_words", 0),
            "total_segments": estimate.get("segment_count", 0),
            "billable_words": estimate.get("billable_words", 0),
            "covered_words": estimate.get("covered_words", 0),
            # one row per band, best-leverage-first: {band,label,words,segments,covered,billable}
            "bands": [
                {
                    "band": r["band"],
                    "label": r["label"],
                    "words": r["words"],
                    "segments": r["segments"],
                    "covered": r["covered"],
                    "billable": r["billable"],
                }
                for r in rows
            ],
            "note": note,
        }
    )


def _locale_add_block(ident):
    """Adding a language to a project with a content source needs a manager (it widens what the
    next run translates). A ready 403 naming why, or None."""
    if not security.auth_required() or security.can_manage((ident or {}).get("user")):
        return None
    return JSONResponse(
        {"ok": False, "error": connected_projects.LOCALE_ADD_MANAGER_ONLY}, status_code=403
    )


@router.post("/api/projects/{pid}/add-locale")
async def api_projects_add_locale(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/add-locale is a write action
    if blocked:
        return blocked
    body = await _json(request)
    locale = str(body.get("locale") or "").strip()
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if connected_projects.is_bound(s, project.id):
            blocked = _locale_add_block(ident)  # after the ring: 404 before 403
            if blocked:
                return blocked
        try:
            added = content.add_target_locale(s, project.id, locale, actor=ident["actor"])
        except content.InvalidLocaleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "project_id": pid, "locale": locale, "added": added})


@router.post("/api/projects/{pid}/remove-locale")
async def api_projects_remove_locale(pid: int, request: Request):
    """Remote-CLI parity for `project remove-locale`: mirrors the web
    POST /projects/{id}/remove-locale EXACTLY — same org binding, same write gate, same
    typed-confirmation (confirm_locale must match the locale), same last-locale block,
    same service (content.remove_target_locale). Deletes the locale's segments; keeps
    TM + glossary + run history. On a CONNECTED project (one with a content source) it only
    STOPS the language: nothing is deleted, no typed confirmation is needed, the response says
    `stopped: true` and the next run no longer pulls, translates or writes it."""
    from app.kernel import languages_catalog

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/remove-locale is a write action
    if blocked:
        return blocked
    body = await _json(request)
    locale = str(body.get("locale") or "").strip()
    confirm_locale = str(body.get("confirm_locale") or "").strip()
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        preview = content.remove_locale_preview(s, project.id, locale)
        if preview is None:
            return JSONResponse(
                {"ok": False, "error": f"locale {locale!r} is not on this project."},
                status_code=404,
            )
        # A connected project's language is only STOPPED (nothing deleted, it can come back),
        # so no typed confirmation is needed; every other project keeps the typed confirmation.
        if preview.get("mode") != "stop" and languages_catalog.normalize(
            confirm_locale
        ) != languages_catalog.normalize(preview["locale"]):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "confirm_locale must match the locale being "
                    "removed (typed-confirmation parity with the web surface).",
                },
                status_code=400,
            )
        try:
            deleted = content.remove_target_locale(
                s, project.id, locale, actor=ident["actor"], org_id=org_id
            )
        except content.LocaleRemovalBlocked as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        stopped = preview.get("mode") == "stop"
    return JSONResponse(
        {
            "ok": True,
            "project_id": pid,
            "locale": preview["locale"],
            "deleted": deleted,
            # True for a connected project: the language is stopped (the next run no longer
            # pulls, translates or writes it), nothing was deleted
            "stopped": stopped,
        }
    )


@router.post("/api/projects/{pid}/due")
async def api_projects_set_due(pid: int, request: Request):
    """Remote-CLI parity for `project set-due` (adversarial-review FIX 5): mirrors
    the web POST /projects/{id}/due route EXACTLY -- same org+customer binding via
    _gate/_project_in_org, same can_write role gate (the web route requires a write
    role for changing a due date), same content.set_project_due call (which
    validates + audits project.due_set/due_cleared internally, so this route does
    NOT double-audit, matching the web route's behavior). Blank/absent due_date
    clears it, same as the web form's blank-string convention."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/due is a write action
    if blocked:
        return blocked
    body = await _json(request)
    due_date = body.get("due_date")
    if due_date is None:
        due_date = ""
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            content.set_project_due(
                s, project.id, due_date, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except content.DueDateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        s.refresh(project)
        shown = project.due_date.strftime("%Y-%m-%d") if project.due_date else None
    return JSONResponse({"ok": True, "project_id": pid, "due_date": shown})


@router.post("/api/projects/{pid}/content-type")
async def api_projects_set_content_type(pid: int, request: Request):
    """Remote-CLI parity for `project set-content-type` (SPEC-content-types §8.5).
    Mirrors the web POST /projects/{id}/content-type route EXACTLY — same org+customer
    binding via _gate/_project_in_org, same can_write role gate (assigning a content
    type is a project SETTING), same content.set_project_content_type service (which
    validates attachability + audits internally, so this route does NOT double-audit).
    Blank/absent content_type_id clears it. A bad/foreign id is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/content-type is a write action
    if blocked:
        return blocked
    body = await _json(request)
    ct_id = body.get("content_type_id")
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            content.set_project_content_type(
                s, project.id, ct_id, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except content.InvalidContentTypeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        s.refresh(project)
        out = {"ok": True, "project_id": pid, "content_type_id": project.content_type_id}
    return JSONResponse(out)


@router.post("/api/projects/{pid}/set-customer")
async def api_projects_set_customer(pid: int, request: Request):
    """Remote-CLI/web parity for `project set-customer`. Mirrors the
    web POST /projects/{id}/set-customer EXACTLY — same org binding via
    _gate/_project_in_org, same can_write gate, same content.assign_project_customer
    service (from-NULL only; validates the customer + audits internally). Assigning to a
    project that already has a customer is a clean 400 (reassignment is not supported);
    a missing/archived/foreign customer is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/set-customer is a write action
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            content.assign_project_customer(
                s,
                project.id,
                body.get("customer_id"),
                actor=ident["actor"],
                ip=deps._ip(request),
                org_id=org_id,
            )
        except (content.ProjectCustomerAssignError, content.ArchivedCustomerError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        s.refresh(project)
        out = {"ok": True, "project_id": pid, "customer_id": project.customer_id}
    return JSONResponse(out)


@router.post("/api/projects/{pid}/pair-providers")
async def api_projects_set_pair_providers(pid: int, request: Request):
    """Set a project's per-pair provider mode + mapping (Phase 2). Mirrors the web
    POST /projects/{id}/pair-providers route EXACTLY — same org+customer binding
    via _gate/_project_in_org, same can_write role gate, same shared
    pair_providers.apply_mode service (no route-local logic). Body:
    {"provider_mode": "uniform"|"per_pair",
     "pair_providers": ["SRC:TGT=PROVIDER", ...] | {"SRC:TGT": provider}}.
    A bad mapping is a clean 400 (PairProviderError)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/pair-providers is a write action
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            mapping = pair_providers.parse_pair_mapping(body.get("pair_providers"))
            pair_providers.apply_mode(
                s,
                project,
                body.get("provider_mode"),
                mapping,
                org_id=org_id,
                user=(ident or {}).get("user"),
            )
        except pair_providers.MockEngineAdminOnlyError as exc:
            # #577: pinning a pair to the offline Mock engine is admin-only.
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except pair_providers.PairProviderError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {
            "ok": True,
            "project_id": pid,
            "provider_mode": project.provider_mode,
            "pair_providers": pair_providers.mapping_for_project(s, pid),
        }
    return JSONResponse(out)


@router.post("/api/projects/{pid}/apply-langset")
async def api_projects_apply_langset(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/languages apply is a write action
    if blocked:
        return blocked
    body = await _json(request)
    ls_id = body.get("langset_id")
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        ls = _langset_in_org(languages.get(ls_id), org_id)
        if ls is None:
            return JSONResponse({"ok": False, "error": "no such language set"}, status_code=404)
        if connected_projects.is_bound(s, project.id):
            blocked = _locale_add_block(ident)
            if blocked:
                return blocked
        added = content.apply_language_set_to_project(s, project.id, ls, actor=ident["actor"])
    audit.log(
        actor=ident["actor"],
        action="project.languages_apply",
        target=f"project {pid} set {ls.id}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "project_id": pid, "langset_id": ls.id, "added": added})


# ============================================================================
# Documents (list within a project / per-locale show) — upload/translate/download
# already live in api.py (the MCP front door). These fill the read gaps the CLI's
# `doc list` / `doc show` need.
# ============================================================================
@router.post("/api/projects/{pid}/rename")
async def api_projects_rename(pid: int, request: Request):
    """Remote-CLI/web parity for project rename (SPEC-rename-delete). can_write-gated
    (matches the web /projects/{id}/rename route). Org+customer bound via _project_in_org
    (a foreign id 404s). A blank name is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            project = content.rename_project(
                s,
                pid,
                str(body.get("name") or ""),
                actor=ident["actor"],
                ip=deps._ip(request),
                org_id=org_id,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "id": pid, "name": project.name})


@router.post("/api/projects/{pid}/archive")
async def api_projects_archive(pid: int, request: Request):
    """Remote-CLI/web parity for project archive (residual sweep: archive verbs).
    ADMIN-only (same posture as delete + the customer-archive precedent). Ring BEFORE
    role so a foreign id 404s. Retires the project from listings/pickers + blocks new
    runs; data preserved (no cascade). Idempotent-safe (a clean 400 when already
    archived)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        try:
            result = content.archive_project(
                s, pid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except content.ProjectArchiveError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if result is None:  # missing / cross-org -> 404, never a false success (L3)
            return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "id": pid, "archived": True})


@router.post("/api/projects/{pid}/unarchive")
async def api_projects_unarchive(pid: int, request: Request):
    """Remote-CLI/web parity for project unarchive (residual sweep). ADMIN-only. Restores
    the project to listings/pickers + makes it runnable. Ring before role."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        try:
            result = content.unarchive_project(
                s, pid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except content.ProjectArchiveError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if result is None:  # missing / cross-org -> 404, never a false success (L3)
            return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "id": pid, "archived": False})


@router.post("/api/projects/{pid}/ai-opt-out")
async def api_projects_ai_opt_out(pid: int, request: Request):
    """AI opt-out: switch AI off (`{"ai_opt_out": true}`) or back on
    (`false`) for one project. When AI is off:
    This instance's AI engines never process this project's content.
    Content you download, export, or connect to your own AI tools
    (for example the MCP agent connection) is outside this switch.
    Manager or admin (can_manage). Ring BEFORE role so a foreign id 404s.
    Audited (project.ai_opt_out / project.ai_opt_in). A customer-level opt-out still
    applies on top and is reported in `ai_opt_out_customer`."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        value = ai_optout.parse_switch(body.get("ai_opt_out"))
        if value is None:
            return JSONResponse(
                {"ok": False, "error": "ai_opt_out must be true (AI off) or false (AI on)."},
                status_code=400,
            )
        project = ai_optout.set_project(
            s, pid, value, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
        )
        if project is None:  # missing / cross-org -> 404, never a false success
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = {"ok": True, "id": pid, **ai_optout.status(s, project)}
    return JSONResponse(out)


@router.post("/api/projects/{pid}/translate-comments")
async def api_projects_translate_comments(pid: int, request: Request):
    """Translate comments (#690): offer reviewer comments and annotations in Office files
    for translation (`{"translate_comments": true}`) or leave them in the source language
    (`false`). One switch for Word, Excel (notes and threaded comments), PowerPoint,
    OpenDocument and Visio; authors and dates never change. Applies to files uploaded or
    re-imported after the change. Manager or admin. Audited. Returns
    `translate_comments`, `translate_comments_word`, `translate_comments_source`."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        value = comment_option.parse_switch(body.get("translate_comments"))
        if value is None:
            return JSONResponse(
                {"ok": False, "error": "translate_comments must be true or false."},
                status_code=400,
            )
        out = comment_option.set_for(project, value, actor=ident["actor"], ip=deps._ip(request))
    return JSONResponse({"ok": True, "id": pid, **out})


@router.post("/api/projects/{pid}/delete")
async def api_projects_delete(pid: int, request: Request):
    """Remote-CLI/web parity for the full-cascade project delete (SPEC-rename-delete).
    ADMIN-only (deps._admin_block mirror). Typed confirmation: the body's `confirm` must
    equal the EXACT project name. `dry_run: true` returns the itemized pre-counts without
    deleting (the preview surface). Fails closed on Connection/RoutingRule referrers.
    Boolean `privacy_erasure` (#429) also erases Lab snapshots copied from the project's
    documents; otherwise `warning` reports how many snapshots still hold their text."""
    # #441: privacy_erasure also purges the LLMemory entries, events and suggestions learned
    # from the project; a plain delete keeps them, un-scoped to the customer. (Comment, not
    # docstring: the route docstring ships as OpenAPI text, where a literal brand name is banned.)
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # Ring BEFORE role (permanent route-sweep finding): a foreign-org/customer
        # id must 404 before the admin gate ever runs, matching the web
        # project_delete route (_visibility_block before _admin_block) — a
        # customer-bound key is never admin either way, but checking role first
        # would have every non-admin caller's request depend on ring state only
        # incidentally; putting the ring first keeps the two surfaces identical.
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        preview = content.delete_project_preview(s, pid)
        if body.get("dry_run"):
            return JSONResponse({"ok": True, "dry_run": True, **preview})
        if str(body.get("confirm") or "") != preview["project_name"]:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "confirm must equal the exact project name to delete.",
                    **preview,
                },
                status_code=400,
            )
        try:
            counts = content.delete_project(
                s,
                pid,
                actor=ident["actor"],
                ip=deps._ip(request),
                org_id=org_id,
                privacy_erasure=body.get("privacy_erasure") is True,
            )
        except content.ProjectDeleteBlocked as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "blockers": exc.blockers}, status_code=400
            )
    return JSONResponse(
        {
            "ok": True,
            "project_id": pid,
            "deleted": True,
            "cascade": counts,
            "warning": counts.get("warning", ""),
        }
    )


# ============================================================================
# R1a content-parity: project lifecycle + TM-source / glossary attach (SPEC-full-
# remote-parity Wave R1). Thin shells over the SAME services the web forms + the
# in-process CLI use (lifecycle.py, delivery.py, project_sources.py), org+customer
# funnelled through the shared ring so the /api surface cannot drift on tenancy.
# ============================================================================
@router.post("/api/projects/{pid}/status")
async def api_projects_status(pid: int, request: Request):
    """Move a project through its lifecycle (mirrors `project status` + the web
    settings form). can_write. Org+customer bound via _project_in_org (a foreign id
    404s). An unknown status, a move outside the allowed graph, or a repeat of the current
    status is a clean 400 listing the allowed next states (#580)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import lifecycle
    from app.models import ProjectStatus

    status = str(body.get("status") or "").strip().lower()
    with Session(engine) as s:
        # SCOPE FIRST, validate second (the existence-oracle law, api_document_apply_tm
        # + the by-id route sweep): a foreign-customer id must 404 before a bad-status
        # 400 is ever reachable, or the 400-vs-404 difference leaks which ids exist.
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if status not in ProjectStatus.ALL:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"unknown status {status!r} (choose one of: "
                    f"{', '.join(ProjectStatus.ALL)}).",
                },
                status_code=400,
            )
        before = project.status
        # #580: only the allowed graph (lifecycle.MANUAL_TRANSITIONS); the 400 lists the
        # allowed next states. The app's own automatic transitions do not pass through here.
        refused = lifecycle.transition_error(before, status)
        if refused:
            return JSONResponse(
                {
                    "ok": False,
                    "error": refused,
                    "previous": before,
                    "allowed": list(lifecycle.allowed_next(before)),
                },
                status_code=400,
            )
        lifecycle.set_status(s, pid, status, actor=ident["actor"], ip=deps._ip(request))
    return JSONResponse({"ok": True, "project_id": pid, "status": status, "previous": before})


# ============================================================================
# Custom project fields (SPEC-project-automation, MVP part C). Thin shells over
# app.services.custom_fields (the one service web, CLI, /api and MCP share).
# Reading definitions needs only an in-ring key; defining/changing/archiving a field
# is manager-or-admin (managers: customer-scoped, own fields; admins: all); setting a
# project's values is a write action on a project the key can see.
# ============================================================================
@router.get("/api/custom-fields")
def api_custom_fields_list(request: Request, archived: str = Query("")):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        defs = cf_service.list_defs(
            s,
            org_id,
            _cust_of(ident),
            include_archived=archived.strip().lower() in ("1", "true", "all", "yes"),
        )
        payload = [cf_service.to_dict(d) for d in defs]
    return JSONResponse({"ok": True, "fields": payload, "types": list(cf_service.TYPES)})


@router.post("/api/custom-fields")
async def api_custom_fields_create(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    with Session(engine) as s:
        bound = _cust_of(ident)
        cust_id = bound
        if bound is None and str(body.get("customer_id") or "").strip():
            cust_id = _int_id(body.get("customer_id"))
            if cust_id is None or _customer_in_org(s, cust_id, org_id, ident) is None:
                return JSONResponse({"ok": False, "error": "unknown customer."}, status_code=400)
        refusal = cf_service.define_refusal(ident.get("user"), cust_id)
        if refusal:
            return JSONResponse({"ok": False, "error": refusal}, status_code=403)
        try:
            d = cf_service.create_def(
                s,
                body.get("key"),
                body.get("label"),
                str(body.get("type") or "text"),
                options=body.get("options"),
                key_pattern=body.get("key_pattern") or "",
                link_system=body.get("link_system") or "",
                help_text=body.get("help_text") or "",
                required=body.get("required", False),
                show_in_list=body.get("show_in_list", False),
                filterable=body.get("filterable", True),
                list_order=body.get("list_order") or 0,
                org_id=org_id,
                customer_id=cust_id,
                created_by=ident["actor"],
            )
        except cf_service.FieldError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **cf_service.to_dict(d)}
    audit.log(
        actor=ident["actor"],
        action="field.define",
        target=f"custom_field {out['id']} '{out['key']}'",
        detail=f"type={out['type']} required={out['required']}",
        ip=deps._ip(request),
        customer_id=out.get("customer_id"),
    )
    return JSONResponse(out)


@router.patch("/api/custom-fields/{fid}")
async def api_custom_fields_update(fid: int, request: Request):
    """Change a field definition (present-only) or archive/restore it with
    {"archived": true|false}. The key and type are immutable."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    with Session(engine) as s:
        d = cf_service.visible(s, fid, org_id, _cust_of(ident))
        if d is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        refusal = cf_service.modify_refusal(ident.get("user"), ident["actor"], d)
        if refusal:
            return JSONResponse({"ok": False, "error": refusal}, status_code=403)
        body, bad = await _json_object(request)
        if bad:
            return bad
        archived = body.pop("archived", None)
        before = cf_service.to_dict(d)
        try:
            if archived is not None:
                archived = cf_service.as_bool(archived, "archived")
            changed = cf_service.update_def(s, d, **body) if body else []
            archive_changed = (
                cf_service.set_archived(s, d, archived) if archived is not None else False
            )
        except cf_service.FieldError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **cf_service.to_dict(d)}
        cust = d.customer_id
    if changed:
        audit.log(
            actor=ident["actor"],
            action="field.update",
            target=f"custom_field {fid} '{out['key']}'",
            detail=cf_service.change_detail(before, out, changed),
            ip=deps._ip(request),
            customer_id=cust,
        )
    if archive_changed:
        audit.log(
            actor=ident["actor"],
            action="field.archive" if archived else "field.restore",
            target=f"custom_field {fid} '{out['key']}'",
            ip=deps._ip(request),
            customer_id=cust,
        )
    return JSONResponse(out)


@router.get("/api/projects/{pid}/fields")
def api_project_fields_show(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        defs = [
            cf_service.to_dict(d)
            for d in cf_service.defs_for_project(s, project)
            if _cust_of(ident) is None or d.customer_id in (None, _cust_of(ident))
        ]
        values = cf_service.payload_for_projects(s, [project], org_id, _cust_of(ident)).get(
            project.id, {}
        )
    return JSONResponse({"ok": True, "project_id": pid, "custom_fields": values, "fields": defs})


@router.patch("/api/projects/{pid}/fields")
async def api_project_fields_set(pid: int, request: Request):
    """Set (or clear, with null/blank) custom field values: {"fields": {key: value}}.
    All-or-nothing: one bad value writes nothing."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    with Session(engine) as s:
        # SCOPE FIRST, validate second (the existence-oracle law): a foreign id 404s
        # before any body complaint is reachable.
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        body, bad = await _json_object(request)
        if bad:
            return bad
        values = body.get("fields")
        if not isinstance(values, dict):
            return JSONResponse(
                {"ok": False, "error": 'send {"fields": {"<key>": <value>, ...}}.'},
                status_code=400,
            )
        try:
            changes = cf_service.set_values(s, project, values, actor=ident["actor"])
        except cf_service.FieldError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {
            "ok": True,
            "project_id": pid,
            "changed": [c["key"] for c in changes],
            "custom_fields": cf_service.project_values(s, project),
        }
        cust = project.customer_id
    if changes:
        audit.log(
            actor=ident["actor"],
            action="field.set",
            target=f"project {pid}",
            detail=cf_service.audit_detail(changes),
            ip=deps._ip(request),
            customer_id=cust,
        )
    return JSONResponse(out)


@router.post("/api/projects/{pid}/intake")
async def api_projects_set_intake(pid: int, request: Request):
    """Connected projects: switch a project between `upload` and `connected`. Body:
    {"intake": "connected" | "upload"}. Manager and up; allowed ONLY while the project has no
    documents, no keys and no source binding (otherwise a clean 400 naming why). The project
    ring answers first (a foreign or missing id is the shared 404)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        body, bad = await _json_object(request)
        if bad:
            return bad
        try:
            connected_projects.set_intake(s, project, body.get("intake"), actor=ident["actor"])
        except connected_projects.IntakeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {
            "ok": True,
            "project_id": project.id,
            "intake": project.intake,
            **(
                {"setup_url": connected_projects.setup_url(project.id)}
                if connected_projects.is_connected(project)
                else {}
            ),
        }
    return JSONResponse(out)


@router.post("/api/projects/{pid}/make-connected")
async def api_projects_make_connected(pid: int, request: Request):
    """Connected projects: "Make this a connected project" for an EXISTING project that a
    connection already feeds (Connection.project_id) and that has no content source yet. Body:
    {"connection_id": N} (optional when exactly one connection feeds the project). Binds that same
    connection: no lane, no scope change, no schedule, no run. Manager and up; the project ring
    answers first (a foreign or missing id is the shared 404). Refused with a 400 that says why for
    uploaded documents, inbox and archived projects, projects that are already connected or bound.
    Never touches a credential and never creates a connection."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        body, bad = await _json_object(request)
        if bad:
            return bad
        try:
            row = connected_projects.convert_existing(
                s, project, body.get("connection_id"), actor=ident["actor"]
            )
        except connected_projects.IntakeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {
            "ok": True,
            "project_id": project.id,
            "intake": project.intake,
            "project_type": project.project_type,
            "connection_id": row.connection_id,
            "source_url": connected_projects.setup_url(project.id),
        }
    return JSONResponse(out)
