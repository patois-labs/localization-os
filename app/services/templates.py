"""Project templates (P7, item 9) — save a project's setup as a reusable,
optionally customer-scoped snapshot and instantiate new projects from it.

Vendor precedent: Phrase lets any project be
saved as a template that pre-populates languages/workflow/providers; XTM scopes
templates per customer. Localization OS templates capture: source locale, target
locales, default provider (or "engine:<id>"), default workflow, segmentation
profile. A template is a ONE-TIME snapshot (no versioning/propagation — plan §g).

Isolation (P4 bar): a CUSTOMER-scoped template is offered/applicable only for
that customer; list_templates(customer_id) returns global + own, never a foreign
customer's. apply_template re-validates every captured reference at apply time —
a dangling workflow/segmentation id, or one that is customer-scoped elsewhere,
is SKIPPED with a warning rather than failing the instantiation (plan §h).

Documented in: docs/architecture/project-setup-and-templates.md
"""

from sqlmodel import Session, select

from app.db import engine
from app.models import (
    Customer,
    Project,
    ProjectTemplate,
    SegmentationProfile,
    TargetLocale,
    Workflow,
)
from app.services import ids


class TemplateError(ValueError):
    """A template save/apply was rejected (bad name, foreign scope, missing
    project/template). Human-readable message for the UI/CLI."""


def _org_visible(tpl, *, actor, org_id) -> bool:
    """Whether `tpl` is visible to this caller under the org scope — the single
    cross-org enumeration guard shared by apply/prefill/delete/rename (each turns a
    False into its own not-found shape: a TemplateError or a bare False, never
    enumerable). A web User (actor set) is bounded by access.same_org (a no-op True
    while the multi-org gate is off); the CLI passes its resolved --org (org_id).
    access is imported lazily to avoid an import cycle at module load."""
    from app.services import access

    row_org = getattr(tpl, "org_id", None)
    if actor is not None:
        return access.same_org(actor, row_org)
    return org_id is None or row_org == org_id


def _revalidate_snapshot_locales(tpl, warnings: list[str]):
    """Re-validate a template SNAPSHOT's target + source locales against the CURRENT
    catalog (plan §h / P7 grandfathered-locale skip+warn): drop any target no longer
    in the catalog and fall an unknown source back to en-US, appending a human
    warning for each drop/fallback to `warnings`. Returns
    (valid_targets, invalid_targets, src_locale). Callers decide what an empty
    valid_targets means — apply_template raises, prefill_draft warns. (Distinct from
    create_template_from_fields, which validates user-typed params with its own
    wording, not a stored snapshot.)"""
    from app.services import languages_catalog

    valid_targets, invalid_targets = languages_catalog.validate_codes(tpl.target_locales or [])
    for code in invalid_targets:
        warnings.append(f"target locale {code!r} is no longer in the language catalog — dropped.")
    src_locale = tpl.source_locale or "en-US"
    src_valid, src_invalid = languages_catalog.validate_codes([src_locale])
    if src_invalid:
        warnings.append(
            f"source locale {src_locale!r} is no longer in the language catalog — "
            "falling back to en-US."
        )
        src_locale = "en-US"
    else:
        src_locale = src_valid[0]
    return valid_targets, invalid_targets, src_locale


def get_template(tid: int, org_id=None) -> ProjectTemplate | None:
    """A template by id. `org_id`, when given (multi-org scoped caller), treats
    a foreign org's template as not-found (None); None (single-tenant / gate
    off / unscoped operator) returns any template — byte-identical to today."""
    with Session(engine) as s:
        tpl = s.get(ProjectTemplate, tid)
        if tpl is None:
            return None
        if org_id is not None and getattr(tpl, "org_id", None) != org_id:
            return None
        return tpl


def get_by_name(name: str) -> ProjectTemplate | None:
    with Session(engine) as s:
        return s.exec(select(ProjectTemplate).where(ProjectTemplate.name == name)).first()


def list_templates(customer_id=None, org_id=None) -> list[ProjectTemplate]:
    """Templates offered in a given context: GLOBAL ones plus — when a customer
    is given — that customer's own. A foreign customer's template is never
    returned (the P4 isolation rule). org_id scopes under the multi-org gate
    (None = single-tenant = no filter)."""
    with Session(engine) as s:
        q = select(ProjectTemplate).order_by(ProjectTemplate.name)
        if org_id is not None:
            q = q.where(ProjectTemplate.org_id == org_id)
        rows = list(s.exec(q).all())
    cid = _coerce_id(customer_id)
    return [t for t in rows if t.customer_id is None or t.customer_id == cid]


def all_templates(org_id=None) -> list[ProjectTemplate]:
    """EVERY template in the install (global + every customer's) — for an
    INTERNAL-STAFF caller only (e.g. the projects-index card, where customer-
    scoped rows render labeled). NOT admin-gated by this function itself; the
    caller is responsible for never handing these rows to a customer-bound
    account (R2-3) — use list_templates(customer_id=...) for any caller that
    might be customer-bound. Scoped surfaces (customer 360, apply validation)
    use list_templates instead."""
    with Session(engine) as s:
        q = select(ProjectTemplate).order_by(ProjectTemplate.name)
        if org_id is not None:
            q = q.where(ProjectTemplate.org_id == org_id)
        return list(s.exec(q).all())


def global_templates() -> list[ProjectTemplate]:
    """Every GLOBAL template (org_id NULL) across the WHOLE install, unscoped by
    org or customer — the back-office's own cross-org read authority (Day-2 item
    #6), mirroring how `_org_health_rows`/`_org_inspect` in app/backoffice/app.py
    do their own unconfined reads. NOT for the customer app or /api — those stay
    on list_templates/all_templates(org_id=...)."""
    with Session(engine) as s:
        return list(
            s.exec(
                select(ProjectTemplate)
                .where(ProjectTemplate.org_id.is_(None))
                .order_by(ProjectTemplate.name)
            ).all()
        )


def save_as_template(
    session: Session,
    project_id: int,
    name: str,
    *,
    scope: str = "global",
    description: str = "",
    actor: str = "",
    org_id=None,
    bound_customer_id=None,
) -> ProjectTemplate:
    """Snapshot a project's setup as a template. `scope` is "global" or
    "customer" (= the project's own customer; a project without a customer can
    only save globally). Captures source locale, target locales, default
    provider, default workflow and segmentation profile as they are RIGHT NOW.

    `bound_customer_id` (#488 item 5): the CALLER's own customer binding when the caller is
    a customer-bound account (None = staff / trusted CLI, unchanged). A bound caller never
    publishes a GLOBAL template every customer sees: its scope is forced to "customer" (its
    own), and a project that is not its own customer's reads as not found. Same rule as
    project_template_from_draft, so the web, /api and draft doors agree."""
    name = (name or "").strip()
    if not name:
        raise TemplateError("template name is required")
    if get_by_name(name) is not None:
        raise TemplateError(f"a template named {name!r} already exists")
    proj = session.get(Project, project_id)
    if proj is None:
        raise TemplateError(f"no such project: {project_id}")
    bound = None
    if bound_customer_id is not None and str(bound_customer_id).strip().isdigit():
        bound = int(bound_customer_id)
    if bound is not None:
        if proj.customer_id != bound:
            raise TemplateError(f"no such project: {project_id}")
        scope = "customer"
    cust_id = None
    if (scope or "global").lower() == "customer":
        if not proj.customer_id:
            raise TemplateError("this project has no customer — save the template as global.")
        cust_id = proj.customer_id
    locales = [
        tl.locale
        for tl in session.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_(None)
            )
        ).all()
        if tl.locale and tl.locale != "—"
    ]
    tpl = ProjectTemplate(
        name=name,
        customer_id=cust_id,
        source_locale=proj.source_locale or "en-US",
        target_locales=locales,
        default_provider=proj.default_provider or "claude",
        default_workflow_id=proj.default_workflow_id,
        segmentation_profile_id=proj.segmentation_profile_id,
        description=(description or "").strip(),
        created_by=actor,
        org_id=org_id if org_id is not None else proj.org_id,
    )
    session.add(tpl)
    session.commit()
    session.refresh(tpl)
    return tpl


def apply_template(
    session: Session,
    template_id: int,
    project_name: str,
    *,
    customer_id=None,
    created_by: str = "",
    creator=None,
    actor=None,
    org_id=None,
    bound_customer_id=None,
    require_customer=False,
    content_type_scope=None,
    intake=None,
) -> tuple[Project, list[str]]:
    """Create a project from a template. Returns (project, warnings).

    `intake="connected"` makes the new project a connected project (empty, Documents shape). A
    template never binds a source and never schedules anything: the person
    still picks the connection and what to pull in the project's setup menu.

    `customer_id`: None = the template's own customer (global templates create
    customer-less projects); an explicit id overrides — but a CUSTOMER-scoped
    template can never be applied to a DIFFERENT customer than its own via this
    override (isolation).
    Every captured reference is re-validated: a dangling/foreign workflow or
    segmentation profile is skipped with a warning, never a crash (plan §h).

    `actor`/`org_id`: the same org-scope seam as delete_template — a template in
    another org must look not-found (TemplateError, never enumerable), never
    silently applied. `actor` is the requesting web User (uses access.same_org);
    `org_id` is the CLI's resolved --org scope (None = single-tenant/unscoped,
    byte-identical to today).

    `bound_customer_id` (R2-3 hostile-review fix): the CALLER's own customer
    binding, when the caller is a customer-bound account (None = unbound /
    internal staff / trusted operator — unchanged behavior). When given, a
    template scoped to a DIFFERENT customer (tpl.customer_id not None and !=
    bound_customer_id) is refused with the same not-found-style TemplateError
    used elsewhere in this module — never silently applied. This closes the
    write-leak where a bound user could apply a foreign customer's template and
    have the new project inherit THAT template's customer_id, planting a real
    project inside a tenant they may not touch. A global template (customer_id
    None) is always fine for a bound caller; the resulting project still lands
    in the bound caller's own customer (see below).

    Target locales are re-validated against the CURRENT catalog
    (languages_catalog.validate_codes): a template snapshotted from a legacy/
    connector project may carry non-catalog codes (e.g. "iw", "sr-Latn") — those
    are DROPPED with a warning per code rather than failing the whole apply (the
    P7 skip+warn contract). An invalid snapshot source_locale similarly warns and
    falls back to "en-US". If EVERY target locale is invalid, the apply fails
    cleanly with a TemplateError (never a locale-less project)."""
    from app.services import content
    from app.services import segmentation_profiles as segprof

    tpl = session.get(ProjectTemplate, template_id)
    if tpl is None:
        raise TemplateError(f"no such template: {template_id}")
    # Org scope: a cross-org template id must behave as not-found (never
    # enumerable) — mirrors delete_template's seam exactly.
    if not _org_visible(tpl, actor=actor, org_id=org_id):
        raise TemplateError(f"no such template: {template_id}")
    # R2-3: customer-bound caller guard. A bound account may only apply a
    # GLOBAL template or its OWN customer's — a foreign customer-scoped
    # template must look not-found, never silently applied (the write-leak the
    # hostile review caught). Checked BEFORE the customer_id override logic
    # below so a bound caller can't route around it via an explicit override.
    bound_cid = _coerce_id(bound_customer_id)
    if bound_cid is not None and tpl.customer_id is not None and tpl.customer_id != bound_cid:
        raise TemplateError(f"no such template: {template_id}")
    cid = _coerce_id(customer_id)
    if cid is None:
        cid = tpl.customer_id
    elif tpl.customer_id is not None and cid != tpl.customer_id:
        raise TemplateError(
            f"template {tpl.id} is scoped to another customer — apply it there "
            "or clone the setup into a global template."
        )
    # Belt-and-suspenders (adversarial review): a bound caller's RESOLVED customer must
    # be their own even for a GLOBAL template applied with an explicit customer_id=
    # override. The 226-228 guard only covers a CUSTOMER-scoped template's own id; a
    # global template (tpl.customer_id None) let a bound caller's foreign override sail
    # through to project_create (plant-and-forget into another tenant). Not-found style,
    # so a foreign customer stays non-enumerable exactly like the guard above.
    if bound_cid is not None and cid is not None and cid != bound_cid:
        raise TemplateError(f"no such template: {template_id}")
    # A bound caller applying a GLOBAL template must have the new project land
    # in THEIR OWN customer, not customer-less/global — else a bound user could
    # create an internal/orphan project outside any customer's tenant. An
    # explicit customer_id= override already had to match bound_cid (or be
    # None) to reach here at all, since cid is either tpl.customer_id (None for
    # a global template) or the validated override.
    if bound_cid is not None and cid is None:
        cid = bound_cid
    warnings: list[str] = []
    # Grandfathered-locale skip+warn (plan §h / P7 contract): partition the
    # snapshot's targets against the CURRENT catalog rather than handing them
    # straight to project_create (which raises on the first unknown code — a
    # template saved from a legacy/connector project could never be applied).
    from app.services import connected_projects as _cp

    valid_targets, invalid_targets, src_locale = _revalidate_snapshot_locales(tpl, warnings)
    if not valid_targets:
        raise TemplateError(
            f"template {tpl.id}: no valid target locale remains "
            f"({', '.join(invalid_targets) or 'none captured'}) — cannot apply."
        )
    try:
        proj = content.project_create(
            session,
            project_name,
            source_locale=src_locale,
            target_locales=", ".join(valid_targets),
            customer_id=str(cid) if cid else "",
            default_provider=tpl.default_provider or "claude",
            created_by=created_by,
            creator=creator,
            # Thread the caller's own binding into the create seam so project_create's
            # OWN ForeignCustomerError guard (R2-3 Finding E) is a second wall behind the
            # resolved-cid check above — the WEB /projects/from-template sibling, which
            # passes bound_customer_id, is protected without relying on the route.
            bound_customer_id=bound_cid,
            # Review (#554): forward the caller's org so a principal that carries an org but
            # no User (an /api token) stamps it and is confined to it, like project create.
            org_id=org_id,
            # 2026-07-14: the from-template route is a user-facing create seam —
            # it passes require_customer=True so applying a GLOBAL template (no captured
            # customer) without a chosen customer fails cleanly (MissingCustomerError,
            # re-wrapped below) instead of creating a customer-less project. Internal /
            # test callers leave it False (unchanged).
            require_customer=require_customer,
            # #73 COUPLING (gate catch): the applied project inherits the customer's
            # default content type, which project_create validates through the now
            # fail-closed content_types.attachable. `creator` is None on the CLI
            # --operator door, so thread the DELIBERATE resolution scope (access.UNSCOPED
            # for --operator; the concrete id for --org) the same way `project create`
            # does. Web/API omit it -> project_create falls back to the creator org, then
            # to its own org_id param: byte-identical.
            content_type_scope=content_type_scope,
            intake=intake,
        )
    except content.MissingCustomerError as exc:
        # A global template applied with no customer chosen — surface as a TemplateError
        # so the web from-template route + the CLI `template apply` (which only catch
        # TemplateError) show a clean message directing the user to pick a customer.
        raise TemplateError(
            "a customer is required — pick one when applying this template "
            "(a global template carries no customer)."
        ) from exc
    except content.ForeignCustomerError as exc:
        # A bound caller reached project_create with a foreign customer (defense in
        # depth behind the resolved-cid guard above): not-found style, never enumerable.
        raise TemplateError(f"no such template: {template_id}") from exc
    except content.InvalidLocaleError as exc:
        raise TemplateError(f"template locale no longer valid: {exc}") from exc
    except (content.ArchivedCustomerError, content.UnknownCustomerError) as exc:
        # SPEC-customer-archive: the template's customer_id may have been archived
        # AFTER the template was created/saved (archive doesn't touch existing
        # templates). project_create refuses the archived customer at its own
        # seam; re-wrap as a TemplateError so the CLI (`template apply`, which
        # only catches TemplateError) and the web from-template route get the
        # same clean error + usage exit instead of a raw traceback.
        raise TemplateError(str(exc)) from exc
    except _cp.IntakeError as exc:
        raise TemplateError(str(exc)) from exc
    except content.InvalidContentTypeError as exc:
        # #73 gate catch: now that content_types.attachable fails closed, a principal with
        # NO resolvable org scope (an UNBOUND connector token -> BLOCKED_ORG_ID) genuinely
        # fails the inherited-content-type validation here. Unwrapped it escaped as a 500
        # on /api/templates/{id}/apply and a raw traceback on `template apply` (both catch
        # only TemplateError); re-wrap it like every sibling refusal above.
        raise TemplateError(str(exc)) from exc
    if proj is None:
        raise TemplateError("project name is blank.")
    # Default workflow: display/dispatch-preference only — set when the workflow
    # still exists and is global or this customer's (the P4 bar); else skip+warn.
    if tpl.default_workflow_id:
        wf = session.get(Workflow, tpl.default_workflow_id)
        if wf is None:
            warnings.append(
                f"default workflow #{tpl.default_workflow_id} no longer exists — skipped."
            )
        # customer_id alone, never scope (see customers.customer_workflows): a row can be
        # scope=GLOBAL and still be owned by another customer.
        elif wf.customer_id is not None and wf.customer_id != proj.customer_id:
            warnings.append(f"default workflow #{wf.id} belongs to another customer — skipped.")
        else:
            proj.default_workflow_id = wf.id
            session.add(proj)
            session.commit()
    # Segmentation profile: validated by the P6 service; dangling/foreign → warn.
    if tpl.segmentation_profile_id:
        if session.get(SegmentationProfile, tpl.segmentation_profile_id) is None:
            warnings.append(
                f"segmentation profile #{tpl.segmentation_profile_id} no longer "
                "exists — project inherits the chain."
            )
        else:
            try:
                segprof.assign_to_project(session, proj.id, str(tpl.segmentation_profile_id))
            except segprof.SegProfileError as exc:
                warnings.append(f"segmentation profile skipped: {exc}")
    session.refresh(proj)
    return proj, warnings


def prefill_draft(
    session: Session, template_id: int, *, actor=None, org_id=None, bound_customer_id=None
) -> dict:
    """Read-only "start from this template" prefill for the new-project dialog
    (the feature: pick a template at the TOP of the popout, auto-fill the form
    below, human reviews/tweaks/submits normally). A sibling of apply_template
    that creates NOTHING -- it returns a draft-shaped dict matching the SAME
    contract the promptable-setup draft uses (SPEC-promptable-setup A2), so the
    dialog's existing applyDraft()/renderWarnings() JS can fill the form without a
    parallel fill path.

    Visibility mirrors apply_template's org/bound-customer scope check EXACTLY
    (the same not-found-style TemplateError, never enumerable): a template in
    another org, or -- for a customer-bound caller -- scoped to a DIFFERENT
    customer, is refused. This is also byte-identical to the set of templates
    projects_index already lists for the same caller (list_templates/
    all_templates), so the dialog can never fetch a template it wouldn't already
    see offered on /projects.

    Re-validates target/source locales against the CURRENT catalog exactly like
    apply_template (drop+warn / fallback+warn). Two references the dialog's base
    project-creation FORM cannot represent are downgraded to a WARNING instead of
    a value, rather than silently losing them:
      - `default_workflow_id`: Project.default_workflow_id is currently applied
        ONLY by apply_template (see services/workflows.workflow_reference_count's
        "project_defaults -- FK-less; UI pre-select only"); the plain create form
        has no field for it. Warn + point at the existing full-fidelity paths
        ("Create from template" on /projects, or `template apply` in the CLI)
        rather than bolting a new create-time field onto every surface (web +
        CLI + /api) for one inert metadata column.
      - `segmentation_profile_id`: when it is CUSTOMER-scoped, it can't appear in
        this dialog's segmentation picker, which (like the base create form
        itself, projects.html's own comment: "only GLOBAL profiles are offered at
        create time") only offers global profiles here -- the customer isn't
        settled until the project exists. Warn + point at the project page's own
        "Edit segmentation" form (POST /projects/{id}/segmentation), which DOES
        offer the customer's own profiles, as the fixup path. A GLOBAL profile
        (or a dangling one) behaves exactly like apply_template: included when
        it still exists, skip+warn when it doesn't.

    Returns {"draft": {...}, "warnings": [str, ...]}. Raises TemplateError when
    the template does not exist or is not visible (the route maps this to a
    plain 404, matching the not-found-style contract used elsewhere in this
    module)."""
    tpl = session.get(ProjectTemplate, template_id)
    if tpl is None:
        raise TemplateError(f"no such template: {template_id}")
    if not _org_visible(tpl, actor=actor, org_id=org_id):
        raise TemplateError(f"no such template: {template_id}")
    bound_cid = _coerce_id(bound_customer_id)
    if bound_cid is not None and tpl.customer_id is not None and tpl.customer_id != bound_cid:
        raise TemplateError(f"no such template: {template_id}")

    warnings: list[str] = []
    valid_targets, _invalid_targets, src_locale = _revalidate_snapshot_locales(tpl, warnings)
    if not valid_targets:
        warnings.append(
            f"template {tpl.id}: no valid target locale remains — pick languages manually."
        )

    draft: dict = {
        "source_locale": src_locale,
        "target_locales": valid_targets,
        "default_provider": tpl.default_provider or "claude",
    }

    # Customer: include only when it still resolves to a live, non-archived
    # customer (the dialog's picker is a PICKER, not a management view -- an
    # archived customer is never offered there, see new_project_ctx). A
    # dangling/archived reference degrades to a warning rather than setting a
    # value the <select> doesn't have, so applyDraft's setField() no-ops cleanly.
    if tpl.customer_id:
        cust = session.get(Customer, tpl.customer_id)
        if cust is None:
            warnings.append(f"customer #{tpl.customer_id} no longer exists — pick one manually.")
        elif getattr(cust, "archived_at", None) is not None:
            warnings.append(f"customer {cust.name!r} is archived — pick a customer manually.")
        else:
            draft["customer_id"] = tpl.customer_id

    # 2026-07-14: a project requires a customer. A GLOBAL template (or one whose
    # captured customer no longer resolves) prefills no customer, so WARN — the dialog's
    # customer <select> is `required` (hard gate at submit) and POST /projects
    # re-enforces it server-side, but the warning makes the "pick a customer" ask
    # explicit on the review screen. Gated on a customer actually EXISTING to pick
    # (same seam as content.project_create) so a zero-customer install isn't nagged.
    from app.services.content import org_has_selectable_customer

    if not draft.get("customer_id") and org_has_selectable_customer(session, org_id):
        warnings.append("Pick a customer — a project requires one before it can be created.")

    # Default workflow: the base create form has no field for it (see docstring) --
    # always a warning, never a value, when the template captured one.
    if tpl.default_workflow_id:
        wf = session.get(Workflow, tpl.default_workflow_id)
        if wf is None:
            warnings.append(f"default workflow #{tpl.default_workflow_id} no longer exists.")
        else:
            warnings.append(
                f"this template's default workflow ({wf.name!r}) isn't applied by "
                'this prefill — use "Create from template" on the Projects page '
                "(or `template apply` in the CLI) for full fidelity."
            )

    # Segmentation profile: included only when GLOBAL (the dialog's picker only
    # offers global profiles, matching the base create form -- see docstring);
    # dangling or customer-scoped degrades to a warning instead of a lost value.
    if tpl.segmentation_profile_id:
        sp = session.get(SegmentationProfile, tpl.segmentation_profile_id)
        if sp is None:
            warnings.append(
                f"segmentation profile #{tpl.segmentation_profile_id} no longer "
                "exists — the project will inherit the chain."
            )
        elif sp.customer_id is not None:
            warnings.append(
                f"segmentation profile {sp.name!r} is customer-scoped — set it from "
                'the project page\'s "Edit segmentation" after creating.'
            )
        else:
            draft["segmentation_profile_id"] = sp.id

    return {"draft": draft, "warnings": warnings}


def delete_template(tid: int, *, actor=None, org_id=None, platform_operator: bool = False) -> bool:
    """Delete a template, enforcing scope at the SEAM so BOTH surfaces are covered
    (#1 — the web route used to gate only can_write, and the CLI bypassed even
    that). Returns True on delete, False when the template does not exist (or is
    not visible to this caller — a foreign-org id must look identical to a missing
    one, never enumerable). Raises TemplateError when the caller is visible-but-
    forbidden (a manage-role denial), so the route/CLI surface a clean message.

      - `actor`: the requesting web User, or None for the trusted in-process CLI
        operator (already org-gated by require_org_scope). A web User must be
        manage-level (manager/admin) to delete a template — a plain translator
        can no longer delete any global/other-customer template.
      - `org_id`: the org to scope the lookup to (the web caller's current org, or
        the CLI's resolved --org scope). A template in another org is not-found.
        None = single-tenant / unscoped (byte-identical to today).
      - `platform_operator` (Day-2 item #6): the EXPLICIT escape hatch for
        deleting a GLOBAL template. Under the multi-org gate a GLOBAL template
        (org_id NULL) is otherwise undeletable by anyone: `access.same_org(actor,
        None)` is False for every org, so it fails closed (deliberately, because
        a regular org admin must never be able to remove a global template that
        other orgs may depend on).

        2026-09-20: the caller used to be the /back-office route, authorized by a
        super-admin behind that mount's own auth wall. Both are gone. The one
        remaining caller is `cli template delete --operator`, authorized by
        require_operator against LOC_TMS_OPERATOR_KEY, which is shell access on
        the host rather than any account. Do NOT add a web caller: nothing on the
        customer surface may lift this. This flag:
          * bypasses the org-scope not-found check ONLY when `tpl.org_id is
            None` (falls straight through to delete);
          * for an ORG-OWNED row (`tpl.org_id is not None`), `platform_operator`
            NEVER deletes — regardless of `actor`/`org_id` (defense in depth —
            even a future caller that forgets the route's own org_id-not-None
            400 guard cannot delete another org's template via this flag). A
            web-style caller (`actor` set) gets the usual not-found `False`
            (never enumerable, same as any other cross-org miss); the trusted
            operator call shape (`actor is None`, matching the ONE real caller
            — `cli template delete --operator`) gets an explicit `TemplateError` instead
            of silently falling through to the trusted-CLI delete path, since
            `actor is None` would otherwise skip both the org check and the
            can_manage check below and delete it anyway. The org's own admins
            keep their existing delete path via the normal actor/org_id/
            can_manage checks (platform_operator=False);
          * does NOT bypass anything when called with `actor` set. The one real
            caller passes `actor=None`, because the authorization that matters
            happened one layer up: today that is shell access to the host plus
            an explicit `--operator`, where it used to be a super-admin holding
            a live back-office session.
        AUDIT (gate R3 MED-2): this service deliberately writes no audit row,
        and the CALLER owns that. On main the caller was the back-office route
        and the row was `backoffice.template_delete`; that route is deleted, so
        `app/cli/cmd_template.py::_delete` now writes a plain `template.delete`
        row for every delete it performs. If you add a second caller, it audits
        too. An unaudited door to an install-wide destructive verb is the thing
        this note exists to prevent.
        Never set by the web `/templates/{id}/delete` route, which keeps today's
        fail-closed behavior for a global template under the gate. The CLI IS the
        caller that sets it (`--operator`), which is a narrowing of what that
        flag reached on main, not a widening: see cmd_template._delete."""
    from app.services import security

    with Session(engine) as s:
        tpl = s.get(ProjectTemplate, tid)
        if tpl is None:
            return False
        row_org = getattr(tpl, "org_id", None)
        if platform_operator and row_org is not None:
            # An org-owned row is NEVER touched by this flag. A web-style caller
            # (actor set) gets the ordinary not-found miss; the trusted
            # actor=None operator shape gets an explicit refusal rather than
            # silently falling through to the unscoped/unguarded delete below.
            if actor is not None:
                return False
            # ASCII only: this string predates the train, but until now its only
            # caller was the back-office HTML route. It is a CLI error path now, so
            # 2026-09-20 is the first time it reaches a terminal (GATE R2 LOW-6).
            raise TemplateError(
                "This template belongs to an organization. The platform "
                "operator may only delete GLOBAL templates."
            )
        elif platform_operator:
            # row_org is None here: the ONLY sanctioned bypass -- a GLOBAL template,
            # deleted by `cli template delete --operator` (the back-office
            # super-admin that used to be the only caller was removed 2026-09-20).
            # Falls straight through to the delete below -- no actor/org_id scope
            # check applies to a row with no org.
            pass
        else:
            # Org scope: a cross-org id must behave as not-found (never
            # enumerable). same_org() is a no-op (True) while the multi-org gate
            # is off, so the explicit org_id path only bites under SaaS. For a
            # web User the actor's own org is the authority; the CLI passes its
            # resolved --org scope.
            if not _org_visible(tpl, actor=actor, org_id=org_id):
                return False
            # Role scope: a web User needs manage rights (matching the tightened
            # route). The in-process CLI operator (actor is None) is trusted — it
            # is already gated by require_org_scope for the SaaS cross-org case.
            if actor is not None and security.auth_required() and not security.can_manage(actor):
                raise TemplateError("Deleting templates requires a manager role.")
        s.delete(tpl)
        s.commit()
        return True


def rename_template(
    session: Session, template_id: int, name: str, *, actor=None, org_id=None
) -> ProjectTemplate:
    """Rename a template. Returns the renamed ProjectTemplate.

    Signature matches the module's neighbors (save_as_template/apply_template take a
    session; the org-scope seam mirrors delete_template/apply_template exactly):
      - `actor`: the requesting web User (uses access.same_org), or None for the
        trusted in-process CLI operator (already org-gated upstream).
      - `org_id`: the CLI's resolved --org scope (None = single-tenant / unscoped).
    A template in another org is not-found (TemplateError, never enumerable), never
    silently renamed — identical to delete_template's scope check.

    Keeps the EXISTING app-level dup-check semantics (CASE-SENSITIVE exact match,
    no new uniqueness rule — the general-rule bar) but scopes it to the template's
    own org: an identically named template in ANOTHER org is neither a conflict nor
    enumerable via the error message (cross-org name oracle, review finding). A
    blank name, a duplicate name, or a missing/foreign template each raise
    TemplateError, so the route/CLI surface a clean message (never a 500)."""
    name = (name or "").strip()
    if not name:
        raise TemplateError("template name is required")
    tpl = session.get(ProjectTemplate, template_id)
    if tpl is None:
        raise TemplateError(f"no such template: {template_id}")
    if not _org_visible(tpl, actor=actor, org_id=org_id):
        raise TemplateError(f"no such template: {template_id}")
    row_org = getattr(tpl, "org_id", None)
    if name != tpl.name:
        dup_q = select(ProjectTemplate).where(ProjectTemplate.name == name)
        if row_org is not None:
            dup_q = dup_q.where(ProjectTemplate.org_id == row_org)
        existing = session.exec(dup_q).first()
        if existing is not None and existing.id != tpl.id:
            raise TemplateError(f"a template named {name!r} already exists")
        tpl.name = name
        session.add(tpl)
        session.commit()
        session.refresh(tpl)
    return tpl


def create_template_from_fields(
    session: Session,
    name: str,
    *,
    source_locale: str = "en-US",
    target_locales=None,
    default_provider: str = "claude",
    customer_id=None,
    scope: str = "global",
    default_workflow_id=None,
    segmentation_profile_id=None,
    description: str = "",
    actor: str = "",
    org_id=None,
    bound_customer_id=None,
) -> tuple[ProjectTemplate, list[str]]:
    """Create a ProjectTemplate DIRECTLY from validated field values -- the
    "save a drafted setup as a template without a project ever existing" path
    (SPEC-promptable-setup A2). A sibling of save_as_template, but the values come
    from a promptable-setup draft rather than a snapshot of a live project.

    Re-validates EVERY captured reference at the SEAM (never trusting the caller --
    the values may arrive from a FORGED POST, not just a server-built draft):
    invalid target locales are DROPPED with a warning, an invalid source falls back
    to en-US with a warning, and a dangling/foreign default workflow or a dangling
    segmentation profile is SKIPPED with a warning -- exactly apply_template's
    skip-and-warn tolerance, so the template is still created (never a crash).

    `customer_id` (HIGH-1/MED-1): a customer-scoped template's customer_id is
    re-resolved against the caller's VISIBLE customers -- it must EXIST, sit in the
    caller's `org_id`, and (for a bound caller) be their OWN `bound_customer_id`. A
    nonexistent id (would FK-crash -> 500) OR a foreign-org id (silent org-isolation
    bypass) is SKIPPED with a warning and the template drops to GLOBAL scope, the
    same skip-and-warn contract the workflow/segmentation refs use. No uncaught
    IntegrityError path remains.

    `default_provider` (LOW): validated against the same offered-provider set the
    promptable-setup draft path uses (built-ins + this org's enabled engines); an
    unoffered value is skipped with a warning and stored as the "claude" default.
    Returns (template, warnings)."""
    from app.models import SegmentationProfile, Workflow
    from app.services import engines, languages_catalog
    from app.services.setup_author import _match_provider

    name = (name or "").strip()
    if not name:
        raise TemplateError("template name is required")
    if get_by_name(name) is not None:
        raise TemplateError(f"a template named {name!r} already exists")

    warnings: list[str] = []

    valid_t, invalid_t = languages_catalog.validate_codes(target_locales or [])
    for code in invalid_t:
        warnings.append(f"target locale {code!r} is not in the language catalog -- dropped.")
    src_valid, src_invalid = languages_catalog.validate_codes([source_locale or "en-US"])
    src = src_valid[0] if src_valid else "en-US"
    if src_invalid:
        warnings.append(
            f"source locale {source_locale!r} is not in the language catalog -- using en-US."
        )

    # A customer-scoped template needs a customer; a global one ignores it.
    cust_id = _coerce_id(customer_id)
    bound_cid = _coerce_id(bound_customer_id)
    if (scope or "global").lower() == "customer":
        if cust_id is None:
            raise TemplateError("a customer-scoped template needs a customer -- save it as global.")
        # HIGH-1/MED-1: re-resolve customer_id against the caller's VISIBLE ring
        # (exists + same org + own bound customer). A nonexistent id would FK-crash
        # (500) and a foreign-org id would silently plant a cross-org customer_id --
        # both drop to GLOBAL with a warning (skip-and-warn), never a crash or a leak.
        cust = session.get(Customer, cust_id)
        visible = (
            cust is not None
            and (org_id is None or getattr(cust, "org_id", None) == org_id)
            and (bound_cid is None or cust.id == bound_cid)
        )
        if not visible:
            warnings.append(
                f"customer #{cust_id} is not one you can save a template for -- "
                "saved as a global template instead."
            )
            tpl_customer = None
        else:
            tpl_customer = cust_id
    else:
        tpl_customer = None

    # Default workflow: skip+warn on a dangling id or a foreign customer-scoped one
    # (mirrors apply_template's re-validation of the captured workflow ref).
    wf_id = None
    if default_workflow_id:
        wf = session.get(Workflow, _coerce_id(default_workflow_id) or -1)
        if wf is None:
            warnings.append(f"default workflow #{default_workflow_id} no longer exists -- skipped.")
        # customer_id alone, never scope (see customers.customer_workflows).
        elif wf.customer_id is not None and wf.customer_id != tpl_customer:
            warnings.append(f"default workflow #{wf.id} belongs to another customer -- skipped.")
        else:
            wf_id = wf.id

    # Segmentation profile: skip+warn on a dangling id.
    seg_id = None
    if segmentation_profile_id:
        sp = session.get(SegmentationProfile, _coerce_id(segmentation_profile_id) or -1)
        if sp is None:
            warnings.append(
                f"segmentation profile #{segmentation_profile_id} no longer exists -- skipped."
            )
        else:
            seg_id = sp.id

    # Default provider (LOW): validate against the SAME offered set the draft path
    # builds (built-ins + this org's ENABLED engines) via _match_provider. An
    # unoffered/forged value (incl. a foreign-org engine:<id>) is skipped+warned and
    # stored as the "claude" default rather than persisted unvalidated.
    from app import config

    offered = list(config.PROVIDERS)
    for prof in engines.list_profiles(org_id=org_id):
        row = engines.engine_row(prof)
        # A quality-judge engine (jev / jev_local) can never be a translate default.
        if row.get("enabled") and engines.role_allows(prof.kind, "translate"):
            offered.append(row["provider_name"])
    prov = _match_provider(default_provider or "claude", offered)
    if prov is None:
        warnings.append(
            f"default provider {default_provider!r} is not an available provider -- "
            "using the claude default."
        )
        prov = "claude"

    tpl = ProjectTemplate(
        name=name,
        customer_id=tpl_customer,
        source_locale=src,
        target_locales=valid_t,
        default_provider=prov,
        default_workflow_id=wf_id,
        segmentation_profile_id=seg_id,
        description=(description or "").strip(),
        created_by=actor,
        org_id=org_id,
    )
    session.add(tpl)
    session.commit()
    session.refresh(tpl)
    return tpl, warnings


def template_row(tpl: ProjectTemplate) -> dict:
    """A safe, echo-able summary for the CLI --json / web."""
    return {
        "id": tpl.id,
        "name": tpl.name,
        "customer_id": tpl.customer_id,
        "scope": "customer" if tpl.customer_id else "global",
        "source_locale": tpl.source_locale,
        "target_locales": list(tpl.target_locales or []),
        "default_provider": tpl.default_provider,
        "default_workflow_id": tpl.default_workflow_id,
        "segmentation_profile_id": tpl.segmentation_profile_id,
        "description": tpl.description,
    }


def _coerce_id(v):
    # App-wide id law: a malformed request-supplied value (Unicode digit / >2**63) is
    # None, never a 500 at int()/the SQLite bind.
    return ids.coerce_id(v)
