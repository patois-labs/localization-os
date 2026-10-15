"""content: project rename + delete (spec-rename-delete)

Documented in: docs/architecture/content-and-projects.md
"""

import logging
import math
import re
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import (
    Customer,
    Document,
    Project,
    ProjectPairProvider,
    ProjectStatus,
    ProjectType,
    Segment,
    TargetLocale,
)
from app.services import access, ids, languages_catalog, locale_seed

DUE_SOON_DAYS = 3


_SQLITE_INT64_MAX = 2**63 - 1


# Re-exported so the web/CLI/API create paths can `except content.PairProviderError`
# alongside the other create-time validation errors (single import surface).
# Due-date "due soon" window (Feature A): a project whose due date is within this
# many days (and not yet past) shows the "Due soon" badge; past = "Overdue".
# SQLite's binder rejects any integer outside a signed 64-bit range with a raw
# OverflowError. A digit-only string coerced to an id (customer_id here) must be
# bounded before it reaches session.get()/a query, else an oversized value (a
# typo, a fuzzer) crashes instead of cleanly resolving to "not found" (mirrors
# app.cli._common.bounded_id / digit_ref_to_id for the CLI surface).
class InvalidLocaleError(ValueError):
    """Raised when a NEW locale code isn't in the BCP-47 catalog (P2, item 5).
    Carries the offending code(s) so the route/CLI can show a clear message.
    Only ever raised on fresh input — existing TargetLocale rows are grandfathered
    (never re-validated)."""

    def __init__(self, invalid: list[str]):
        self.invalid = invalid
        super().__init__(
            "unknown locale code(s): " + ", ".join(invalid) + " (not in the language catalog)."
        )


class ForeignCustomerError(ValueError):
    """R2-3 (Finding E): raised when a customer-BOUND caller's requested
    customer_id resolves to a customer other than their own. Mirrors
    templates.TemplateError's refuse-don't-silently-apply shape — the route/CLI
    surfaces this as a clean error, never a silently-mis-owned project."""

    def __init__(self, customer_id):
        super().__init__(
            f"customer {customer_id} is not your own customer -- cannot create a project there."
        )


def org_has_selectable_customer(session: Session, org_id=None) -> bool:
    """True when the given org has at least one SELECTABLE (active, non-archived)
    customer — the single seam the required-customer enforcement + its advisory
    warnings share (project_create, templates.prefill_draft, setup_author). org_id
    None (single-tenant) considers every active customer. Documented rationale lives on
    MissingCustomerError / project_create's require_customer block: enforce only where a
    customer can actually be chosen, so zero-customer (pristine/test) installs stay
    customer-less + get the assignment banner rather than being blocked."""
    q = select(Customer.id).where(Customer.archived_at.is_(None))
    if org_id is not None:
        q = q.where(Customer.org_id == org_id)
    return session.exec(q).first() is not None


class MissingCustomerError(ValueError):
    """2026-07-14: every USER-FACING project-creation seam now requires a
    customer (there is no "no-customer" bucket). Raised by project_create ONLY when
    the caller opts in via require_customer=True — the web POST /projects route, the
    CLI `project create` / `project author --create`, the /api create, and the
    from-template apply all pass it. Internal/trusted callers and hermetic test
    fixtures call project_create WITHOUT the flag and stay customer-less (the
    enforcement lives at the user-facing seam, NOT in the service internals), so
    existing fixtures don't all break. The surface shows this as a clean 400 /
    EXIT_USAGE, never a 500. EXISTING customer-less projects keep working — they are
    flagged for assignment (banner + badge + `project set-customer`), not blocked."""

    def __init__(self):
        super().__init__("a customer is required to create a project -- pick one and try again.")


class ArchivedCustomerError(ValueError):
    """SPEC-customer-archive: raised when a project create names a customer that is
    ARCHIVED. Archive retires a customer from every "choose a customer" surface, so a
    new project can never be attached to one — enforced at the SERVICE (not just
    omitted from the UI picker) so a forged POST / stray CLI id is refused too. Clean
    error on the route/CLI, never a silently mis-attached project."""

    def __init__(self, customer_id):
        super().__init__(
            f"customer {customer_id} is archived -- unarchive it first, or pick an "
            "active customer, to create a project for it."
        )


class UnknownCustomerError(ValueError):
    """#580: a project create named a customer id that does not exist. Raised BEFORE any row
    is written, so a typo or stale id is a clean refusal (web flash, CLI usage exit, /api 404)
    instead of a foreign-key failure that surfaced as a 500."""

    def __init__(self, customer_id):
        super().__init__(f"customer {customer_id} not found.")


class DuplicateNameError(ValueError):
    """#580: a customer (per org) or project (per customer) with this name already exists.
    Raised ONLY when the caller opts in with reject_duplicate=True, which the user-facing
    create seams (web, /api, CLI) do; hermetic fixtures and internal callers keep creating
    same-named rows. `existing_id` names the row to reuse; the surfaces show it so a retried
    client call can switch to the existing record instead of creating a twin."""

    def __init__(self, kind: str, name: str, existing_id: int):
        self.kind = kind
        self.existing_id = existing_id
        super().__init__(
            f"a {kind} named {name!r} already exists (id {existing_id}); "
            "use it, or pick another name."
        )


class ProjectDeleteBlocked(ValueError):
    """SPEC-rename-delete: a project delete was REFUSED because INDEPENDENT referrers
    still point at it — Connection.project_id and RoutingRule.project_id are BLOCKERS
    (fail closed, never cascade). Carries the live blocker counts so the surface can
    name exactly what to reassign/delete first. Mirrors ContentTypeError's fail-closed
    shape; the route/CLI/api surface it as a clean 400/EXIT_USAGE, never a 500."""

    def __init__(self, project_id, blockers, message=None):
        self.project_id = project_id
        self.blockers = blockers
        super().__init__(
            message
            or (
                "cannot delete project %d: still referenced by %d connection(s) and %d "  # noqa: UP031
                "routing rule(s). Reassign or delete those first."
                % (project_id, blockers["connections"], blockers["routing_rules"])
            )
        )


class DueDateError(ValueError):
    """Feature A: raised when a due-date input can't be parsed. The route/CLI
    surface it as a clean 400 / EXIT_USAGE, never a 500."""

    def __init__(self, value):
        self.value = value
        super().__init__(f"invalid due date {value!r} (expected YYYY-MM-DD).")


class LocaleRemovalBlocked(ValueError):
    """Raised when removing a target locale from a project is REFUSED (fail closed).

    Two blockers today:
      - LAST-LOCALE: a project must keep at least one target locale — a zero-locale
        project has nothing to translate/export and breaks the pair-provider forms +
        the download hero (both iterate `locales`).
      - PERMANENCE: once a locale has ANY translated content —
        a Segment with target text, or a WorkflowRun (a translation attempt whose
        output shape a segment scan can't always show) — its documents are
        permanent. A locale is only removable BEFORE it has been translated.
    The inverse of add_target_locale; the route/CLI/api surface it as a clean 400 /
    EXIT_USAGE, never a 500. Carries `reason` so the surface can name why."""

    def __init__(self, project_id, locale, reason):
        self.project_id = project_id
        self.locale = locale
        self.reason = reason
        super().__init__(reason)


class InvalidContentTypeError(ValueError):
    """SPEC-content-types: raised when a project-create / settings-change names a
    content_type_id that either does not exist OR is not ATTACHABLE to the project's
    customer (a global type or the project's own customer's — never another
    customer's, mirroring ForeignCustomerError's R2-3 confinement). The route/CLI
    surface it as a clean 400 / EXIT_USAGE, never a 500. A non-existent id and a
    foreign-customer id are deliberately indistinguishable (the type simply 'is not
    available for this project')."""

    def __init__(self, content_type_id):
        self.content_type_id = content_type_id
        super().__init__(
            f"content type {content_type_id} is not available for this project "
            "(unknown, or scoped to a different customer)."
        )


class InvalidProjectTypeError(ValueError):
    """SPEC-key-based-projects: raised when a create names a project_type that is not
    one of ProjectType.ALL, or names "key_based" while the key-projects kill switch is
    OFF. The two cases are deliberately indistinguishable at the service (a disabled
    module offers no types), and every surface reports it as a clean 400 / EXIT_USAGE.

    There is no *change*-type error because there is no change-type path: the type is
    IMMUTABLE in v1 (spec 2.1), so nothing writes Project.project_type after create."""

    def __init__(self, project_type):
        self.project_type = project_type
        super().__init__(
            "project type {!r} is not available (expected one of: {}).".format(
                project_type, ", ".join(available_project_types())
            )
        )


def available_project_types() -> list:
    """The project types a create may actually name RIGHT NOW.

    A kill switch that still ENUMERATES what it disables is not a kill switch
    (review A-F9): with `key_projects_enabled` off, the refusal used to answer
    "expected one of: document, key_based" -- advertising a Beta feature to whoever
    probed it, and, worse, telling them the exact string to keep trying. Every other
    surface of the switch (the type selector, the nav affordances, the routes, the
    CLI) makes the feature invisible; this message was the one that did not."""
    from app.services import keys as _keys

    return [t for t in ProjectType.ALL if t != ProjectType.KEY_BASED or _keys.enabled()]


_logger = logging.getLogger("loc_tms")


def parse_due_date(value) -> datetime | None:
    """Coerce a due-date INPUT to the stored representation, or raise DueDateError.

    Accepts:
      - None / "" (or an all-whitespace string)   -> None (no due date / clear);
      - a datetime                                 -> passed through (already stored form);
      - a "YYYY-MM-DD" string                      -> that day's midnight, naive UTC
        (the _utcnow convention — no tzinfo — so it compares cleanly against
        datetime.utcnow() with no naive-vs-aware error).
    Anything else (a garbage string, a bad date like 2026-13-40) raises DueDateError.
    A leading/trailing 'T...' time component is tolerated (only the date part is
    used) so an <input type="date"> or a datetime-local both parse."""
    if value is None:
        return None
    if isinstance(value, datetime):
        # Defensive (adversarial-review FIX 4): every web/CLI/api caller sends a
        # string (always parsed naive below), but a direct Python caller COULD
        # pass an aware datetime straight through. An aware value stored as-is
        # would later crash due_status's naive-vs-aware comparison, so normalize
        # to naive-UTC here — convert to UTC then drop tzinfo. A naive datetime
        # (the normal case) passes through completely unchanged.
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    text = str(value).strip()
    if not text:
        return None
    # Keep only the date portion: an <input type="date"> posts "YYYY-MM-DD", but a
    # datetime-local (or a stray time) posts "YYYY-MM-DDTHH:MM" — take the day and
    # store midnight so the stored value is always a day boundary.
    date_part = text.split("T", 1)[0].strip()
    try:
        d = datetime.strptime(date_part, "%Y-%m-%d")
    except (ValueError, TypeError) as e:
        raise DueDateError(value) from e
    return datetime(d.year, d.month, d.day)  # naive-UTC midnight


def days_until_due(due: datetime, now: datetime | None = None) -> int:
    """The whole-day count from `now` to `due` (Feature A adversarial-review
    FIX 2), used by BOTH due_status's due_soon classification and the email's
    due-line so they always agree on "N days".

    `due` is always a stored midnight and `now` is intra-day, so a plain
    `(due - now).days` FLOORS toward zero: a project due in 2 calendar days but
    less than 48 wall-clock hours away (e.g. due tomorrow-plus-one at 09:00,
    checked today at 14:00) would floor to 1 -- disagreeing with a caller that
    reasons in calendar days. math.ceil on the fractional-day delta rounds that
    up to 2, matching "how many calendar days until the due date" the way a
    person reads it. A due date that is exactly now (or in the past) never
    calls this -- callers guard with `due < now` / `due <= now` first."""
    return math.ceil((due - now).total_seconds() / 86400)


def due_status(project: Project, now: datetime | None = None) -> str | None:
    """Feature A badge helper: classify a project's due date relative to `now`.

    Returns:
      - None        — no due date, OR the project is in a terminal status
                      (completed/closed/cancelled): a finished project is NEVER
                      "overdue" (the work is done);
      - "overdue"   — an ACTIVE project whose due date is strictly in the past;
      - "due_soon"  — an ACTIVE project due within DUE_SOON_DAYS (inclusive of today);
      - None        — an ACTIVE project due further out than that.

    Comparison uses naive-UTC on both sides (project.due_date is stored naive-UTC;
    `now` defaults to datetime.utcnow()) so there is no tz mismatch."""
    if project is None or project.due_date is None:
        return None
    if project.status in ProjectStatus.HIDDEN_BY_DEFAULT:
        # HIDDEN_BY_DEFAULT == completed / closed / cancelled (terminal-ish): a
        # finished project is never flagged overdue/due-soon.
        return None
    now = now or datetime.utcnow()
    due = project.due_date
    if due < now:
        return "overdue"
    if days_until_due(due, now) < DUE_SOON_DAYS:
        return "due_soon"
    return None


def set_project_content_type(
    session: Session,
    project_id: int,
    content_type_id,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
) -> Project | None:
    """Set or clear a project's content type (SPEC-content-types §8.5 — editable
    post-creation). `content_type_id` may be an int, a digit-string, or None/"" to
    CLEAR it (back to the shared "General" bucket).

    Validates the new id via content_types.attachable against the PROJECT's own org +
    customer BEFORE writing (raises InvalidContentTypeError on a bad/foreign one), so
    a forged POST can never plant another customer's type on the project. Audits
    `project.content_type_set` / `project.content_type_cleared`. Reuses the org guard
    the other project mutations use: an `org_id` that doesn't match the project's
    resolves to 'not found' (None). Does NOT retro-restamp the project's existing TM
    rows — they keep the content type they were WRITTEN under (honest provenance, per
    §3.5); the caller surfaces that as a warning. Returns the Project, or None when it
    doesn't exist / is cross-org."""
    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    resolved = None
    if content_type_id is not None and str(content_type_id).strip() != "":
        from app.services import content_types as _ct

        ct = _ct.attachable(
            session,
            content_type_id,
            org_id=getattr(project, "org_id", None),
            customer_id=project.customer_id,
        )
        if ct is None:
            raise InvalidContentTypeError(content_type_id)
        resolved = ct.id
    project.content_type_id = resolved
    session.add(project)
    session.commit()
    session.refresh(project)
    from app.services import audit

    if resolved is None:
        audit.log(
            actor=actor or "system",
            action="project.content_type_cleared",
            target=f"project {project.id}",
            ip=ip,
            customer_id=project.customer_id,
        )
    else:
        audit.log(
            actor=actor or "system",
            action="project.content_type_set",
            target=f"project {project.id}",
            detail=f"content_type {resolved}",
            ip=ip,
            customer_id=project.customer_id,
        )
    return project


def _org_for(creator):
    """Org id to stamp on a newly-created row, or None (single-tenant / today's
    behavior). Returns the creating user's org ONLY when multi-org is active; with
    the gate off (or no creator) this is None so the row is left as-is / the
    Default-org backfill owns it — byte-identical to before.

    #73: current_org_id now poisons a NULL-org principal to access.BLOCKED_ORG_ID so
    every READ predicate fails closed. A WRITE must never persist that sentinel, so it
    is collapsed back to None here — the exact value such a creator stamped before
    #73, which the boot backfill then owns."""
    if creator is None or not access.multi_org_active():
        return None
    return access.stampable_org_id(access.current_org_id(creator))


# --- customers ---------------------------------------------------------------
def _name_key(name) -> str:
    """The duplicate-name comparison key: trimmed + Unicode casefolded, the same match
    GET /api/customers?name= uses (and so the MCP create_project customer lookup)."""
    return (name or "").strip().casefold()


def find_duplicate_customer(
    session: Session, name: str, org_id=None, exclude_id=None
) -> Customer | None:
    """The customer in `org_id` (None = every customer, the single-tenant case) whose name
    equals `name` ignoring case and surrounding space, archived ones included, or None.
    Oldest first, so the reported id is stable. `exclude_id` skips one customer (#617: a
    rename must not collide with itself)."""
    if access.org_scope_blocked(org_id):
        # Under an active multi-org gate a creator with no resolvable org stamps NULL; its
        # twin can only live in that same org-less space, so there is nothing to compare
        # against (the #73 fail-closed reading: an org-less scope sees no org's rows).
        return None
    key = _name_key(name)
    q = select(Customer)
    if org_id is not None:
        q = q.where(Customer.org_id == org_id)
    for cust in session.exec(q.order_by(Customer.id)).all():
        if exclude_id is not None and cust.id == exclude_id:
            continue
        if _name_key(cust.name) == key:
            return cust
    return None


def find_duplicate_project(session: Session, name: str, customer_id=None) -> Project | None:
    """The ACTIVE (not archived) project under `customer_id` named `name` ignoring case and
    surrounding space, or None. Archived projects are retired, so their names are free."""
    key = _name_key(name)
    q = select(Project).where(Project.archived_at.is_(None))
    q = q.where(
        Project.customer_id == customer_id
        if customer_id is not None
        else Project.customer_id.is_(None)
    )
    for proj in session.exec(q.order_by(Project.id)).all():
        if _name_key(proj.name) == key:
            return proj
    return None


def customer_create(
    session: Session, name: str, creator=None, reject_duplicate: bool = False
) -> Customer | None:
    """Create a customer from a (stripped) name. Returns the new Customer, or
    None when the name is blank (the route silently no-ops in that case).

    `creator` (the acting User) is optional and only consulted when multi-org is
    active, to stamp the new row with the creator's org. Existing callers that
    omit it (and every install with the gate off) get today's behavior exactly:
    org_id is left NULL for the Default-org backfill to own.

    `reject_duplicate` (#580, opt-in like require_customer on project_create): refuse a name
    that already exists in the same org (case and surrounding space ignored, archived
    customers included) with DuplicateNameError. The user-facing seams pass it; existing
    same-named rows are never touched."""
    name = ids.clean_name(name, field="customer name")
    if not name:
        return None
    org_id = _org_for(creator)
    if reject_duplicate:
        existing = find_duplicate_customer(session, name, org_id)
        if existing is not None:
            raise DuplicateNameError("customer", name, existing.id)
    customer = Customer(name=name, org_id=org_id)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    from app.services import customers as _customers

    _customers.invalidate_duplicate_labels()
    return customer


def _rollback_project_create(session: Session, project: "Project") -> None:
    """Drop a half-created project and every child row that references it, in
    child-before-parent order so FK enforcement is satisfied.

    apply_mode may have persisted ProjectPairProvider rows (project_id ->
    project.id) BEFORE it raised, so those must be deleted before the project — on
    Postgres (which has always enforced this FK) deleting the parent first would
    have failed. TargetLocale rows are created earlier in project_create and are
    likewise dropped first.

    The child deletes are FLUSHED before the project delete: neither TargetLocale
    nor ProjectPairProvider declares a SQLModel Relationship back to Project, so
    the unit-of-work has no dependency edge to order a single commit's DELETEs by —
    it could otherwise emit DELETE project before its children and trip the FK.
    An explicit flush pins the child-before-parent order."""
    for ppp in session.exec(
        select(ProjectPairProvider).where(ProjectPairProvider.project_id == project.id)
    ).all():
        session.delete(ppp)
    for tl in session.exec(select(TargetLocale).where(TargetLocale.project_id == project.id)).all():
        session.delete(tl)
    session.flush()
    session.delete(project)
    session.commit()


# --- assign a customer to a customer-LESS project (NULL -> set only) ----------
class ProjectCustomerAssignError(ValueError):
    """2026-07-14: a project's customer may be assigned ONLY when it is
    currently NULL (a legacy customer-less project). General customer REASSIGNMENT is
    deliberately NOT built — moving a project between customers has TM/glossary/context
    provenance implications (all scoped by project -> customer) that a bare column swap
    would silently corrupt. So assignment is from-NULL-only; changing an already-set
    customer raises this. The surface shows it as a clean 400 / EXIT_USAGE."""


def assign_project_customer(
    session: Session,
    project_id: int,
    customer_id,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
) -> Project | None:
    """Assign a customer to a project whose customer is currently NULL (the legacy
    customer-less remediation path — banner + `project set-customer` + /api). Returns
    the Project, or None when it doesn't exist / is cross-org. Raises:
      * ProjectCustomerAssignError when the project ALREADY has a customer (assignment
        is from-NULL-only — general reassignment is intentionally not offered; see the
        exception's docstring);
      * ArchivedCustomerError / a ValueError when the target customer is missing,
        archived, or cross-org (validated via customers.selectable, the same guard the
        create seam uses — an archived/foreign customer can never be attached).
    Audits project.assign_customer. Org-guarded like the other project mutations."""
    from app.services import customers as _customers

    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    if project.customer_id is not None:
        raise ProjectCustomerAssignError(
            f"project {project_id} already has a customer (id {project.customer_id}); "
            "customer reassignment is not supported (TM/glossary provenance is scoped "
            "by customer). Assignment is only from no-customer."
        )
    cust = _customers.selectable(session, customer_id, org_id=getattr(project, "org_id", None))
    if cust is None:
        # Nonexistent / archived / cross-org: indistinguishable, fail closed.
        raise ArchivedCustomerError(customer_id)
    project.customer_id = cust.id
    session.add(project)
    session.commit()
    session.refresh(project)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="project.assign_customer",
        target=f"project {project_id}",
        detail=f"customer -> {cust.id} ({cust.name})",
        ip=ip,
        customer_id=cust.id,
    )
    return project


# --- project rename + delete (SPEC-rename-delete) ----------------------------
def rename_project(
    session: Session,
    project_id: int,
    name: str,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
) -> Project | None:
    """Rename a project (DISPLAY name only). Every cross-entity reference is by id, so
    the rename propagates automatically and nothing disconnects (the audit-verified
    invariant). The department-inbox lookup is now keyed on Project.inbox_key, not the
    name, so renaming an inbox no longer forks a duplicate. Org-guarded exactly like the
    other project mutations (a cross-org id resolves to None). Audits project.rename with
    old->new in `detail`. Returns the Project, or None when it doesn't exist / is
    cross-org; raises ValueError on a blank name."""
    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    new_name = (name or "").strip()
    if not new_name:
        raise ValueError("project name is required.")
    old_name = project.name
    project.name = new_name
    session.add(project)
    session.commit()
    session.refresh(project)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="project.rename",
        target=f"project {project.id}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=ip,
        customer_id=project.customer_id,
    )
    return project


# --- archive / unarchive (residual sweep: archive verbs; Customer.archived_at precedent) --
class ProjectArchiveError(ValueError):
    """An archive/unarchive was a no-op-with-a-reason — archiving an already-archived
    project, or unarchiving an active one. Raised as a CLEAN error (400 / EXIT_USAGE)
    rather than silently double-stamping, so the surfaces show a precise message."""


class ProjectArchivedError(ValueError):
    """A NEW run/dispatch was refused because its project is archived. Archive is not a
    kill switch (in-flight work finishes untouched) but it blocks LAUNCHING new work, so
    an archived project can never accidentally be re-run. Surfaced as a clean refusal."""


def archive_project(
    session: Session, project_id: int, *, actor: str = "system", ip: str = "", org_id=None
) -> Project | None:
    """Archive (retire) a project: stamp archived_at=now so it drops out of the default
    project listings + pickers and is BLOCKED from launching new runs/dispatches. Its
    documents / segments / TM / run history are fully preserved (NO cascade, NO delete)
    and it is reversible (unarchive). Org-guarded (a cross-org id resolves to None, like
    rename/delete). Idempotent-safe: archiving an already-archived project raises a clean
    ProjectArchiveError rather than re-stamping. Audits project.archive. Returns the
    Project, or None when it doesn't exist / is cross-org."""
    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    if project.archived_at is not None:
        raise ProjectArchiveError(f"project {project_id} ({project.name}) is already archived.")
    project.archived_at = datetime.utcnow()
    session.add(project)
    session.commit()
    session.refresh(project)
    from app.services import audit, project_automation

    # an archived project never runs again: the items it owned on a shared connection go back
    # to the other projects (project automation claims)
    project_automation.release_claims(project.id, reason="released: project archived")

    audit.log(
        actor=actor or "system",
        action="project.archive",
        target=f"project {project.id} ({project.name})",
        ip=ip,
        customer_id=project.customer_id,
    )
    return project


def unarchive_project(
    session: Session, project_id: int, *, actor: str = "system", ip: str = "", org_id=None
) -> Project | None:
    """Unarchive (reactivate) a project: clear archived_at so it is listed, offered in
    pickers, and runnable again. Org-guarded. Idempotent-safe: unarchiving an active
    project raises a clean ProjectArchiveError. Audits project.unarchive. Returns the
    Project, or None when it doesn't exist / is cross-org."""
    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    if project.archived_at is None:
        raise ProjectArchiveError(f"project {project_id} ({project.name}) is not archived.")
    project.archived_at = None
    session.add(project)
    session.commit()
    session.refresh(project)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="project.unarchive",
        target=f"project {project.id} ({project.name})",
        ip=ip,
        customer_id=project.customer_id,
    )
    return project


def project_active_only(stmt):
    """Append the ACTIVE filter (`archived_at IS NULL`) to a `select(Project)` — the
    single seam a default LISTING / PICKER routes through so no surface drifts on the
    archive rule. A management/name-resolution read deliberately does NOT call this (it
    shows archived rows, labelled, so a project's card never silently vanishes)."""
    return stmt.where(Project.archived_at.is_(None))


def project_is_active(project: "Project | None") -> bool:
    """True when the project exists and is not archived."""
    return project is not None and getattr(project, "archived_at", None) is None


def refuse_if_project_archived(session: Session, project_id) -> None:
    """Raise ProjectArchivedError if `project_id` names an ARCHIVED project. The service
    guard every NEW-run launcher calls so an archived project can never be re-run via a
    forged POST / stray CLI id, not just be omitted from the picker. A missing project /
    None is left to the caller's own resolution (this only refuses a real archived one)."""
    if project_id is None:
        return
    project = session.get(Project, project_id)
    if project is not None and getattr(project, "archived_at", None) is not None:
        raise ProjectArchivedError(
            f"project {project.id} ({project.name}) is archived — unarchive it before "
            f"running new translations."
        )


def _project_job_ids(session: Session, project_id: int, doc_ids) -> list[int]:
    """Ids of background Jobs owned by this project. Job has NO FK to project (its
    project_id/document_id live in the JSON payload), so this scans payloads: a job is
    the project's when payload.project_id == pid OR payload.document_id is one of the
    project's documents. Used by both the delete preview + the cascade."""
    from app.models import Job

    doc_set = {int(d) for d in (doc_ids or [])}
    out: list[int] = []
    for job in session.exec(select(Job)).all():
        payload = job.payload or {}
        pj, dj = payload.get("project_id"), payload.get("document_id")
        try:
            if (pj is not None and int(pj) == project_id) or (
                dj is not None and int(dj) in doc_set
            ):
                out.append(job.id)
        except (TypeError, ValueError):
            continue
    return out


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


_CD_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f"\\]')


# --- document rename + delete (SPEC-rename-delete) ---------------------------
# F1 (MEDIUM, adversarial review): document.filename is reflected verbatim into the
# `Content-Disposition` header on download (documents.py's export route builds
# `f'attachment; filename="{fname}"'` straight from it, unescaped). rename_document
# used to only .strip()/reject-blank, so an authenticated same-tenant can_write token
# could rename to `evil"\r\nX-Injected: 1.txt` and the next download would carry a
# raw CR/LF + a quote-break — live response-header injection. A filename legitimately
# carries spaces/unicode/punctuation (unlike key_files.ascii_name's ASCII-only
# allow-list, built for a different, purely-header-facing value), so the fix is a
# narrow reject rather than a mangle: only the ASCII control range is disallowed —
# C0 (0x00-0x1F, which includes CR/LF/TAB) plus DEL (0x7F).
# C0/DEL controls (a header-line injection primitive) PLUS `"` and `\` (the two
# chars that break out of a Content-Disposition quoted-string). Used at the
# download emit seam; `;` is deliberately NOT here (inert while the quotes hold).
def header_safe_filename(name: str) -> str:
    """Neutralise the characters that let a filename break OUT of a
    `Content-Disposition` quoted-string (a bare `"` ends the value early; a
    trailing `\\` escapes the closing quote) or inject a header line (C0/DEL
    controls) — while preserving every legitimate filename character (spaces,
    Unicode, punctuation, `;` — which is inert inside the quotes). `rename_document`
    already REJECTS `"`/controls at write time (F3); this is the belt-and-suspenders
    at the emit seam, covering rows renamed before that guard landed and the
    `locale`/`fmt` pieces interpolated alongside the stem. Documents legitimately
    carry Unicode names, so this is a narrow neutralise — NOT key_files.ascii_name,
    whose ASCII-only allow-list is built for a purely-header-facing value."""
    return _CD_UNSAFE_RE.sub("_", str(name))


def delete_document(
    session: Session,
    doc_id: int,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
    privacy_erasure: bool = False,
    purge_exports: bool = True,
) -> dict | None:
    """Delete ONE document + its upload blob + segments + its whole workflow-run history
    (the admin document-delete surface). Reuses retention.delete_document with
    cascade_tm=False — a single-document delete must NOT wipe the project's shared TM (TM is
    project-scoped, spanning every document). Explicit privacy_erasure also removes Lab
    snapshots and their results without deleting shared TM. Org-guarded via the owning
    project. Returns the counts dict, or None when the
    document doesn't exist / is cross-org.

    THE CASCADE ITSELF LIVES IN `retention._reap_document_children` (2026-08-31 fix-train).
    This function used to hand-roll a run-history reap (LQA/PE -> queries -> vendor legs ->
    memory un-scope -> DeliveryEmptyApproval -> StepRun -> WorkflowRun) and then call retention
    for the rest, which meant TWO orderings of one FK graph and BOTH WERE INCOMPLETE:

      * retention's was missing the whole run leg, so `retention.purge` and the Art.17
        cascade_tm=True path raised IntegrityError on any document that had ever been
        translated (the reported bug: retention aged out nothing but untranslated uploads);
      * THIS one was missing `workflowbatch.document_id` (NOT NULL) and
        `termcandidate.source_document_id`, so the ADMIN document-delete surface ALSO raised
        on any document with a multi-locale fan-out batch or an extracted term candidate —
        and `delete_project`, which relies on the per-document seam, was FK-blocked for any
        project containing a batched document.

    So the fix is the UNION, not a transplant, and its blast radius is wider than the ticket:
    the admin document-delete and project-delete surfaces were repaired alongside retention.
    Merging them leaves ONE ordering to keep true — see that function's referrer table. This
    wrapper keeps only what is genuinely its own: the org guard, the actor/audit line, and the
    None-vs-counts contract."""
    from app.models import Document
    from app.services import audit, retention

    doc = session.get(Document, doc_id)
    if doc is None:
        return None
    project = session.get(Project, doc.project_id)
    if org_id is not None and (project is None or getattr(project, "org_id", None) != org_id):
        return None
    cust_id, fname = getattr(project, "customer_id", None), doc.filename
    # #539: an explicit Art. 17 privacy erasure also removes the TM rows that came from THIS
    # document's content (matched by its segment source text and locale pair; TM rows carry no
    # document link) and nothing else; the rest of the project TM stays. An ordinary delete
    # keeps all shared TM.
    counts = retention.delete_document(
        session,
        doc_id,
        cascade_tm=False,
        privacy_erasure=privacy_erasure,
        purge_exports=purge_exports,
        erase_document_tm=bool(privacy_erasure),
    )
    if counts.get("lab_datasets_preserved"):
        counts["warning"] = (
            f"{counts['lab_datasets_preserved']} Lab snapshot(s) still hold this document's text."
        )
    audit.log(
        actor=actor or "system",
        action="document.delete",
        target=f"document {doc_id} ({fname})",
        detail=", ".join(f"{k}={v}" for k, v in counts.items()),
        ip=ip,
        customer_id=cust_id,
    )
    return counts


def _materialize_key_values(session: Session, project_id: int, actor: str = "") -> int:
    """Give every live key of a KEY-BASED project its value rows for the project's
    current target locale set. The key-project counterpart of
    `_seed_locale_segments`: a locale added to a key project must become editable
    and translatable immediately, and the rows it needs are per (locale x CLDR
    plural form), which only `keys.ensure_value_rows` can decide.

    A strict no-op on a document project (the type check short-circuits before the
    keys service is even imported), and idempotent on a key project -- it fills gaps
    and disturbs nothing that already exists. The new rows ride the change feed as
    `value`/`created` entries, so a delta consumer learns about the new locale.
    Returns the number of keys visited."""
    project = session.get(Project, project_id)
    if project is None or getattr(project, "project_type", None) != ProjectType.KEY_BASED:
        return 0
    from app.services import keys as _keys

    visited = 0
    # ATTRIBUTED (review B5). The new rows ride the change feed as `value`/`created`
    # entries written by the ORM listener, which reads `keys.current_actor()` -- and
    # FastAPI runs a sync route handler on anyio's worker POOL, where that is "" at
    # best and another request's leftover at worst (re-review R5). Every "who added
    # this locale" entry was therefore blank. The scope is entered HERE, on the thread
    # that does the writing, so all four surfaces (web, /api, CLI, language sets)
    # inherit it from the one seam instead of each remembering.
    with _keys.actor_scope(actor or _keys.current_actor()):
        for key in _keys.iter_keys(session, project_id):
            _keys.ensure_value_rows(session, key, actor=actor)
            visited += 1
        if visited:
            session.commit()
    return visited


def _seed_locale_segments(session: Session, document_id: int, locale: str) -> int:
    """Seed source segments for `locale` on one document if it has none yet.

    Living-projects support (P2, item 4): adding a language to a project that
    already has parsed documents must give the new locale a segment set to
    translate. Clones source_text / order_index / inline_tags from whatever locale
    ingest already seeded (the first, by order_index). A no-op when the document
    already has segments for `locale` or has no segments at all. Returns the number
    of segments seeded. (Mirrors app/web/api._ensure_doc_locale_segments so the
    web-upload-then-translate path and the add-locale path seed identically.)"""
    # SPEC-key-based-projects 2.5: an INTERNAL key document's segments are key
    # VALUES -- each one carries a `key_id` and a plural form, and the set a locale
    # needs is decided by CLDR, not by cloning another locale's rows. Cloning here
    # produced key-LESS junk rows inside a translatable document (the change feed
    # cannot describe them and a run would spend budget on them). Key projects
    # materialize through `_materialize_key_values` below instead.
    doc = session.get(Document, document_id)
    if doc is not None and doc.fmt == "keys":
        return 0
    return locale_seed.seed_locale(session, document_id, locale, strict=False)


def _project_locale_norms(session: Session, project_id: int) -> set[str]:
    """The set of a project's target locales, each normalized
    (languages_catalog.normalize) so an existence/dedup test matches regardless of
    the stored spelling (legacy / connector-seeded free-text codes). The one key
    shared by the add / merge / reactivate target-locale paths."""
    return {
        languages_catalog.normalize(tl.locale)
        for tl in session.exec(
            select(TargetLocale).where(TargetLocale.project_id == project_id)
        ).all()
    }


def _project_locale_rows(session: Session, project_id: int, locale: str):
    """Every TargetLocale row of this project whose code NORMALIZES to `locale`.

    Matches on the normalized key (not the raw string), mirroring add_target_locale's
    dedup: a legacy/connector row may hold a non-canonical spelling ("es_ES") of the
    same locale a canonical request ("es-ES") names. Empty list = the locale isn't on
    the project."""
    want = languages_catalog.normalize(locale)
    return [
        tl
        for tl in session.exec(
            select(TargetLocale).where(TargetLocale.project_id == project_id)
        ).all()
        if languages_catalog.normalize(tl.locale) == want
    ]


def translated_target_locales(
    session: Session, project_id: int, doc_ids: list[int] | None = None
) -> set[str]:
    """Normalized locale codes (languages_catalog.normalize) that have EVER had
    translated content on this project — the permanence predicate (the maintainer,
    2026-07-13): "before anything has been translated it can be removed. But
    after, those documents are permanent."

    A locale counts as translated when EITHER is true:
      (a) any Segment for that locale (across the project's documents) has
          non-empty target_text — covers both SEGMENTED mode (many rows) and
          FULL_TEXT mode (a single whole-document segment; translate.py writes
          its result into the same Segment.target_text field, so one predicate
          covers both);
      (b) any WorkflowRun exists for the project's documents with that target
          locale — covers an ORCHESTRATOR workflow ATTEMPT whose output shape
          isn't reliably visible from segment state alone (e.g. a run whose
          target text was later hand-cleared in the editor). A run's mere
          existence is enough; it is never un-counted by clearing segment text.

    KNOWN BOUNDARY (accepted, needs live-tenant QA call 2026-07-13): predicate (b) fires
    only for orchestrator WorkflowRuns. The DIRECT-MT pretranslate path
    (translate.pretranslate_stream — the web SSE endpoint, the CLI `doc
    translate`, the async translate_document job, and connector pulls) writes
    Segment.target_text but creates NO WorkflowRun. So while it is running or
    done, (a) holds and the locale is permanent; but if a user then hand-clears
    ALL of that locale's target text in the editor, neither (a) nor (b) holds and
    the locale returns to removable. This is deliberately NOT closed: there is no
    cheap durable per-locale signal common to every pretranslate surface (Job
    rows miss the web/CLI direct paths; TM is excluded by design — removal
    preserves it), the trigger requires a deliberate translate→clear-all→remove
    sequence, a fully-blanked locale carries no translation work to protect, and
    removal is reversible (TM+glossary survive; re-add re-seeds). Expanding (b)
    to a partial Job/TM predicate would give a false sense of completeness.

    Deliberately batched — ONE grouped segment query (DISTINCT target_locale
    where target_text != "") + ONE grouped run query (DISTINCT target_locale) —
    so a caller computing a whole project's removable set (the project-page
    chip) never pays a per-locale N+1. A project with no documents yet has
    nothing translated (empty set)."""
    from app.models import WorkflowRun

    if doc_ids is None:
        doc_ids = list(
            session.exec(select(Document.id).where(Document.project_id == project_id)).all()
        )
    if not doc_ids:
        return set()
    seg_locales = session.exec(
        select(Segment.target_locale)
        .where(Segment.document_id.in_(doc_ids), Segment.target_text != "")
        .distinct()
    ).all()
    run_locales = session.exec(
        select(WorkflowRun.target_locale).where(WorkflowRun.document_id.in_(doc_ids)).distinct()
    ).all()
    out = {languages_catalog.normalize(loc) for loc in seg_locales if loc}
    out |= {languages_catalog.normalize(loc) for loc in run_locales if loc}
    return out


def _locale_query_thread_ids(session: Session, project_id: int, seg_ids, want) -> list:
    """Thread ids reaped when a locale is removed (SPEC-wave3-collaboration §3.5): a
    thread on one of the doomed segments, OR a thread whose `target_locale` normalizes to
    `want` (a document/project-wide locale-specific question). Threads are project-scoped;
    normalize() is a Python fn so the locale arm is filtered in Python. Used by BOTH
    remove_locale_preview (count) and remove_target_locale (delete) so the preview can
    never disagree with what is destroyed.

    F1 memory law (L6): PROJECT only (id, segment_id, target_locale) — never the whole row,
    so counting a project's threads never loads their `subject` text — and walk in
    CHUNK-sized keyset batches so a project with very many threads never materializes them
    all at once."""
    from app.models import QueryThread

    seg_set = set(seg_ids or [])
    out = []
    _CHUNK = 150
    cursor = 0
    while True:
        rows = session.exec(
            select(QueryThread.id, QueryThread.segment_id, QueryThread.target_locale)
            .where(QueryThread.project_id == project_id, QueryThread.id > cursor)
            .order_by(QueryThread.id)
            .limit(_CHUNK)
        ).all()
        if not rows:
            break
        for tid, seg_id, loc in rows:
            cursor = tid
            if (seg_id is not None and seg_id in seg_set) or (
                loc and languages_catalog.normalize(loc) == want
            ):
                out.append(tid)
    return out
