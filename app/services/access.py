"""Project visibility + membership — the single authority for project scoping.

Rules (enforced only when auth is ON and the `project_scoping` setting is "1"):
admins, managers and linguists see every project; translators/reviewers/viewers
see only projects they created or were added to via ProjectMember. When auth is
OFF (the SYSTEM operator) scoping is a no-op so single-user installs and the
offline verify suites behave exactly as before.

MULTI-ORG (the SACRED gate, default OFF): when `multi_org_enabled` is "1" AND auth
is on, a SECOND, orthogonal dimension is layered on top of project scoping — every
row belongs to exactly one org and a user only ever sees their OWN org's rows. The
org predicates (multi_org_active/current_org_id/same_org/org_filter) are the single
authority for that isolation, and they are strict no-ops while the gate is OFF, so
every existing install + verify suite is byte-identical to today.

THE LINCHPIN, and what became of it (2026-09-20): current_org_id resolved a
super-admin to their HOME org here, NOT None, so that a leaked operator credential
could not read other orgs through the normal app. The back-office was the ONLY place
with cross-org authority, and the maintainer's decision #3 was the rule keeping the two apart.

The back-office, the super-admin account type and impersonation were REMOVED. The
linchpin is therefore not weakened, it is MOOT: there is no longer an account that
could widen scope, no second surface to be confined to, and `is_superadmin` is a
dormant column nothing may read (verify_multiorg's ratchet fails on absence of that
rule, and app/db.py clears the column on every boot). What survives unchanged is the
property that mattered: current_org_id NEVER returns None for a real user in
multi-org mode. Cross-org authority now requires shell access on the host
(`require_operator` on LOC_TMS_OPERATOR_KEY), never an account flag.

Documented in: docs/architecture/identity-and-access.md
"""

from sqlmodel import select

from app.models import Customer, Organization, Project, ProjectMember, Role, User
from app.services import security, settings

# Roles that always see every project regardless of membership.
_SEE_ALL_ROLES = (Role.ADMIN, Role.MANAGER, Role.LINGUIST)

# Roles a CUSTOMER-BOUND account may hold (R2-3, item 3, verbatim: "translator/
# linguist/viewer roles only — NEVER admin/manager"). A customer-bound user is a
# "customer account": deliberately kept to non-privileged roles so admin/manager
# power (user management, settings, chain-verify, back-office) is structurally out
# of reach. Enforced at the security seam (BOTH surfaces). Note LINGUIST is allowed
# as a role but its /users, /settings, /security pages are admin-gated, so a bound
# linguist still can't reach them. (REVIEWER is NOT in the allow-list — the spec
# names exactly these three, and "only" is taken literally: fail closed.)
CUSTOMER_BINDABLE_ROLES = (Role.TRANSLATOR, Role.LINGUIST, Role.VIEWER)


# --- multi-org predicates (all no-ops unless the sacred gate is ON) -----------
def multi_org_active() -> bool:
    """True only when SaaS multi-org isolation is engaged: auth is on AND the
    `multi_org_enabled` setting is "1". While this is False (every existing
    install + all verify suites) every org predicate below short-circuits to its
    single-tenant no-op, so behavior is byte-identical to today."""
    return security.auth_required() and settings.get("multi_org_enabled") == "1"


#: #281: the one refusal sentence every web and /api settings door shows under multi-org.
INSTALL_WIDE_REFUSAL = (
    "Install-wide security settings apply to every organization, so under multi-org they "
    "are a host-operator action. Set them from the CLI on the host: `config set --help`."
)


def install_wide_security_refused(keys) -> list[str]:
    """#281: the install-wide security keys in `keys` that an org admin may NOT write
    because multi-org is active (sorted; empty = allowed). Settings are one global table and
    retention.purge has no org filter, so under multi-org an org admin who could write them
    would change posture, or arm a purge, for EVERY org. Only the host operator (`config
    set`, `admin retention config`, ... on the host) may, the Ledger-controls pattern.
    STRICT NO-OP with the gate off (every install today): multi_org_active() is False."""
    if not multi_org_active():
        return []
    guarded = settings.security_setting_keys()
    return sorted(k for k in keys if k in guarded)


def engine_profile_permitted(profile_org_id, mission_org_id) -> bool:
    """Pure engine-ring permission rule after the sacred gate has been read.

    A shared/install-level profile belongs to every mission ring; an org-owned profile belongs
    only to that org.  ``mission_org_id=None`` therefore admits only shared profiles and is never
    an admission bypass.  Callers decide whether the gate is active exactly once, then reuse this
    predicate rather than spelling the ownership expression independently.
    """
    return profile_org_id is None or profile_org_id == mission_org_id


# === #73: THE org_id=None DISAGREEMENT, AND HOW IT IS CLOSED ================= #
# The bug (round-3 finding #73, HIGH, confinement). Under an ACTIVE gate the value
# `org_id=None` meant two OPPOSITE things depending on which helper read it:
#
#   * BLOCKED, in the authorities — same_org() returns False for a NULL row org and
#     compares current_org_id(user) (None) against every stamped row (never equal);
#     org_filter() appends a WHERE that no stamped row satisfies.
#   * SEE-ALL, in every read helper written as `if org_id is not None: <narrow>` —
#     tm_module.browsable_customers, llmemory_view.visible / list_for_view,
#     org_id_clause, and ~40 siblings across services (list_glossaries, list_types,
#     list_vendors, onboarding._has_workflow, ...). None simply skipped the WHERE, so
#     a caller whose org resolved to None read EVERY org's rows.
#
# So one NULL-org principal under the gate was a cross-tenant reader on the whole
# `if org_id is not None:` family. Converging 40+ call sites one by one would be both
# incomplete (the next new helper re-opens it) and unreviewable, so the convergence is
# done at the TWO PLACES a principal's org is resolved — here and deps._ident_org_id —
# using a POISON id that is not None and can never match a row:
#
#   BLOCKED_ORG_ID = -1. Organization.id is a positive autoincrement PK, so no row can
#   ever carry it (db._assert_org_confinement_invariant asserts that at boot). Every
#   `if org_id is not None:` predicate in the tree therefore KEEPS its existing shape
#   and starts failing closed for free: `Customer.org_id == -1` selects nothing,
#   `e.org_id != -1` blocks, `Project.org_id == -1` yields an empty visible set. The
#   two authorities are unchanged in outcome (same_org still False, org_filter still
#   matches nothing) — the family now AGREES with them instead of contradicting them.
#
# The poison is a READ scope only. `stampable_org_id()` below maps it back to None at
# the four write-stamp seams that read a principal's org (content._org_for,
# security.create_user, the /api user-create stamp, llmemory.write_org_id) so a broken
# principal can never plant an org_id=-1 row.
#
# GATE OFF: multi_org_active() is False, current_org_id returns None on the first line
# as it always did, and nothing anywhere sees the sentinel. Byte-identical.
BLOCKED_ORG_ID = -1


class _Unscoped:
    """Type of `UNSCOPED` — see below. A distinct class so `is` identity is the only
    way to produce it and no int/None can be mistaken for it."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<access.UNSCOPED>"


#: The DELIBERATE, AUTHORIZED cross-org read scope — the local-root CLI's `--operator`
#: door (cli/_common.require_org_scope, backed by LOC_TMS_OPERATOR_KEY). There, and
#: only there, org_id=None means "authorized to sweep every org", not "broken
#: principal". Passing UNSCOPED to a converged read helper keeps today's see-all
#: behaviour; passing a bare None under an active gate now fails closed.
UNSCOPED = _Unscoped()


def org_scope_blocked(org_id) -> bool:
    """FAIL-CLOSED LAW (#73): must this read return NOTHING for this org scope?

    True only when the sacred gate is ACTIVE and the scope is a bare None — the
    "blocked" reading same_org()/org_filter() have always taken. `UNSCOPED` (the
    key-verified CLI operator door) is never blocked, and with the gate OFF nothing
    is ever blocked, so single-tenant behaviour is byte-identical."""
    if org_id is UNSCOPED:
        return False
    return org_id is None and multi_org_active()


def org_scope_value(org_id):
    """The concrete org id a converged read helper should narrow on, or None for
    "apply no org filter". Collapses `UNSCOPED` to None (see-all, authorized) and is
    the identity on everything else. Always called AFTER org_scope_blocked()."""
    if org_id is UNSCOPED:
        return None
    return org_id


def stampable_org_id(org_id):
    """The org a WRITE may stamp on a new row: the poison BLOCKED_ORG_ID (and
    UNSCOPED) collapse to None — a broken/unscoped principal stamps nothing, exactly
    as it did before #73 — and every real id passes through unchanged."""
    if org_id is UNSCOPED or org_id == BLOCKED_ORG_ID:
        return None
    return org_id


def current_org_id(user):
    """The org this user is scoped to in the NORMAL app / API, or None meaning
    unrestricted (single-tenant).

    None (unrestricted) is returned when:
      - the gate is off (multi_org_active() is False) — single-tenant, no scoping;
      - user is None or the synthetic SYSTEM operator (auth OFF has no org).

    Otherwise (gate ON, real user) returns user.org_id for EVERYONE, with no
    exception of any kind. This function NEVER returns None for a real user in
    multi-org mode. The old carve-out this paragraph used to describe (a super-admin
    resolving to their HOME org rather than to None) went away with the account type
    itself on 2026-09-20: there is no longer any caller that could ask for more, and
    no cross-org surface for it to ask on behalf of.

    #73: "NEVER returns None for a real user" is now TRUE BY CONSTRUCTION rather than
    by hope. A real user whose org_id is NULL under an active gate (an operator-minted
    "global" account, a row created between a write and the next boot backfill) used to
    fall out of here as None — which the `if org_id is not None:` read family read as
    SEE-ALL. It now resolves to BLOCKED_ORG_ID, so that same family fails closed. Such
    a user cannot log in anyway (user_org_active -> org_active(None) -> False), so this
    is the by-construction backstop behind the login wall, not a new refusal.
    """
    if not multi_org_active():
        return None
    if user is None or getattr(user, "email", "") == "system":
        return None
    oid = getattr(user, "org_id", None)
    return BLOCKED_ORG_ID if oid is None else oid


def same_org(user, row_org_id) -> bool:
    """May this user see a row stamped with `row_org_id`?

    No-op (always True) while the gate is off. When active, True only when the
    row's org matches the user's current org. A row with a NULL org_id (legacy /
    un-stamped / global rows) is treated as NOT visible to a scoped user — in
    multi-org mode every row a user may see must carry their org explicitly; an
    un-stamped row is quarantined rather than leaked. (Such rows are only visible
    with the gate OFF, i.e. on-prem single-tenant.)
    """
    if not multi_org_active():
        return True
    if row_org_id is None:
        return False
    return current_org_id(user) == row_org_id


# --- NULL-org normalization (re-review NEW-1) --------------------------------- #
# THE TRAP the ungated org legs walked into: off-gate, org_id is a column NOTHING
# WRITES. content._org_for returns None unless multi_org_active(), so every row created
# on a single-tenant install is stamped NULL; db._ensure_default_org backfills NULL ->
# the Default org, but only AT BOOT. So an install's rows are a MIXTURE — everything
# older than the last restart carries the Default org id, everything created since
# carries NULL — and a predicate that compares the raw column produces answers that
# depend on when the server was last restarted. Concretely: create a project, try to
# attach a source to it, and the attach is REFUSED (stamped target vs NULL source)
# until someone restarts, at which point it starts working. It also left the mirror
# hole open in the other direction: a NULL target applied no org filter at all.
#
# THE FIX: off-gate, treat a NULL org_id as the install's DEFAULT org — which is
# exactly what it means, since the boot backfill will assign precisely that. Both
# attach predicates and build_glossary_plan's long-standing org filter normalize
# before comparing, so the comparison is restart-independent and symmetric.
#
# NOT applied under the sacred gate. There, an un-stamped row is quarantined on
# purpose (same_org()'s rule) and must never be silently adopted into any org.
_DEFAULT_ORG_ID_CACHE: list = [None]


def _install_default_org_id():
    """The install's DEFAULT/root org id — the lowest-id Organization, i.e. the row
    db._ensure_default_org seeds and backfills every NULL org_id onto. None when the
    table is empty (a bare schema with no seed: then normalization is a no-op and the
    org legs simply do not engage).

    Process-cached, and deliberately only on SUCCESS: the id is stable for the life of
    an install (delete_org refuses to delete the root org), so one query per process is
    enough — while a premature None, read before the seed runs, is never cached and is
    re-resolved on the next call. This matters because the predicates are called once
    per candidate row; without the cache a project page would issue one extra query per
    project in the picker."""
    if _DEFAULT_ORG_ID_CACHE[0] is not None:
        return _DEFAULT_ORG_ID_CACHE[0]
    try:
        from sqlmodel import Session as _S

        from app.db import engine

        with _S(engine) as s:
            oid = s.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()
    except Exception:
        return None
    if oid is not None:
        _DEFAULT_ORG_ID_CACHE[0] = oid
    return oid


def normalized_org_id(row_org_id):
    """A row's org for COMPARISON purposes: NULL -> the install's default org.

    A strict identity under the sacred gate (see the block comment above: NULL-org
    rows are quarantined there, never adopted). Off-gate it collapses the
    "NULL until the next restart, then Default" duality that makes a raw comparison
    restart-dependent."""
    if multi_org_active():
        return row_org_id
    if row_org_id is not None:
        return row_org_id
    return _install_default_org_id()


def org_id_clause(col, org_id):
    """The SQL twin of normalized_org_id: rows whose `col` matches `org_id` once BOTH
    sides are normalized. None when there is nothing to filter on.

    The row side cannot be normalized in Python (it is a column), so the DEFAULT-org
    case widens to `col == default OR col IS NULL` — a NULL row org normalizes to the
    default, so it must match. A non-default org stays an exact match, and under the
    gate this is the plain equality it always was.

    #73: UNDER THE GATE a None `org_id` now returns a FALSE clause, not "no filter".
    This one is ROW PROVENANCE, not a user scope — its only caller
    (glossary_module.build_glossary_plan) passes the PROJECT's org — so the poison
    sentinel in current_org_id does not reach it and it needs its own guard. A NULL-org
    project under the gate used to auto-attach EVERY org's approved glossaries; it now
    attaches none, which is exactly what its write-time twin project_can_use_glossary
    already decided ("both orgs must be present, else quarantine")."""
    if org_scope_blocked(org_id):
        from sqlalchemy import false

        return false()
    org_id = org_scope_value(org_id)
    if org_id is None:
        return None
    if multi_org_active():
        return col == org_id
    if org_id == _install_default_org_id():
        from sqlalchemy import or_

        return or_(col == org_id, col.is_(None))
    return col == org_id


def cross_project_scope_allows(target, source) -> bool:
    """May project `target` REACH project `source`'s resources (its TM rows)?

    THE SINGLE AUTHORITY for the project-to-project attach seam (ProjectTmSource) —
    called at WRITE time (may this link be created?) AND, crucially, again at READ time
    on every TM lookup (what may this link still reach?). Because both sides call this
    one predicate against the LIVE Project rows, a link row is never an authority in
    itself: a source that has since moved org or customer, or a link planted straight
    into the DB by an attacker with write access to the table, resolves to nothing.

    Rules, outermost first:
      * NEVER a self-link. A project already reads its own rows; a self row would be a
        no-op at best and a loop invitation at worst, so it is refused at both ends.
      * ORG, UNGATED — character-for-character the same rule as
        project_can_use_glossary's org leg below, and deliberately so: the two
        predicates decide the same question about the same kind of pair, and a reader
        should be able to check one against the other line by line. If the TARGET
        carries an org, the source must carry the SAME one, on a single-tenant install
        too. This is NOT gated on multi_org_active() the way same_org()/org_filter()
        are: those answer "what may this USER see" and are no-ops off-gate by design,
        whereas this is row-to-row provenance. Every install that ever ran
        _ensure_default_org has org-stamped rows, so gating it would let an off-gate
        install attach across orgs — which is exactly the inconsistency the glossary
        leg was fixed for (behaviour review F5), arriving here by the same reasoning.
      * ORG, NULL HANDLING — also mirrored: a NULL-org TARGET applies no org filter at
        all (legacy/un-stamped installs keep today's behaviour), while an org-stamped
        target refuses a NULL-org source, since a NULL org is not the target's org.
      * ORG, UNDER THE GATE — additionally strict: BOTH orgs must be present, so an
        un-stamped row on either side is quarantined exactly the way same_org()
        quarantines one, and an attach can never become the one path that crosses orgs.
      * CUSTOMER (always, gate or no gate — this is the isolation that carries the
        single-tenant install). The source must belong to the SAME customer as the
        target, or to NO customer at all (a customer-less / house project — the
        install-wide shared bucket, which is already how a NULL-customer glossary is
        treated everywhere else). A DIFFERENT customer is refused: TM provenance is
        customer-scoped (the from-NULL-only reassignment rule is the same reasoning).

    Accepts Project instances (or anything exposing id/org_id/customer_id). A missing
    project on either side is False — fail closed."""
    if target is None or source is None:
        return False
    t_id, s_id = getattr(target, "id", None), getattr(source, "id", None)
    if t_id is None or s_id is None or t_id == s_id:
        return False
    t_org = getattr(target, "org_id", None)
    s_org = getattr(source, "org_id", None)
    if multi_org_active():
        if t_org is None or s_org is None or t_org != s_org:
            return False
    else:
        # NULL -> the install's default org on BOTH sides before comparing, so the
        # answer does not depend on when the server was last restarted and the NULL
        # target no longer skips the filter entirely (see the block comment above).
        t_org, s_org = normalized_org_id(t_org), normalized_org_id(s_org)
        if t_org is not None and s_org != t_org:
            return False
    s_cust = getattr(source, "customer_id", None)
    if s_cust is not None and s_cust != getattr(target, "customer_id", None):  # noqa: SIM103
        return False
    return True


def project_can_use_glossary(project, glossary) -> bool:
    """May `project` pin/consume module `glossary` (ProjectGlossary)?

    The glossary-side twin of cross_project_scope_allows, and likewise the SINGLE
    authority called at both write and RUN time against the live Glossary row — a pin
    whose glossary was later reassigned to another customer stops contributing without
    anything having to clean the link up.

    Mirrors, EXACTLY, the visibility rule build_glossary_plan already applies to
    auto-attach (glossary_module.build_glossary_plan's typed branch) — because the two
    sit side by side deciding the same question about the same pair of rows, and a pin
    that could reach a glossary auto-attach would refuse is a pin that quietly escapes
    the stricter neighbour:

      * ORG, UNGATED (behaviour review F5). If the project carries an org, the glossary
        must carry the SAME one — on a single-tenant install too. This is deliberately
        NOT gated on multi_org_active() the way same_org()/org_filter() are: those are
        about what a USER may see, and are no-ops off-gate by design, whereas this is
        row-to-row provenance, and the auto-attach filter it must agree with has always
        applied it ungated. An install whose rows are org-stamped (every install that
        ran _ensure_default_org) but whose gate is off would otherwise be able to PIN
        across orgs while auto-attach refused to.
      * ORG, UNDER THE GATE: additionally strict — both orgs must be present, so an
        un-stamped row on either side is quarantined rather than shared, matching
        same_org()'s treatment of a NULL-org row.
      * CUSTOMER (always): the glossary must be the project's customer's, or
        customer-less (the shared bucket auto-attach also honours).

    APPROVAL is NOT checked here; that is a lifecycle question the callers ask
    separately, so this stays purely an isolation predicate."""
    if project is None or glossary is None:
        return False
    p_org = getattr(project, "org_id", None)
    g_org = getattr(glossary, "org_id", None)
    if multi_org_active():
        if p_org is None or g_org is None or p_org != g_org:
            return False
    else:
        # Same NULL -> default normalization as cross_project_scope_allows; the two
        # blocks stay line-for-line identical modulo variable names.
        p_org, g_org = normalized_org_id(p_org), normalized_org_id(g_org)
        if p_org is not None and g_org != p_org:
            return False
    g_cust = getattr(glossary, "customer_id", None)
    if g_cust is not None and g_cust != getattr(project, "customer_id", None):  # noqa: SIM103
        return False
    return True


def org_filter(stmt, model, user):
    """Append `.where(model.org_id == current_org_id(user))` when the gate is on,
    else return the statement unchanged (single-tenant no-op). A convenience for
    P2+ list routes; unused until enforcement is wired in later phases."""
    if not multi_org_active():
        return stmt
    return stmt.where(model.org_id == current_org_id(user))


# --- customer binding predicates (R2-3, item 3) ------------------------------ #
# The INNER isolation ring: a customer-bound user is confined to ONE customer's
# projects. Org scoping (above) applies FIRST and unchanged; this is a second,
# tighter filter layered UNDER it. A strict no-op for INTERNAL STAFF (customer_id
# NULL) and when auth is OFF, so every existing install + verify suite is
# byte-identical to today.
def current_customer_id(user):
    """The customer this user is BOUND to, or None (an internal-staff account /
    the SYSTEM operator / auth off — no customer confinement).

    Unlike current_org_id, there is NO gate setting: customer binding is always
    active when auth is on and the user carries a customer_id. With auth OFF (the
    verify suites' default) the synthetic SYSTEM operator has no binding, so the
    whole feature is inert until a real bound user signs in."""
    if not security.auth_required():
        return None
    if user is None or getattr(user, "email", "") == "system":
        return None
    return getattr(user, "customer_id", None)


def is_customer_bound(user) -> bool:
    """True when this user is a customer account (confined to one customer)."""
    return current_customer_id(user) is not None


def customer_ring_blocks(caller_customer_id, row_customer_id) -> bool:
    """The SHARED workflow-family customer ring predicate (web + /api single seam).

    A GLOBAL row (row_customer_id NULL) is a shared resource visible to EVERY
    customer-bound caller — never blocked. A customer-scoped row is blocked only for
    a bound caller whose customer differs. An UNBOUND caller (caller_customer_id
    None — internal staff / auth off) is never blocked. This is the ONE predicate the
    web `_wf_customer_block` and the /api `_wf_in_org` / `_grp_in_org` / `_pt_in_org`
    helpers both call, so the two surfaces CANNOT drift on Workflow / WorkflowGroup /
    PromptTemplate isolation. 404-not-403 at the call sites (not enumerable)."""
    return (
        caller_customer_id is not None
        and row_customer_id is not None
        and row_customer_id != caller_customer_id
    )


#: #468: the refusal a customer-bound caller gets when it tries to change a shared
#: (global or org-level) asset. The same words on web, /api, MCP and the remote CLI.
SHARED_ASSET_READ_ONLY = (
    "Shared items (global workflows, groups, prompt templates, style guides and "
    "language sets) are read-only for customer accounts. Clone a workflow to make "
    "your own copy, or ask an admin or linguist on the provider side to change it."
)


def customer_write_blocks(caller_customer_id, row_customer_id) -> bool:
    """#468: the WRITE-side customer ring (the read ring is customer_ring_blocks).

    A customer-bound caller may change only rows that belong to its OWN customer. A
    GLOBAL row (row_customer_id NULL) is shared by every customer, so it is readable
    (customer_ring_blocks lets it through) but NOT writable by a bound caller; an
    org-level row with no customer column is passed as None and is read-only too. An
    UNBOUND caller (internal staff, auth off, the trusted CLI) is never blocked here,
    so single-customer installs and every staff account are unchanged."""
    return caller_customer_id is not None and row_customer_id != caller_customer_id


def _customer_project_ids(session, customer_id):
    """Project ids owned by a customer (the confinement set for a bound user)."""
    return set(session.exec(select(Project.id).where(Project.customer_id == customer_id)).all())


def can_see_all(user) -> bool:
    """True when project scoping does not restrict this user by MEMBERSHIP.

    R2-3: a CUSTOMER-BOUND user IS "see-all" WITHIN its customer — the customer
    account sees every one of its customer's projects (the maintainer's "everything for that
    customer"), NOT just ones it was explicitly added to. Membership is not a
    further restriction below the customer set; the customer binding is that user's
    scope authority. So a bound user is treated as see-all here, and
    visible_project_ids / can_view_project then CONFINE the see-all set to the
    customer's projects (exactly the way multi-org confines see-all to the org)."""
    if not security.auth_required():
        return True
    if user is None:
        return False
    if getattr(user, "email", "") == "system":  # synthetic SYSTEM operator
        return True
    # A customer-bound account sees its WHOLE customer (confined below), regardless
    # of role/membership — it is see-all within the customer ring.
    if is_customer_bound(user):
        return True
    if getattr(user, "role", "") in _SEE_ALL_ROLES:
        return True
    return settings.get("project_scoping") != "1"


def visible_project_ids(session, user):
    """Project ids this user may see, or None meaning unrestricted.

    Dimensions, from OUTER to INNER: project-scoping membership (unchanged) ->
    multi-org confinement -> R2-3 customer confinement. With every ring inert
    (gate off, unbound user, project_scoping off) this is byte-identical to before.
    """
    cid = current_customer_id(user)
    if can_see_all(user):
        # See-all is unrestricted by MEMBERSHIP. But it is still confined to the
        # user's own ORG (multi-org) AND, R2-3, to the user's CUSTOMER (bound
        # accounts). Both return a concrete id set rather than None so a scoped
        # see-all user can never list outside its ring.
        if cid is not None:
            # Customer-bound: exactly the customer's projects. #467: NOT "already
            # org-confined transitively": a bound user moved to another org keeps its
            # customer binding, so under multi-org the set is intersected with the caller's
            # CURRENT org, exactly as the membership branch below does (a NULL org
            # principal fails closed to the empty set).
            ids = _customer_project_ids(session, cid)
            if multi_org_active():
                oid = current_org_id(user)
                ids &= set(session.exec(select(Project.id).where(Project.org_id == oid)).all())
            return sorted(ids)
        if not multi_org_active():
            return None
        oid = current_org_id(user)
        return sorted(session.exec(select(Project.id).where(Project.org_id == oid)).all())
    if user is None:
        return []
    ids = set()
    email = getattr(user, "email", "") or ""
    if email:
        ids.update(session.exec(select(Project.id).where(Project.created_by == email)).all())
    uid = getattr(user, "id", None)
    if uid:
        ids.update(
            m.project_id
            for m in session.exec(select(ProjectMember).where(ProjectMember.user_id == uid)).all()
        )
    if multi_org_active():
        # Intersect the membership set with the caller's org: a project shared /
        # created cross-org (shouldn't happen, but defense in depth) is dropped.
        oid = current_org_id(user)
        org_ids = set(session.exec(select(Project.id).where(Project.org_id == oid)).all())
        ids &= org_ids
    # NB: a customer-bound user never reaches this membership branch — is_customer_bound
    # makes can_see_all True, so it is handled by the see-all customer branch above.
    return sorted(ids)


def can_view_project(session, user, project) -> bool:
    """May this user see this project? Accepts a Project instance or an id.

    Two gates in order: the ORG check (multi-org only) BEFORE the existing
    membership/role logic. When the gate is off the org check is skipped and this
    is byte-identical to before.
    """
    # Org isolation first: a project in another org is invisible regardless of
    # role/membership (no-op while the gate is off; None-org rows quarantined).
    if multi_org_active():
        p = project if isinstance(project, Project) else session.get(Project, project)
        if p is None:
            return False
        if not same_org(user, getattr(p, "org_id", None)):
            return False
    # R2-3: customer isolation ring (INNER, after org). A customer-bound user may
    # ONLY see projects owned by THEIR customer — a foreign customer's project is
    # invisible. This runs BEFORE the can_see_all short-circuit below, so a bound
    # user (who IS see-all within its customer) is still hard-confined here: on its
    # own customer's project the gate passes and can_see_all returns True; on any
    # other project this returns False. No-op for internal staff (cid is None).
    cid = current_customer_id(user)
    if cid is not None:
        p = (
            project
            if isinstance(project, Project)
            else (session.get(Project, project) if project is not None else None)
        )
        if p is None or getattr(p, "customer_id", None) != cid:
            return False
    if can_see_all(user):
        return True
    if user is None or project is None:
        return False
    p = project if isinstance(project, Project) else session.get(Project, project)
    if p is None or p.id is None:
        return False
    if p.created_by and p.created_by == (getattr(user, "email", "") or ""):
        return True
    uid = getattr(user, "id", None)
    if not uid:
        return False
    return (
        session.exec(
            select(ProjectMember).where(
                ProjectMember.project_id == p.id, ProjectMember.user_id == uid
            )
        ).first()
        is not None
    )


def can_view_job_projects(session, user, job) -> bool:
    """A job's ownership never grants access to a project after membership is revoked."""
    from app.models import Document

    if not security.auth_required():
        return True
    payload = job.payload or {}
    pid = payload.get("project_id")
    if pid is not None and not can_view_project(session, user, pid):
        return False
    did = payload.get("document_id")
    if did is not None:
        doc = session.get(Document, did)
        if doc is None or not can_view_project(session, user, doc.project_id):
            return False
    return True


# --- membership CRUD ---
def list_members(session, project_id: int):
    """(ProjectMember, User) pairs for a project, oldest first."""
    return list(
        session.exec(
            select(ProjectMember, User)
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == User.id)
            .order_by(ProjectMember.created_at, ProjectMember.id)
        ).all()
    )


def add_member(session, project_id: int, email: str, added_by: str = ""):
    """Grant a user visibility into a project. Idempotent. Returns (ok, msg).

    #178-4: under an active multi-org gate the target account is resolved INSIDE the
    project's org. An account in another org (or with no org) gets the same answer as an
    email with no account at all, so a project manager can neither add a foreign user
    nor use this form to learn whether a foreign address has an account. Every surface
    (web, CLI, /api) inherits it here. A strict no-op with the gate off."""
    email = (email or "").lower().strip()
    if not email:
        return False, "Email is required."
    user = session.exec(select(User).where(User.email == email)).first()
    if user is not None and multi_org_active():
        project = session.get(Project, project_id)
        if project is None or project.org_id is None or user.org_id != project.org_id:
            user = None
    if not user:
        return False, f"No account with email {email}."
    existing = session.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
        )
    ).first()
    if existing:
        return True, f"{email} is already a member."
    session.add(ProjectMember(project_id=project_id, user_id=user.id, added_by=added_by or ""))
    session.commit()
    return True, f"{email} added."


def remove_member(session, project_id: int, user_id: int) -> bool:
    """Revoke a membership. Returns True if a row was removed."""
    row = session.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    ).first()
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


# --- org suspension authority (multi-org P7) --------------------------------- #
def org_active(org_id) -> bool:
    """Is the org with this id active (not suspended)?

    A STRICT no-op (always True) while the sacred gate is OFF — single-tenant
    installs never read Organization.active, so behavior is byte-identical to
    today. When the gate is ON, returns False for a suspended org (active=0) or an
    unknown/None org id: a suspended org's users cannot log in and its API tokens
    are rejected. This is the SINGLE authority for org suspension; login
    (security.authenticate / session_user) and the API (deps.api_identity) call it.
    """
    if not multi_org_active():
        return True
    if org_id is None:
        return False
    from sqlmodel import Session as _S

    from app.db import engine

    with _S(engine) as s:
        org = s.get(Organization, org_id)
    return bool(org is not None and org.active)


def user_org_active(user) -> bool:
    """Is this user's home org active? No-op True with the gate off. Convenience
    wrapper so the login/session/API paths don't each re-derive the org id."""
    if not multi_org_active():
        return True
    if user is None or getattr(user, "email", "") == "system":
        return True
    return org_active(getattr(user, "org_id", None))


# --- org management CRUD (multi-org P7) -------------------------------------- #
# Cross-org operator work: the back-office (super-admin only) + the in-process CLI
# under the operator gate call these. They FUNCTION single-tenant too (creating a
# 2nd org is allowed but inert without the gate), so there is no behavior change to
# existing installs — the gate governs enforcement, not the CRUD itself.
def list_orgs(session):
    """Every Organization, id order. Cross-org: the operator CLI only, since
    2026-09-20 (`require_operator` on LOC_TMS_OPERATOR_KEY). No web caller may reach
    this: the mount that used to is deleted."""
    return list(session.exec(select(Organization).order_by(Organization.id)).all())


def get_org(session, org_id: int):
    return session.get(Organization, org_id)


def create_org(session, name: str) -> Organization:
    """Create an organization. Raises ValueError on a blank/duplicate name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Organization name is required.")
    dup = session.exec(select(Organization).where(Organization.name == name)).first()
    if dup is not None:
        raise ValueError(f"An organization named {name!r} already exists.")
    org = Organization(name=name, active=True)
    session.add(org)
    session.commit()
    session.refresh(org)
    return org


def rename_org(session, org_id: int, name: str) -> Organization:
    """Rename an org. Raises ValueError on a blank/duplicate name or unknown id."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Organization name is required.")
    org = session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"No such organization: {org_id}")
    dup = session.exec(
        select(Organization).where(Organization.name == name, Organization.id != org_id)
    ).first()
    if dup is not None:
        raise ValueError(f"An organization named {name!r} already exists.")
    org.name = name
    session.add(org)
    session.commit()
    session.refresh(org)
    return org


def set_org_active(session, org_id: int, active: bool) -> Organization:
    """Suspend (active=False) or unsuspend (active=True) an org. Idempotent.
    Raises ValueError on an unknown id."""
    org = session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"No such organization: {org_id}")
    org.active = bool(active)
    session.add(org)
    session.commit()
    session.refresh(org)
    return org


def default_org_id(session) -> int | None:
    """The id of the platform's DEFAULT/root organization — the lowest-id row,
    which db._ensure_default_org seeds ('Default') and backfills every legacy row
    onto. Renames are allowed, so the NAME is not a reliable marker; the seed is
    always the smallest id. Used by delete_org to refuse deleting the root org."""
    return session.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()


# The org-stamped tenant tables (every model carrying an org_id FK). delete_org
# fails closed while ANY of these still hold a row for the org — the emptiness
# precondition. Kept as a lazy import list (model classes) so this module stays
# import-cheap; the four the operator most cares about (users/customers/projects/
# connections) are surfaced first in the blocker message.
def _org_stamped_counts(session, org_id: int) -> "list[tuple[str, int]]":
    """[(label, count)] of org-stamped rows still belonging to `org_id`, only for
    tables that have at least one. Order puts the four headline occupants
    (users/customers/projects/connections) first, then the rest. Empty list = the
    org holds nothing and is safe to delete."""
    from sqlalchemy import func

    from app.models import (
        Connection,
        ContentType,
        ContextProfile,
        Customer,
        DepartmentBudget,
        EngineProfile,
        Glossary,
        LanguageSet,
        NotifyChannel,
        Project,
        ProjectTemplate,
        PromptTemplate,
        RoutingRule,
        SegmentationProfile,
        StyleGuide,
        User,
        Workflow,
        WorkflowBatch,
        WorkflowGroup,
    )

    ordered = [
        ("users", User),
        ("customers", Customer),
        ("projects", Project),
        ("connections", Connection),
        ("workflows", Workflow),
        ("workflow groups", WorkflowGroup),
        ("workflow batches", WorkflowBatch),
        ("language sets", LanguageSet),
        ("routing rules", RoutingRule),
        ("department budgets", DepartmentBudget),
        ("prompt templates", PromptTemplate),
        ("project templates", ProjectTemplate),
        ("content types", ContentType),
        ("context profiles", ContextProfile),
        ("engine profiles", EngineProfile),
        ("glossaries", Glossary),
        ("style guides", StyleGuide),
        ("segmentation profiles", SegmentationProfile),
        ("notification channels", NotifyChannel),
    ]
    out: list[tuple[str, int]] = []
    for label, model in ordered:
        n = session.exec(
            select(func.count()).select_from(model).where(model.org_id == org_id)
        ).one()
        if n:
            out.append((label, int(n)))
    return out


def delete_org(session, org_id: int, *, caller_org_id: int | None = None) -> Organization:
    """Delete an EMPTY organization. Operator-gated at the call site: since
    2026-09-20 there is exactly ONE, the in-process `org delete` under --operator.
    (It was two; the back-office super-admin console was the other.) This service
    stays the single authority for the destructive-verb preconditions, because a
    second caller may return and must not be able to drift from these rules.

    Fails closed (ValueError) when:
      * the id is unknown;
      * it is the DEFAULT/root org (default_org_id) — the platform's home org is
        never deletable;
      * it is the CALLER'S OWN org (caller_org_id) — you cannot delete the org you
        are acting from. NOTE (gate R3 LOW-11): this arm is currently DEAD. Its only
        caller that ever passed a real value was the back-office, deleted 2026-09-20;
        the operator CLI acts from no org and passes None. Kept deliberately, because
        the precondition is correct and a future operator surface would need it, but
        do not read a passing suite as evidence that it works.
      * it is NON-EMPTY — ANY org-stamped row (users/customers/projects/connections
        and every other org-owned entity) blocks the delete, with an itemized list
        of what remains. There is deliberately NO --force cascade: emptiness is the
        precondition, so an operator must first move/delete the org's contents.

    On success removes the Organization row (safe: emptiness means no FK referent
    can dangle) and returns the deleted org (detached) for auditing."""
    org = session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"No such organization: {org_id}")
    if org_id == default_org_id(session):
        raise ValueError(
            "The default organization cannot be deleted (it is the platform's root org)."
        )
    if caller_org_id is not None and org_id == caller_org_id:
        raise ValueError("You cannot delete the organization you are acting from.")

    def _not_empty(blockers) -> ValueError:
        detail = ", ".join(f"{n} {label}" for label, n in blockers)
        return ValueError(
            f"Organization {org.name!r} (#{org_id}) is not empty and cannot be "
            f"deleted: {detail}. Move or delete its contents first "
            "(there is no cascading force-delete)."
        )

    blockers = _org_stamped_counts(session, org_id)
    if blockers:
        raise _not_empty(blockers)
    name, oid, active = org.name, org.id, org.active
    from sqlalchemy.exc import IntegrityError

    try:
        session.delete(org)
        session.commit()
    except IntegrityError as e:
        # TOCTOU: a row landed in the org AFTER the emptiness count but BEFORE the
        # commit. The FK RESTRICT kept the data safe (nothing was deleted); turn the
        # raw IntegrityError into the SAME clean itemized "not empty" refusal instead
        # of surfacing a 500/traceback. Re-count after rollback for an accurate list.
        session.rollback()
        blockers = _org_stamped_counts(session, org_id) or [("row", 1)]
        raise _not_empty(blockers) from e
    detached = Organization(id=oid, name=name, active=active)
    return detached


def move_user_to_org(session, user_id: int, org_id: int):
    """Move a user to another org: change User.org_id ONLY. Returns
    (user, old_org_id). Raises ValueError on an unknown user or org.

    FOOTGUN (surfaced, not silently handled): the user's already-created projects,
    connections and other org-stamped rows STAY in the OLD org — this changes the
    user's membership, not their content ownership. Re-parenting content is a
    separate, deliberate operation; we never bulk-reparent here.

    Refuses (ValueError) to move the last active admin of the user's current org out of
    it (gate multiorg-inv round 1, LOW-1): set_role, set_active and erase already refuse
    that outcome, and a move would leave the old org with no administrator. With the gate
    off the count is install-wide (security.is_last_admin_of_own_org), so only the
    install's sole active admin is held back."""
    from app.services.admin_invariant import lock_user_mutations

    lock_user_mutations(session)
    user = session.get(User, user_id, populate_existing=True)
    if user is None:
        raise ValueError(f"No such user: {user_id}")
    org = session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"No such organization: {org_id}")
    old_org_id = user.org_id
    # #467: a customer binding names a customer in ONE org. Moving the account to another
    # org would leave it bound to the old org's customer (and clearing the binding here
    # would silently widen a customer account to the whole new org), so the move is refused
    # until an admin unbinds or rebinds the account on purpose.
    if old_org_id != org_id and user.customer_id is not None:
        bound = session.get(Customer, user.customer_id)
        if bound is not None and bound.org_id != org_id:
            raise ValueError(
                f"{user.email} is bound to customer #{bound.id} of another organization; "
                "unbind the account from that customer first, then move it."
            )
    if old_org_id != org_id and security.is_last_admin_of_own_org(session, user_id):
        raise ValueError(
            f"{user.email} is the last active administrator of organization "
            f"#{old_org_id}; make another account an administrator there first."
        )
    user.org_id = org_id
    session.add(user)
    session.commit()
    session.refresh(user)
    return user, old_org_id
