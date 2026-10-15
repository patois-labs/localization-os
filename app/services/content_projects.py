"""content: projects

Documented in: docs/architecture/content-and-projects.md
"""

from sqlmodel import Session, select

from app import config
from app.models import (
    Customer,
    Document,
    Project,
    ProjectPairProvider,
    ProjectType,
    ProviderMode,
    Segment,
    TargetLocale,
)
from app.services import access, ids, languages_catalog
from app.services.content_part2 import delete_project_preview, remove_locale_preview
from app.services.content_project_rename_delete_sp import (
    _SQLITE_INT64_MAX,
    ArchivedCustomerError,
    DuplicateNameError,
    ForeignCustomerError,
    InvalidContentTypeError,
    InvalidLocaleError,
    InvalidProjectTypeError,
    LocaleRemovalBlocked,
    MissingCustomerError,
    ProjectDeleteBlocked,
    UnknownCustomerError,
    _locale_query_thread_ids,
    _org_for,
    _project_job_ids,
    _project_locale_rows,
    _rollback_project_create,
    find_duplicate_project,
    org_has_selectable_customer,
    parse_due_date,
)


# --- projects ----------------------------------------------------------------
def project_create(
    session: Session,
    name: str,
    source_locale: str = "en-US",
    target_locales: str = "",
    customer_id=None,
    default_provider: str | None = None,
    created_by: str = "",
    creator=None,
    bound_customer_id=None,
    due_date=None,
    provider_mode=None,
    pair_providers=None,
    org_id=None,
    content_type_id=None,
    require_customer=False,
    project_type=None,
    content_type_scope=None,
    reject_duplicate=False,
    intake=None,
) -> Project | None:
    """Create a project plus a de-duplicated TargetLocale per target locale.

    `target_locales` is a comma/space-separated string (as the form sends it).
    `customer_id` may be an int, a digit-string, or None/blank -> no customer.
    `creator` (the acting User) is optional and only consulted when multi-org is
    active, to stamp the project's org (see _org_for); omitting it — as every
    existing caller does with the gate off — leaves org_id NULL for the Default-org
    backfill, byte-identical to today.

    `bound_customer_id` (R2-3 hostile-review fix, Finding E): the CALLER's own
    customer binding, when the caller is a customer-bound account (None = unbound
    / internal staff / trusted in-process caller — unchanged behavior, exactly the
    same shape apply_template's bound_customer_id already uses). When given:
      - an explicit customer_id that resolves to a DIFFERENT customer than the
        caller's own is REJECTED (ForeignCustomerError) — never silently
        creates/attributes the project into a foreign tenant;
      - no customer_id (or the caller's own) is accepted, and the project is
        FORCE-STAMPED to the caller's own customer even if customer_id was blank
        — a bound user can never end up with a customer-less/orphan project.
    This is the seam apply_template's already-resolved customer_id flows through
    (it passes cid == the caller's own customer for a bound caller applying a
    global template), so that path continues to work unchanged: "caller's own
    customer" is always accepted here.
    `reject_duplicate` (#580, opt-in like require_customer): refuse a name that an ACTIVE
    project of the same customer already has (case and surrounding space ignored) with
    DuplicateNameError; the user-facing seams pass it, existing duplicates are untouched.
    A customer id that does not exist raises UnknownCustomerError (always).
    `intake` (Connected projects, 2026-10-13): "upload" (blank, the default) or "connected".
    A connected project is created empty as a Documents project and gets its content from a
    connected system; an invalid value or a key_based shape raises
    connected_projects.IntakeError, before any write.
    Returns the created Project, or None when the name is blank (the route
    redirects home in that case)."""
    name = ids.clean_name(name, field="project name")
    if not name:
        return None

    # Accept `target_locales` as a comma/space STRING (the form/CLI shape) OR a
    # list/tuple (the natural /api JSON shape). The /api route previously str()-
    # coerced a JSON list into its Python repr ("['fr-FR']"), which then split into a
    # junk token and 400'd a perfectly valid, catalog-present locale (the fr-FR aside,
    # 2026-08-08 audit — the catalog itself is correct: is_valid('fr-FR') is True).
    # validate_codes already handles both shapes; normalize any OTHER scalar to a
    # string so a stray int/None can never raise inside validate_codes.
    if not isinstance(target_locales, (str, list, tuple)):
        target_locales = str(target_locales or "")

    # Feature A: parse/validate the optional due date BEFORE the project row is
    # created — a garbage date (raises DueDateError) fails the WHOLE create, never
    # leaving an orphaned project behind. None/"" = no due date (the default).
    due_dt = parse_due_date(due_date)

    # Validate the SOURCE locale against the BCP-47 catalog too (bug #8): --source
    # was previously asymmetric with --targets (below) -- a garbage target code was
    # rejected but a garbage source code sailed through and got stamped on the
    # project. A blank source falls back to "en-US" (the documented default) BEFORE
    # validation, matching the field's default elsewhere in this function, so an
    # omitted --source never raises.
    src = (source_locale or "").strip() or "en-US"
    if not languages_catalog.is_valid(src):
        raise InvalidLocaleError([src])

    # Validate the target locales against the BCP-47 catalog (P2, item 5). Garbage
    # is rejected with a clear error BEFORE the project row is created; disabled-but-
    # valid codes are accepted (the enabled list is only a UI-offering filter). Valid
    # codes are canonicalized (es_ES -> es-ES) and de-duped by validate_codes.
    valid, invalid = languages_catalog.validate_codes(target_locales)
    if invalid:
        raise InvalidLocaleError(invalid)

    # Match the route's customer_id coercion: only a digit-bearing value binds.
    # Bounded to SQLite's signed-64-bit range so an oversized digit string (typo,
    # fuzzer) resolves to "no customer" instead of an OverflowError from the
    # SQLite binder several layers down at the session.get() below.
    cust_id = None
    if isinstance(customer_id, int):
        cust_id = customer_id
    elif isinstance(customer_id, str) and customer_id.isdigit():
        # str.isdigit() is True for a Unicode digit int() rejects (U+00B2 -> ValueError);
        # the bound (added earlier for the >2**63 overflow) did not cover THIS edge, so a
        # forged/typo customer_id still 500'd here -- guard the int() too.
        try:
            n = int(customer_id)
        except ValueError:
            n = None
        cust_id = n if (n is not None and n <= _SQLITE_INT64_MAX) else None

    # R2-3 (Finding E): the customer-bound guard, checked BEFORE default-provider
    # inheritance / row creation below so a rejected request never touches the DB.
    # A bound caller (bound_customer_id given) may only create into their OWN
    # customer: an explicit customer_id resolving to someone else's is refused
    # outright (never silently attributed to the foreign tenant), and a missing/
    # blank customer_id is force-stamped to the caller's own — a bound user can
    # never end up with a customer-less project. An unbound/internal/in-process
    # caller (bound_customer_id None, e.g. the CLI acting as itself) is unchanged.
    bound_cid = None
    if isinstance(bound_customer_id, int):
        bound_cid = bound_customer_id
    elif isinstance(bound_customer_id, str) and bound_customer_id.isdigit():
        bound_cid = int(bound_customer_id)
    if bound_cid is not None:
        if cust_id is not None and cust_id != bound_cid:
            raise ForeignCustomerError(cust_id)
        cust_id = bound_cid

    # #554: the org this project belongs to. The creating User's org when there is one;
    # otherwise the caller's own scope (an org-bound principal that carries an org but no
    # User, e.g. an /api identity or the CLI --org scope). Only under an ACTIVE gate:
    # with it off both are None and the row stays un-stamped for the Default-org backfill,
    # byte-identical to before. The -1 poison of an org-less principal and UNSCOPED collapse
    # to None (stampable_org_id), so nothing ever persists a sentinel.
    proj_org = _org_for(creator)
    if proj_org is None and access.multi_org_active():
        proj_org = access.stampable_org_id(org_id)

    # #554: an explicit customer must live in the caller's org (multi-org on). The bound-
    # caller check above only confines a customer-BOUND caller; an unbound caller in org A
    # could name org B's customer and plant a project there. The caller's scope is the
    # deliberate content_type_scope when the CLI passed one (access.UNSCOPED = the
    # key-verified --operator door, which may cross orgs), else the stamp org, else the
    # org_id parameter. The -1 poison of an org-less principal fails closed, and a NULL-org
    # customer is quarantined exactly as access.same_org does.
    # A nonexistent id is left to the existing FK path. Nothing here runs with the gate off.
    if cust_id is not None and access.multi_org_active():
        scope = content_type_scope if content_type_scope is not None else proj_org
        if scope is None:
            scope = org_id
        # A bare None is a trusted in-process caller with no org to narrow to (no creator,
        # no org_id, no scope): unscoped, like every other service seam. An org-less
        # principal arrives as the -1 poison, never as None.
        if scope is not None and scope is not access.UNSCOPED:
            _scope_cust = session.get(Customer, cust_id)
            if _scope_cust is not None and (
                scope == access.BLOCKED_ORG_ID or getattr(_scope_cust, "org_id", None) != scope
            ):
                raise ForeignCustomerError(cust_id)

    # 2026-07-14: every USER-FACING create seam requires a customer (there is no
    # "no-customer" bucket). This is an OPT-IN gate (require_customer) the user-facing
    # callers pass — after the bound-caller force-stamp above, so a bound caller's own
    # customer satisfies it — and BEFORE any DB write, so a customer-less create fails
    # cleanly with no orphan row.
    #
    # SEAM DECISION (documented, so hermetic fixtures don't all break — the directive's
    # ask): enforcement fires ONLY when the caller's ORG actually has a SELECTABLE
    # (active, same-org) customer to choose. Rationale:
    #   * a real install always has at least one customer, so a real user creating a
    #     project through the dialog/CLI/API/promptable can never end up customer-less
    #     — the requirement holds everywhere it can be satisfied;
    #   * a pristine / hermetic-test install with ZERO customers literally cannot pick
    #     one, so a customer-less project is allowed there — and it is flagged for
    #     assignment (the banner + `project set-customer`), consistent with the legacy
    #     customer-less handling. This keeps the ~dozens of service+route test fixtures
    #     that never create a customer green, while the new "requires-customer" suite
    #     (which DOES create a customer first) still sees the reject.
    # Internal/trusted callers + hermetic service fixtures leave require_customer False
    # and are never gated at all (the enforcement is at the seam, not in the service).
    if require_customer and cust_id is None:  # noqa: SIM102
        if org_has_selectable_customer(session, _org_for(creator)):
            raise MissingCustomerError()

    # SPEC-customer-archive: a project can never be created for an ARCHIVED customer
    # (archive retires a customer from every "choose a customer" surface). Checked at
    # the SERVICE so a forged POST / stray CLI id is refused, not just omitted from the
    # UI picker. A nonexistent id is left to the existing FK-enforcement path
    # (unchanged); only an EXISTING-but-archived customer is rejected here. Runs after
    # the bound-caller force-stamp so a bound user whose OWN customer was archived is
    # also correctly refused. No-op when no customer is attached (cust_id None).
    if cust_id is not None:
        _cust_row = session.get(Customer, cust_id)
        if _cust_row is None:
            # #580: a nonexistent id used to reach the INSERT and die on the foreign key (a 500
            # on /api). Refuse it here, before any write, on every surface.
            raise UnknownCustomerError(cust_id)
        if getattr(_cust_row, "archived_at", None) is not None:
            raise ArchivedCustomerError(cust_id)
    if reject_duplicate:
        _twin = find_duplicate_project(session, name, cust_id)
        if _twin is not None:
            raise DuplicateNameError("project", name, _twin.id)

    # #577: choosing the offline Mock engine for a project is admin-only, like the install
    # default. Checked on what the CALLER supplied (before customer-default inheritance, which
    # is not a choice) and before any row is written. A no-op for the trusted in-process
    # caller (creator None) and with sign-in off.
    from app.services import pair_providers as _mock_pp

    _mock_pp.refuse_mock_unless_admin(
        creator, [default_provider, *_mock_pp.parse_pair_mapping(pair_providers).values()]
    )

    # An OMITTED provider (None or blank) means "the install's configured default"
    # (config.DEFAULT_PROVIDER, which Settings push at boot), the same value the web
    # new-project form, the CLI and /api/projects use. Never a hardcoded engine name.
    _omitted = not (default_provider or "").strip()
    if _omitted:
        default_provider = config.DEFAULT_PROVIDER or "claude"

    # Customer default inheritance (P4, item 5): when a customer is chosen and the
    # caller did NOT pick a specific provider (omitted, or the built-in "claude"
    # fallback), inherit the customer's default_provider. A caller that explicitly
    # chose a different provider is respected (never overridden). Byte-identical when
    # no customer is bound or the customer has no default — the provider stays as
    # supplied.
    if cust_id is not None and (_omitted or default_provider.strip() == "claude"):
        cust = session.get(Customer, cust_id)
        if cust and (cust.default_provider or "").strip():
            default_provider = cust.default_provider

    # Content type (SPEC-content-types). Resolve + validate BEFORE the project row is
    # created so a bad/foreign content type fails the WHOLE create (no orphan
    # project), exactly like the due-date / seg-profile / pair-provider validations.
    #   * inherit the customer's default_content_type_id when the caller passed none
    #     (same inherit-at-create pattern as default_provider above);
    #   * validate the (possibly inherited) id via content_types.attachable against
    #     the project's OWN org + resolved customer: it must exist and be GLOBAL or
    #     scoped to THIS customer. A customer-bound caller's cust_id was already
    #     force-stamped above, so this mirrors ForeignCustomerError's R2-3 confinement
    #     — a bound caller can never attach another customer's type. A non-existent /
    #     foreign id raises InvalidContentTypeError (indistinguishable, by design).
    # With zero ContentType rows + a NULL input this whole block is a no-op (the
    # backward-compat invariant): content_type_id stays None, byte-identical to today.
    # (proj_org was resolved above, #554.)
    # #73: content_types.attachable now fails closed on a bare None org_id under an active
    # gate. `proj_org` is the poison-collapsed STAMP org (stampable_org_id folds the -1
    # sentinel AND UNSCOPED to None), so a legitimate cross-org CLI --operator create would
    # otherwise validate the type against a bare None and fail closed. `content_type_scope`
    # (op_org_scope from the CLI: access.UNSCOPED for the --operator door, the concrete id
    # for --org, None otherwise) is the DELIBERATE resolution scope; every other surface
    # (web/api/templates) omits it -> falls back to proj_org, byte-identical to today.
    #
    # GATE CATCH (probe-proven): `proj_org` is creator-DERIVED, but not every legitimate
    # principal has a `creator`. /api reads it as `ident.get("user")`, which is None for
    # the SHARED CONNECTOR TOKEN even when that token is BOUND to a real org -- so a
    # perfectly legitimate org-A token creating a project with its OWN org-A content type
    # (or merely inheriting the customer's default_content_type_id) resolved against a bare
    # None and 400'd. The caller's actual principal scope is the `org_id` PARAMETER (
    # _ident_org_id / _my_org_id -- poison-protected to BLOCKED_ORG_ID for an org-less
    # principal), so fall back to it when there is no creator org. Net effect vs. before
    # #73: an org-BOUND token now resolves types in ITS OWN org instead of every org (a
    # NARROWING), an UNBOUND/org-less principal still fails closed on the -1 poison, and
    # gate-off is unchanged (both values are None -> no filter).
    resolve_org = content_type_scope if content_type_scope is not None else proj_org
    if resolve_org is None:
        resolve_org = org_id
    ct_raw = content_type_id
    if (ct_raw is None or str(ct_raw).strip() == "") and cust_id is not None:
        cust = session.get(Customer, cust_id)
        if cust is not None and cust.default_content_type_id is not None:
            ct_raw = cust.default_content_type_id
    resolved_ct_id = None
    if ct_raw is not None and str(ct_raw).strip() != "":
        from app.services import content_types as _ct

        ct = _ct.attachable(session, ct_raw, org_id=resolve_org, customer_id=cust_id)
        if ct is None:
            raise InvalidContentTypeError(ct_raw)
        resolved_ct_id = ct.id

    # Project type (SPEC-key-based-projects 2.1). Resolved BEFORE the row is created
    # so an unknown type fails the WHOLE create, like every other validation above.
    # None/"" -> "document", which is what every existing caller passes implicitly, so
    # this is byte-identical for them. A key-based create is ALSO gated on the module
    # kill switch here at the SERVICE seam, so a forged POST or a stray CLI flag cannot
    # make one while the feature is off (the surfaces gate too, Wave B).
    ptype = (project_type or "").strip() or ProjectType.DOCUMENT
    if ptype not in ProjectType.ALL:
        raise InvalidProjectTypeError(ptype)
    if ptype == ProjectType.KEY_BASED:
        from app.services import keys as _keys

        if not _keys.enabled():
            raise InvalidProjectTypeError(ptype)

    # Connected projects: where the content comes from. Validated BEFORE the row is created,
    # like every other input above (a refused intake leaves no orphan project).
    from app.services import connected_projects as _cp

    intake_v = _cp.normalize_intake(intake)
    _cp.check_create(intake_v, ptype)

    # Jev judges: a quality-judge engine can never be a project's default (translate)
    # provider. Refused as a PairProviderError because every create surface already
    # catches that one as a clean 400 / flash / EXIT_USAGE; before any DB write.
    from app.services import engines as _engines

    try:
        _engines.assert_ref_role(default_provider, "translate")
    except _engines.EngineRoleError as exc:
        from app.services.pair_providers import PairProviderError

        raise PairProviderError(str(exc)) from exc

    project = Project(
        name=name,
        project_type=ptype,
        intake=intake_v,
        source_locale=src,
        default_provider=default_provider,
        customer_id=cust_id,
        created_by=created_by,
        org_id=proj_org,
        due_date=due_dt,
        content_type_id=resolved_ct_id,
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    for loc in valid:
        session.add(TargetLocale(project_id=project.id, locale=loc))
    session.commit()

    # Per-pair provider mode (Phase 2). UNIFORM (default/None) is a no-op and
    # leaves provider_mode at its column default, byte-identical to pre-Phase-2.
    # PER_PAIR validates the mapping against the just-created TargetLocale rows and
    # persists it; a bad mapping raises PairProviderError, failing the WHOLE create
    # (the project row would be rolled back by the caller's error path — but we
    # delete it here first so a service caller that swallows the error is never left
    # with an orphaned half-configured project). The rollback also covers any OTHER
    # failure inside apply_mode (e.g. a DB-level error during its commit) so a
    # swallowed non-PairProviderError exception can't leave an orphaned project
    # either — same rollback, then re-raised so the caller still sees the failure.
    from app.services import pair_providers as _pp

    mode = _pp.normalize_mode(provider_mode)
    if mode == ProviderMode.PER_PAIR or pair_providers:
        try:
            mapping = _pp.parse_pair_mapping(pair_providers)
            _pp.apply_mode(session, project, mode, mapping, org_id=org_id, user=creator)
        except _pp.PairProviderError:
            # Roll back the whole create so a swallowed error never leaves an
            # orphaned half-configured project behind.
            _rollback_project_create(session, project)
            raise
        except Exception:
            # failure (e.g. a DB error inside apply_mode's commit); never leave an
            # orphaned project behind, then re-raise so the caller still sees it.
            _rollback_project_create(session, project)
            raise

    # #690: a new project starts with comment translation OFF in every Office format
    # (Word included); projects from before the option keep Word comments on.
    from app.services import comment_option as _comment_option

    _comment_option.stamp_new_project(project, session)

    from app.services import notify as _notify

    _notify.notify("project.created", project.id, {"dedup": f"created:{project.id}"})
    return project


def delete_project(
    session: Session,
    project_id: int,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
    privacy_erasure: bool = False,
) -> dict | None:
    """FULL-cascade delete of a project + every OWNED child row (documents + upload
    blobs, segments, TM, embeddings, jobs, run history, LQA, target locales, pair
    providers, members, legacy glossary entries, project-scoped workflows). FAILS CLOSED
    on the INDEPENDENT referrers Connection.project_id / RoutingRule.project_id — raises
    ProjectDeleteBlocked with the live counts and deletes NOTHING.

    The CALLER owns the admin gate + the typed confirmation (web types the exact name,
    CLI passes --force, /api sends a matching `confirm`). Org-guarded (a cross-org id
    resolves to None). Reuses retention.delete_document (cascade_tm=False — a project
    delete clears the shared TM ONCE below, not per-document) for the per-document purge.
    Hand-ordered child-before-parent with flushes (no ondelete= anywhere). Returns the
    cascade counts dict, or None when the project doesn't exist / is cross-org.

    Lab snapshots copied from the project's documents survive unless `privacy_erasure` is
    True (#429, the project-wide form of the #383 document option). The result always
    carries `lab_datasets_erased` / `lab_datasets_preserved`, plus a `warning` string when
    snapshots were preserved.

    WHY THIS STILL PRE-REAPS run history that retention.delete_document now reaps per
    document (2026-08-31 fix-train): the per-document seam can only see runs whose
    `document_id` is one of THIS project's documents, and `run_ids` below also spans runs of
    a PROJECT-SCOPED WORKFLOW whose document lives in another project (the `workflow_id`
    arm) — those have no per-document delete to ride on and would strand the workflow delete
    at step 3c. The overlap is deliberate and harmless: every reap here is idempotent, so
    step 4 simply finds nothing left to do."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import or_ as _or_
    from sqlalchemy import update as sa_update

    from app.models import (
        ContextProfile,
        DeliveryEmptyApproval,
        Document,
        EvalCandidate,
        GlossaryEntry,
        GlossarySense,
        Job,
        KeyChangeLog,
        KeyImage,
        LQAResult,
        MemoryEntry,
        MemoryEvent,
        MemorySuggestion,
        ProjectAutomation,
        ProjectBindingClaim,
        ProjectBudget,
        ProjectGlossary,
        ProjectMember,
        ProjectPairProvider,
        ProjectTmSource,
        QueryPost,
        QueryThread,
        Segment,
        SegmentPostEdit,
        StepRun,
        StringKey,
        TargetLocale,
        Workflow,
        WorkflowBatch,
        WorkflowRun,
        WorkflowStep,
    )
    from app.services import audit, retention

    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    preview = delete_project_preview(session, project_id)
    # Early exit for INDEPENDENT REFERRERS (Connection / RoutingRule) only -- key on those two,
    # NOT on blockers["total"], because total now also carries the forecast
    # workflow_memory_suggestions count (LOW-2). A suggestion-targeted project must flow past
    # this generic message down to step 3c, whose raise carries the tailored "archive instead"
    # wording; short-circuiting here would surface the wrong (0 connections / 0 routing rules)
    # message. Behaviour for the referrer case is byte-identical to before this train.
    _b = preview["blockers"]
    if _b["connections"] + _b["routing_rules"] > 0:
        raise ProjectDeleteBlocked(project_id, _b)
    counts = preview["cascade"]
    cust_id, name = project.customer_id, project.name

    doc_ids = list(session.exec(select(Document.id).where(Document.project_id == project_id)).all())
    seg_ids = (
        list(session.exec(select(Segment.id).where(Segment.document_id.in_(doc_ids))).all())
        if doc_ids
        else []
    )
    wf_ids = list(session.exec(select(Workflow.id).where(Workflow.project_id == project_id)).all())
    # Run ids = runs on this project's documents OR runs of a project-scoped workflow.
    run_ids: set = set()
    if doc_ids:
        run_ids.update(
            session.exec(select(WorkflowRun.id).where(WorkflowRun.document_id.in_(doc_ids))).all()
        )
    if wf_ids:
        run_ids.update(
            session.exec(select(WorkflowRun.id).where(WorkflowRun.workflow_id.in_(wf_ids))).all()
        )
    run_ids = list(run_ids)

    # ORDER (#63): the WorkflowRun delete moved DOWN to step 3, after its FK referrers.
    # VendorDispatch.workflow_run_id (NOT NULL) and MemoryEvent.workflow_run_id (nullable)
    # both reference a WorkflowRun, and VendorDispatch is ITSELF referenced by LQAResult /
    # SegmentPostEdit / QueryThread / QueryPost (.dispatch_id). So the child-before-parent
    # chain is: LQA/PE + queries (1) -> VendorDispatch legs + MemoryEvent run-unscope (2)
    # -> StepRun/WorkflowRun (3). Previously the WorkflowRun delete ran FIRST, so a project
    # carrying a VendorDispatch leg or a MemoryEvent raised IntegrityError and rolled back
    # (undeletable).
    #
    # 1. LQA results + post-edit episode rows (reference document_id / segment_id / a
    #    dispatch_id) BEFORE the document delete AND before the VendorDispatch purge.
    #    SegmentPostEdit is a Segment child (§3.2.6); cleared here symmetrically with
    #    LQAResult so a Postgres FK is clean even though the per-doc reap at step 4
    #    (retention.delete_document) would also remove it.
    if doc_ids:
        session.exec(sa_delete(LQAResult).where(LQAResult.document_id.in_(doc_ids)))
        session.exec(sa_delete(SegmentPostEdit).where(SegmentPostEdit.document_id.in_(doc_ids)))
    if seg_ids:
        session.exec(sa_delete(LQAResult).where(LQAResult.segment_id.in_(seg_ids)))
        session.exec(sa_delete(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(seg_ids)))
    # 1b. Query threads + their posts (SPEC-wave3-collaboration §3.5). Threads are
    #     project children (project_id anchor), so the whole project's threads go here,
    #     child (posts) before parent (threads), BEFORE the document delete AND before the
    #     VendorDispatch purge (a thread/post may carry a dispatch_id). NOT feature-gated:
    #     a flag never switches referential integrity (§5.2 seam 4). Also swept per-document
    #     by retention.delete_document at step 4; this project-wide pass catches
    #     project/key-scoped threads that a per-doc reap misses.
    q_thread_ids = list(
        session.exec(select(QueryThread.id).where(QueryThread.project_id == project_id)).all()
    )
    if q_thread_ids:
        session.exec(sa_delete(QueryPost).where(QueryPost.thread_id.in_(q_thread_ids)))
        session.flush()
        session.exec(sa_delete(QueryThread).where(QueryThread.project_id == project_id))
        session.flush()
    # 2. VendorDispatch legs + MemoryEvent run-provenance for this project's runs, BOTH
    #    before the WorkflowRun delete (#63). The legs' dispatch_id referrers (step 1/1b)
    #    are gone; MemoryEvent.document_id/segment_id are un-scoped per-document by
    #    retention.delete_document at step 4 and MemoryEvent.project_id at step 8e.
    retention.purge_vendor_dispatch_legs(session, run_ids)
    retention.unscope_memory_event_runs(session, run_ids)
    # 2c. Batch 15 T2: DeliveryEmptyApproval rows FK project.id / document.id / workflowrun.id,
    #     so they must be reaped BEFORE the WorkflowRun (step 3) and Document (step 4) deletes
    #     or SQLite FK enforcement raises IntegrityError. Keyed on project_id, which reaps
    #     BOTH doc-mode and key-mode (document_id NULL) rows in one pass (§5.2 cascade).
    session.exec(
        sa_delete(DeliveryEmptyApproval).where(DeliveryEmptyApproval.project_id == project_id)
    )
    session.flush()
    #     ...but `run_ids` above also spans runs of a PROJECT-SCOPED WORKFLOW whose DOCUMENT
    #     belongs to ANOTHER project (the workflow_id arm), and such a run can be pointed at
    #     by that other project's approval. project_id alone misses those, so NULL the run_id
    #     on any survivor before step 3 deletes the run. Un-scoped, never deleted: the row is
    #     another project's durable human release decision and run_id is informational
    #     provenance only (the same treatment retention._reap_document_children and
    #     workflows.delete_workflow's B-H2 give it).
    if run_ids:
        session.exec(
            sa_update(DeliveryEmptyApproval)
            .where(DeliveryEmptyApproval.run_id.in_(run_ids))
            .values(run_id=None)
        )
        session.flush()
    # 3. run history: StepRun -> WorkflowRun (child before parent, flush-pinned).
    if run_ids:
        session.exec(sa_delete(StepRun).where(StepRun.workflow_run_id.in_(run_ids)))
        session.flush()
        session.exec(sa_delete(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))
        session.flush()
    # 3b. WorkflowBatch fan-out rows: they FK BOTH `document.id` (NOT NULL — so they block
    #     the step-4 document delete) AND `workflow.id` (so they block the step-3c workflow
    #     delete below). Reaped HERE, between the two, keyed on either leg. Their members are
    #     the runs deleted at step 3, so nothing points at them any more (the surviving-member
    #     un-scope is done defensively inside retention._reap_document_children, which repeats
    #     this per document at step 4 and finds nothing left — idempotent, like every other
    #     overlap between this project-wide pass and the per-document one).
    _batch_ids = list(
        session.exec(
            select(WorkflowBatch.id).where(
                _or_(
                    WorkflowBatch.document_id.in_(doc_ids or [-1]),
                    WorkflowBatch.workflow_id.in_(wf_ids or [-1]),
                )
            )
        ).all()
    )
    if _batch_ids:
        session.exec(
            sa_update(WorkflowRun).where(WorkflowRun.batch_id.in_(_batch_ids)).values(batch_id=None)
        )
        session.flush()
        session.exec(sa_delete(WorkflowBatch).where(WorkflowBatch.id.in_(_batch_ids)))
        session.flush()
    # 3c-pre. EvalCandidate.workflow_id (Lab, NULLABLE FK) and MemorySuggestion.workflow_id
    #    (LAW-2 tombstone, NOT NULL FK) both point at project-scoped workflow.id and so would
    #    trip the SAME step-3c IntegrityError the step-3b batch reap prevents (a nullable FK is
    #    NOT auto-nulled when its parent is deleted). Handle them INLINE, exactly as
    #    workflows.delete_workflow does at its steps 4c / the memory-suggestion guard -- NOT by
    #    calling delete_workflow, whose own COMMIT mid-cascade is what husked a project on
    #    2026-09-03 (6d82ca7b): un-scope the nullable EvalCandidate pointer (the row survives; its
    #    config snapshot is self-contained), and FAIL CLOSED on any MemorySuggestion tombstone
    #    (NOT NULL, so it can be neither un-scoped nor -- LAW 2 -- deleted). Nothing is committed
    #    before step 3c (retention.delete_document's commits begin at step 4), so the raise rolls
    #    the whole delete back cleanly with an actionable 400, not a raw FK 500 or a husk. Both
    #    are latent today (no surface sets Workflow.project_id); this is defensive future-proofing.
    if wf_ids:
        _wf_sugg = session.exec(
            select(MemorySuggestion.id).where(MemorySuggestion.workflow_id.in_(wf_ids))
        ).all()
        if _wf_sugg:
            from app import branding

            raise ProjectDeleteBlocked(
                project_id,
                {
                    "connections": 0,
                    "routing_rules": 0,
                    "workflow_memory_suggestions": len(_wf_sugg),
                },
                message=(
                    f"cannot delete project {project_id}: {len(_wf_sugg)} "
                    f"{branding.llmemory_name()} improvement suggestion(s) target a "
                    "project-scoped workflow and are permanent audit records that are never "
                    "deleted. Archive the workflow(s) or the project instead of deleting."
                ),
            )
        session.exec(
            sa_update(EvalCandidate)
            .where(EvalCandidate.workflow_id.in_(wf_ids))
            .values(workflow_id=None)
        )
        session.flush()
    # 3c. project-scoped workflows + their steps (never created via any surface today,
    #    but the FK exists so clear it defensively; their runs are gone above).
    if wf_ids:
        session.exec(sa_delete(WorkflowStep).where(WorkflowStep.workflow_id.in_(wf_ids)))
        session.flush()
        # #356 phase 2b: their system-prompt override history (a plain FK onto workflow.id).
        from app.models import WorkflowSystemPromptOverride

        session.exec(
            sa_delete(WorkflowSystemPromptOverride).where(
                WorkflowSystemPromptOverride.workflow_id.in_(wf_ids)
            )
        )
        session.flush()
        session.exec(sa_delete(Workflow).where(Workflow.id.in_(wf_ids)))
        session.flush()
    session.commit()
    # 4. documents + upload blobs + segments + anything still FK-pointing at them (reuse
    #    retention.delete_document, the one FK-clean per-document seam; TM is cleared once in
    #    step 5, NOT per-document — it is project-scoped + shared). Steps 1-3 have already
    #    removed most of what it looks for; it is idempotent on an empty set.
    counts["file_pending"] = 0
    counts["file_removed"] = 0
    counts["lab_datasets_erased"] = 0
    counts["lab_datasets_preserved"] = 0
    for doc_id in doc_ids:
        doc_counts = retention.delete_document(
            session, doc_id, cascade_tm=False, privacy_erasure=privacy_erasure
        )
        for key in (
            "file_pending",
            "file_removed",
            "lab_datasets_erased",
            "lab_datasets_preserved",
        ):
            counts[key] += doc_counts.get(key, 0)
    # 5. the project's translation memory (SegmentEmbedding -> SegmentTM/FullTextTM).
    retention.delete_project_tm(session, project_id)
    # 6. legacy per-project glossary entries + their senses (child before parent).
    entry_ids = list(
        session.exec(select(GlossaryEntry.id).where(GlossaryEntry.project_id == project_id)).all()
    )
    if entry_ids:
        session.exec(sa_delete(GlossarySense).where(GlossarySense.glossary_entry_id.in_(entry_ids)))
        session.flush()
        session.exec(sa_delete(GlossaryEntry).where(GlossaryEntry.id.in_(entry_ids)))
        session.flush()
    # 7. remaining direct project children (incl. project-scoped context cards — pure
    #    config owned by the project, so they cascade rather than block the delete).
    session.exec(sa_delete(ProjectPairProvider).where(ProjectPairProvider.project_id == project_id))
    session.exec(sa_delete(TargetLocale).where(TargetLocale.project_id == project_id))
    session.exec(sa_delete(ProjectMember).where(ProjectMember.project_id == project_id))
    session.exec(sa_delete(ContextProfile).where(ContextProfile.project_id == project_id))
    session.flush()
    # 8. jobs owned by this project (no FK; payload-scanned). Order-free.
    job_ids = _project_job_ids(session, project_id, doc_ids)
    if job_ids:
        session.exec(sa_delete(Job).where(Job.id.in_(job_ids)))
        session.flush()
    # 8b. KEY-BASED PROJECTS (SPEC-key-based-projects 2.2/2.3). Both tables carry
    #     FOREIGN KEY(project_id) -> project.id, and SQLite FK enforcement is ON, so
    #     WITHOUT this the DELETE at step 9 fails -- after steps 4/5 have already
    #     COMMITTED the internal key documents, every value row and the project TM.
    #     The project survived as an unrecoverable husk and every retry failed the
    #     same way. Ordered changelog-first (it has no FK to stringkey, only the
    #     denormalized key_id that lets a tombstone outlive its key) then stringkey,
    #     which the step-4 segment purge has already stopped referencing. The cascade
    #     must be COMPLETE, not best-effort: SQLite reuses rowids, so an orphan here
    #     would be handed to whichever project takes this id next.
    key_ids = list(
        session.exec(select(StringKey.id).where(StringKey.project_id == project_id)).all()
    )
    # Keys bridge (format Batch 9): the resource-file templates a base-locale import stored
    # (one blob per namespace and format). Computed from the key namespaces BEFORE the keys
    # go, deleted after the final commit like the KeyImage blobs below.
    from app.services import key_files_bridge as _bridge

    _tpl_blob_keys = _bridge.template_blob_keys(session, session.get(Project, project_id))
    session.exec(sa_delete(KeyChangeLog).where(KeyChangeLog.project_id == project_id))
    session.flush()
    # 8b-img. KeyImage (Batch 14, LAW-IMG-15): FOREIGN KEY(key_id) -> stringkey.id with
    #     SQLite FK enforcement ON, so WITHOUT this the sa_delete(StringKey) below raises
    #     IntegrityError -- the first attached image would make the project undeletable +
    #     half-deletable (T-11). Reap the blobs FIRST (the ONLY lifecycle site that does,
    #     H-4), best-effort so a missing blob never aborts the delete, then delete the rows
    #     BEFORE the keys. project_id is the tenant anchor, so the reap is project-scoped.
    from sqlalchemy import or_ as _ki_or

    from app.models import BlobDeleteOutbox
    from app.ports import blobstore

    # L-4: reap on EITHER leg (key_id in this project's keys OR the denormalized
    # project_id anchor), so a row the preview counted by project_id can never be left as
    # a husk that fails the FK DELETE below (LAW-IMG-15). Runs even when key_ids is empty.
    _ki_where = _ki_or(KeyImage.key_id.in_(key_ids), KeyImage.project_id == project_id)
    # #543: the blob delete is irreversible, so it must NOT run before the final commit (a later
    # step or the commit failing rolls the KeyImage rows back pointing at destroyed blobs, and the
    # outbox rows for failed deletes roll back with them). Stage each key in BlobDeleteOutbox IN
    # this transaction instead and delete the blobs AFTER the commit (commit-then-delete); a blob
    # whose delete fails is simply left in the ledger for the next retention sweep.
    _ki_blob_keys: list[str] = []
    for _bk in session.exec(select(KeyImage.blob_key).where(_ki_where)).all():
        if _bk:
            _ki_blob_keys.append(_bk)
            if (
                session.exec(
                    select(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == _bk)
                ).first()
                is None
            ):
                session.add(BlobDeleteOutbox(blob_key=_bk))
    session.exec(sa_delete(KeyImage).where(_ki_where))
    session.flush()
    if key_ids:
        session.exec(sa_delete(StringKey).where(StringKey.id.in_(key_ids)))
        session.flush()
    # 8c. the per-project rows the keys service parks in the GLOBAL Setting table
    #     (key_autounverify:<pid>, key_changelog_floor:<pid>). Not FK-bound, so they
    #     block nothing -- they are cleaned because a rowid-reusing successor project
    #     would otherwise silently inherit this project's auto-unverify override and
    #     pruned-changelog floor.
    from app.services import keys as _keys

    _keys.forget_project_settings(session, project_id)
    from app.services import comment_option as _comment_option

    _comment_option.forget_project(session, project_id)  # #690: translate_comments:<pid>
    # 8d. ATTACH LINKS (feedback item 5) -- the same class of bug as 8b above, and the
    #     reason that comment exists. `projecttmsource` carries TWO FKs to project.id
    #     and `projectglossary` one, with SQLite FK enforcement ON, so without this the
    #     DELETE at step 9 raises IntegrityError AFTER steps 4-8 have already COMMITTED
    #     the documents, the segments and the whole project TM -- leaving the same
    #     unrecoverable husk, retry-proof, that 8b was written about.
    #
    #     BOTH DIRECTIONS. Deleting only `project_id == pid` (this project's own
    #     attachments) is the easy half and the dangerous one to stop at: the rows
    #     where this project is somebody else's SOURCE reference it just as hard, and
    #     that is the leg a hand-written cascade forgets. Removing them degrades the
    #     borrowing project exactly as a manual detach would -- its lookups return to
    #     their own rows -- which is safe precisely because nothing was ever copied.
    #     The cascade must be COMPLETE, not best-effort: SQLite reuses rowids, so an
    #     orphan link here would silently re-point at whichever project takes this id
    #     next, handing a future tenant a live cross-project TM reference nobody made.
    session.exec(sa_delete(ProjectTmSource).where(ProjectTmSource.project_id == project_id))
    session.exec(sa_delete(ProjectTmSource).where(ProjectTmSource.source_project_id == project_id))
    session.exec(sa_delete(ProjectGlossary).where(ProjectGlossary.project_id == project_id))
    session.flush()
    # 8e. LLMEMORY rows scoped to this project (re-review NEW-5) -- also pre-dating the
    #     attach feature, also a real FK, also fatal at step 9. llmemory writes both
    #     tables with the run's project_id, so any project that ever ran with LLMemory
    #     on carried them. UN-SCOPE rather than delete: a memory entry belongs to the
    #     CUSTOMER (customer_id is NOT NULL by design -- the hard partition), and the
    #     project is only the narrower scope it was learned in. Deleting the project
    #     must not destroy what the customer learned, so the rows survive with
    #     project_id NULL, which every read already treats as customer-wide.
    #     MemoryEvent's OTHER nullable FKs are un-scoped the same way, but EARLIER, each
    #     right before the parent it points at is deleted (#63): workflow_run_id at step 2
    #     (before the WorkflowRun delete) and document_id/segment_id inside
    #     retention.delete_document at step 4 (before each Segment/Document delete). They
    #     are NOT auto-cleared by the parent deletes — ondelete=None — so without those
    #     un-scopes the delete raised IntegrityError and rolled back (the project was
    #     undeletable). Only project_id remains to clear here.
    #
    #     THREE tables, not two (gate M2): `MemorySuggestion.project_id` is the third member of
    #     this family and was missed, so a project with any LLMemory suggestion row -- written by
    #     llmemory_suggestions.generate, which the scheduled janitor reaches -- failed at step 9
    #     exactly like the other two once did. It gets the SAME treatment and for the same reason,
    #     with one extra force: a suggestion is a bi-temporal TOMBSTONE that is never hard-deleted
    #     (LAW 2), and its `customer_id` is NOT NULL, so un-scoping is not merely the kinder
    #     option here -- deleting the row would destroy an audit record the customer owns.
    # #441 (owner's decision): LLMemory learned from a deleted project STAYS on a plain delete
    # (un-scoped below), but a privacy ERASURE request also purges the entries, events and
    # suggestions learned from the project first, so nothing of its text outlives the request.
    if privacy_erasure:
        counts.update(retention.erase_project_memory(session, project_id))
    session.exec(
        sa_update(MemoryEntry).where(MemoryEntry.project_id == project_id).values(project_id=None)
    )
    session.exec(
        sa_update(MemoryEvent).where(MemoryEvent.project_id == project_id).values(project_id=None)
    )
    session.exec(
        sa_update(MemorySuggestion)
        .where(MemorySuggestion.project_id == project_id)
        .values(project_id=None)
    )
    session.flush()
    # 8f. ADVANCED WORKFLOWS missions + runs (SPEC-advanced-workflows §2) -- the SAME FK class as
    #     8b/8d/8e, fatal in the same way, and the reason this comment reads like theirs.
    #     `advancedrun.project_id` and `advancedmission.project_id` are real FKs to project.id with
    #     SQLite FK enforcement ON, so WITHOUT this the DELETE at step 9 raises IntegrityError
    #     AFTER steps 4-8 have already COMMITTED the documents, the segments, the keys and the
    #     whole project TM -- the project survives as the same unrecoverable husk, and every retry
    #     fails identically. ONE advanced run was enough to put a project in that state, with or
    #     without quality findings.
    #
    #     Routed through the advanced service's OWN seam (`delete_project_runs`) rather than
    #     hand-written here: the run's children -- QualityFinding on BOTH its `run_id` and
    #     `staged_target_id` legs, then the staged targets, then the transcript -- are that tier's
    #     FK order to own, and duplicating it here would be a second copy to drift. Runs go before
    #     missions (advancedrun.mission_id -> advancedmission.id).
    #
    #     NOT `advanced.delete_run`, deliberately. That is the SURFACE seam: it opens its OWN
    #     Session (a second SQLite writer contending with this open, flushed-but-uncommitted one)
    #     and calls `_require_enabled()`, which would make every Advanced-tier project undeletable
    #     whenever the flag is off -- a feature switch must never revoke referential integrity
    #     (§5.2 seam 4). `delete_project_runs` is session-level, flag-agnostic and non-committing.
    # Reached through the reaper registry (app/extensions.py): the Advanced add-on registers
    # `delete_project_runs` there; with the add-on absent nothing runs and nothing is merged.
    from app import extensions as _extensions

    counts.update(_extensions.run_reapers("project_content", session, project_id))
    session.flush()
    # Core edition, database upgraded from the full edition: the Advanced add-on is absent so no
    # reaper ran, but its rows still hold real FKs to project.id. Reap them in plain SQL, same
    # transaction, children first, reporting the same `advanced_*` keys as the delete preview.
    from app import editions as _editions

    if not _editions.addon_installed("advanced_workflows"):
        from sqlalchemy import or_ as _adv_or

        from app.models import (
            AdvancedMission,
            AdvancedRun,
            AdvancedStagedTarget,
            AdvancedTranscriptEntry,
            QualityFinding,
        )

        _mission_ids = list(
            session.exec(select(AdvancedMission.id).where(AdvancedMission.project_id == project_id))
        )
        _run_conds = [AdvancedRun.project_id == project_id]
        if _mission_ids:
            _run_conds.append(AdvancedRun.mission_id.in_(_mission_ids))
        _adv_run_ids = list(session.exec(select(AdvancedRun.id).where(_adv_or(*_run_conds))))
        _adv_staged = (
            list(
                session.exec(
                    select(AdvancedStagedTarget.id).where(
                        AdvancedStagedTarget.run_id.in_(_adv_run_ids)
                    )
                )
            )
            if _adv_run_ids
            else []
        )
        _qf = 0
        _st = 0
        _te = 0
        if _adv_run_ids:
            _qf = session.exec(
                sa_delete(QualityFinding).where(
                    _adv_or(
                        QualityFinding.run_id.in_(_adv_run_ids),
                        QualityFinding.staged_target_id.in_(_adv_staged or [-1]),
                    )
                )
            ).rowcount
            _st = session.exec(
                sa_delete(AdvancedStagedTarget).where(AdvancedStagedTarget.run_id.in_(_adv_run_ids))
            ).rowcount
            _te = session.exec(
                sa_delete(AdvancedTranscriptEntry).where(
                    AdvancedTranscriptEntry.run_id.in_(_adv_run_ids)
                )
            ).rowcount
            session.exec(sa_delete(AdvancedRun).where(AdvancedRun.id.in_(_adv_run_ids)))
        _ms = session.exec(
            sa_delete(AdvancedMission).where(AdvancedMission.project_id == project_id)
        ).rowcount
        session.flush()
        counts.update(
            {
                "advanced_runs": len(_adv_run_ids),
                "advanced_missions": _ms,
                "advanced_staged_targets": _st,
                "advanced_transcript_entries": _te,
                "advanced_quality_findings": _qf,
            }
        )
    # 8g. PER-PROJECT SPEND CAP (gate M1) -- the last known member of the 8b/8d/8e/8f family.
    #     `projectbudget.project_id` is a NOT NULL FK to project.id (and UNIQUE, so there is at
    #     most one row), writable from all three surfaces (web/routing.py, /api api_cli.py,
    #     cmd_admin.py), and `ProjectBudget` appeared NOWHERE in this module -- so any project that
    #     had ever been given a monthly USD limit raised IntegrityError at step 9 and left the same
    #     retry-proof husk, with the preview promising a clean delete moments earlier.
    #     DELETED, not un-scoped: unlike the LLMemory family this is pure per-project
    #     configuration, it belongs to nothing else, project_id cannot be NULL, and nothing in the
    #     schema references projectbudget.id -- so there is no child to order against and nothing
    #     is lost by removing it. Leaving an orphan would be actively unsafe: SQLite reuses rowids,
    #     so a stale cap would silently re-attach to whichever project takes this id next.
    session.exec(sa_delete(ProjectBudget).where(ProjectBudget.project_id == project_id))
    # 8h. PROJECT AUTOMATION: the project's content-source binding and the claims it holds on
    #     external items. Both carry a NOT NULL FK to project.id. A bound connection still blocks
    #     the delete (Connection.project_id), so this is defence in depth; pure configuration.
    session.exec(sa_delete(ProjectAutomation).where(ProjectAutomation.project_id == project_id))
    session.exec(sa_delete(ProjectBindingClaim).where(ProjectBindingClaim.project_id == project_id))
    session.flush()
    # 8h. CUSTOM FIELD VALUES (SPEC-project-automation part C): per-project values with a
    #     NOT NULL FK to project.id, deleted with the project (the definition stays).
    from app.services import custom_fields as _cf

    _cf.delete_for_project(session, project_id)
    session.flush()
    # `counts` IS `preview["cascade"]` (bound above, not copied), so `project_budgets` and
    # `memory_suggestions` are already carried into the audit detail by the preview itself --
    # only step 8f, which re-reports from the advanced service, actually writes new values.
    # Recompute the total anyway so `counts` can never contradict itself.
    counts["total"] = sum(
        v
        for k, v in counts.items()
        if k
        not in (
            "total",
            "file_pending",
            "file_removed",
            "lab_datasets_erased",
            "lab_datasets_preserved",
        )
    )
    # 9. finally the project row. Every FK referrer THIS cascade knows about is gone (steps 1-8g)
    #    and the blocker pre-check guaranteed no Connection/RoutingRule remains. Deliberately NOT
    #    "every FK referrer is gone": that claim was already false when it was written (gate M1/M2
    #    found two live counter-examples in this very function), and it is the sentence that makes
    #    the next person skip the audit. Any NEW table carrying a project.id FK must be added
    #    above, or it lands here as an IntegrityError on a project whose children are already
    #    committed away -- the husk this whole comment block exists to describe.
    # files-03: a project's content is not only its documents (step 4 purges exports per
    # document). A documentless project still carries TM, glossary entries, keys and
    # settings that a staged data export holds, so invalidate every live export covering the
    # PROJECT itself in this same final transaction, then remove the staged bytes after it.
    from app.services import gdpr as _gdpr

    _proj_org = getattr(session.get(Project, project_id), "org_id", None)
    pending = _gdpr.mark_export_artifacts_for_project(
        session, project_id, org_id=_proj_org, customer_id=cust_id
    )
    session.delete(session.get(Project, project_id))
    session.commit()
    _gdpr.finish_export_artifact_purge(pending, actor=actor or "system")
    # #543: commit-then-delete for the KeyImage blobs staged in the outbox above.
    _store = blobstore.get()
    for _bk in _ki_blob_keys:
        _status = retention._delete_blob_key(_store, _bk)
        if _status == blobstore.DeleteStatus.FAILED:
            counts["file_pending"] += 1
            continue
        if _status == blobstore.DeleteStatus.REMOVED:
            counts["file_removed"] += 1
        try:
            session.exec(sa_delete(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == _bk))
            session.commit()
        except Exception:
            session.rollback()
    for _bk in _tpl_blob_keys:
        _status = retention._delete_blob_key(_store, _bk)
        if _status == blobstore.DeleteStatus.REMOVED:
            counts["file_removed"] += 1
        elif _status == blobstore.DeleteStatus.FAILED:
            counts["file_pending"] += 1
            try:
                session.add(BlobDeleteOutbox(blob_key=_bk))
                session.commit()
            except Exception:
                session.rollback()
    audit.log(
        actor=actor or "system",
        action="project.delete",
        target=f"project {project_id} ({name})",
        detail="cascade " + ", ".join(f"{k}={v}" for k, v in counts.items()),
        ip=ip,
        customer_id=cust_id,
    )
    if counts["lab_datasets_preserved"]:
        counts["warning"] = (
            f"{counts['lab_datasets_preserved']} Lab snapshot(s) still hold this project's "
            "document text."
        )
    return counts


def remove_target_locale(
    session: Session,
    project_id: int,
    locale: str,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
) -> dict | None:
    """Remove a target locale from a project — the inverse of add_target_locale.

    A project with a content source (a ProjectAutomation) does not delete: the language is
    STOPPED (TargetLocale.stopped_at), see the stop branch below; the text that follows
    describes the ordinary removal of every other project.

    DELETES (locale-scoped): the TargetLocale row(s) for `locale`, every Segment for
    that locale across the project's documents (with their editor state — target text,
    tte, qa flags — which live on the segment row), the LQAResult rows tied to those
    segments, and any per-pair provider override for that target.

    PRESERVES (durable / history — the TMS-industry default): the project's TM
    (SegmentTM / FullTextTM) and glossary rows for the pair — durable assets that must
    survive a later re-add of the locale, exactly as re-adding a locale never wipes
    them. (WorkflowRun / StepRun rows for a project's OTHER locales sharing the same
    documents are untouched too, but see the PERMANENCE note below — a locale that has
    a run of its OWN can no longer reach this deletion path at all.)

    FAILS CLOSED on two blockers (raises LocaleRemovalBlocked, deletes NOTHING):
      - LAST-LOCALE: a project must keep >= 1 target locale.
      - PERMANENCE: a locale that already has translated content
        (see translated_target_locales — a Segment with target text, or its OWN
        WorkflowRun) is never removable, only one that hasn't been translated yet.
        This makes the old "run history survives a removal" case for THIS locale
        moot — a locale with a run of its own can never reach the delete below.
    Both blockers are computed ONCE by remove_locale_preview, re-derived fresh here
    right before any write, so a locale that became permanent between an earlier
    preview and this call is still refused (no stale-preview race). Org-guarded (a
    cross-org id resolves to None). Returns the preview `deletes` counts, None when
    the project doesn't exist / is cross-org / doesn't have the locale."""
    from sqlalchemy import delete as sa_delete

    from app.models import LQAResult, QueryPost, QueryThread, SegmentPostEdit
    from app.services import audit

    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    preview = remove_locale_preview(session, project_id, locale)
    if preview is None:
        return None
    if preview["blocked"]:
        raise LocaleRemovalBlocked(project_id, preview["locale"], preview["blocked_reason"])
    want = languages_catalog.normalize(locale)
    shown = preview["locale"]

    if preview.get("mode") == "stop":
        # Connected project (2026-10-13, decision 4): STOP the language, delete nothing.
        # The row stays with a stop time; its segments, editor state, TM, glossary and run
        # history stay readable and exportable. Runs stop pulling, translating and writing it
        # from the next run on; its claims are released; nothing is written to the external
        # system for it.
        from datetime import datetime as _dt

        now = _dt.utcnow()
        for tl in _project_locale_rows(session, project_id, locale):
            if tl.stopped_at is None:
                tl.stopped_at = now
                session.add(tl)
        session.commit()
        audit.log(
            actor=actor or "system",
            action="project.remove_locale",
            target=f"project {project_id} {shown}",
            detail="stopped at the next run (connected project); nothing deleted: "
            "segments, TM, glossary and run history kept",
            ip=ip,
            customer_id=project.customer_id,
        )
        try:
            from app.services import project_automation

            project_automation.locale_stopped(project_id, shown, actor=actor)
        except Exception:
            import logging

            logging.getLogger("loc_tms.content").exception(
                "Project automation bookkeeping failed for a stopped language on project %s",
                project_id,
            )
        return preview["deletes"]

    doc_ids = list(session.exec(select(Document.id).where(Document.project_id == project_id)).all())
    seg_ids: list[int] = []
    doomed: list = []
    if doc_ids:
        for sg in session.exec(select(Segment).where(Segment.document_id.in_(doc_ids))).all():
            if languages_catalog.normalize(sg.target_locale) == want:
                seg_ids.append(sg.id)
                doomed.append(sg)
    # 0. KEY VALUE TOMBSTONES (SPEC-key-based-projects 2.3, review A3). The bulk
    #    sa_delete below is invisible to the key change feed's ORM listener, and unlike
    #    the project/document deletes this one leaves the PROJECT ALIVE: a since-cursor
    #    consumer would keep every value row of a locale that no longer exists, forever,
    #    with no entry ever telling it otherwise. So the deletions are announced
    #    explicitly, inside this same transaction, BEFORE the rows go. A document
    #    project appends nothing here (no segment carries a key_id).
    if doomed:
        from app.services import keys as _keys

        # ATTRIBUTED (review B5): `log_bulk_value_deletions` reads
        # `keys.current_actor()`, which is "" on a FastAPI worker-pool thread -- so
        # every "this locale's values are gone" tombstone named nobody, on the one
        # event a delta consumer can never recover from by re-reading. This seam
        # already knows the actor; it just was not handing it over.
        _keys.log_bulk_value_deletions(session, doomed, actor=actor)
    # 1. Query threads + posts for this locale (§3.5), child before parent, BEFORE the
    #    Segment delete below (a thread's segment_id FK would otherwise dangle). This is
    #    an INDEPENDENT site — it does its OWN sa_delete(Segment), NOT
    #    retention.delete_document — so it must reap threads itself (N11). Runs even when
    #    seg_ids is empty: a document/project-wide thread stamped with this locale (no
    #    segment) is reaped by the target_locale arm of the shared helper.
    q_thread_ids = _locale_query_thread_ids(session, project_id, seg_ids, want)
    if q_thread_ids:
        session.exec(sa_delete(QueryPost).where(QueryPost.thread_id.in_(q_thread_ids)))
        session.flush()
        session.exec(sa_delete(QueryThread).where(QueryThread.id.in_(q_thread_ids)))
        session.flush()
    # 1c. The ADVANCED tier's own content for these segments (review gate round 1, M2). This is
    #     an INDEPENDENT sa_delete(Segment) site -- it does NOT funnel through
    #     retention.delete_document -- so it must reap the Advanced tier itself, or a run-parented
    #     finding / staged target / transcript entry / roster preview carrying the removed locale's
    #     source+target text SURVIVES (the staged segment_id is a SOFT reference, so the delete
    #     below would not raise and simply leave the content). Same seam, same session, per-UNIT:
    #     the run + mission rows are kept, only this locale's segments' content goes. Placed BEFORE
    #     the Segment delete so the soft refs are NULLed while the rowids are still live (L1). The
    #     project is passed explicitly (this verb has no single doc_id) so the roster strip is
    #     scoped to it. Lazy import (the HARD RULE + the retention idiom).
    _adv_removed = {}
    if seg_ids:
        from app import extensions as _extensions

        _adv_removed = _extensions.run_reapers(
            "document_content", session, seg_ids=seg_ids, project_id=project_id
        )
        session.flush()
    # 2. LQA results + post-edit episode rows referencing those segments (FK-clean on
    #    Postgres too), then the segments. This site does its OWN sa_delete(Segment) — it
    #    does NOT go through retention.delete_document — so it is the INDEPENDENT
    #    load-bearing PE child-delete (§3.2.6, N11) and must clear SegmentPostEdit itself.
    if seg_ids:
        session.exec(sa_delete(LQAResult).where(LQAResult.segment_id.in_(seg_ids)))
        session.exec(sa_delete(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(seg_ids)))
        session.flush()
        session.exec(sa_delete(Segment).where(Segment.id.in_(seg_ids)))
        session.flush()
    # 3. per-pair provider override(s) for this target (normalized match).
    for pp in session.exec(
        select(ProjectPairProvider).where(ProjectPairProvider.project_id == project_id)
    ).all():
        if languages_catalog.normalize(pp.target_locale) == want:
            session.delete(pp)
    # 4. the TargetLocale row(s) themselves (TM / glossary / runs deliberately kept).
    for tl in _project_locale_rows(session, project_id, locale):
        session.delete(tl)
    # M2: fold the Advanced erasure counts into the returned deltas + the audit detail, so the
    # blast radius this verb reports is TRUE (the Advanced content this locale's segments produced
    # is severed, even though the runs themselves are preserved with TM+glossary).
    _adv_nonzero = {k: v for k, v in (_adv_removed or {}).items() if v}
    preview["deletes"].update({f"advanced_{k}": v for k, v in _adv_nonzero.items()})
    session.commit()
    audit.log(
        actor=actor or "system",
        action="project.remove_locale",
        target=f"project {project_id} {shown}",
        detail="deleted "
        + ", ".join(f"{k}={v}" for k, v in preview["deletes"].items())
        + "; preserved TM+glossary+runs (Advanced run rows kept, their content severed)",
        ip=ip,
        customer_id=project.customer_id,
    )
    return preview["deletes"]
