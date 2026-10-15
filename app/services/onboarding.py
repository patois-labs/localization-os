"""First-run onboarding checklist — the single source of truth for eligibility,
per-user state, and the four setup steps (SPEC-onboarding-tour Phases 0+1).

This is the SERVICE SEAM the web dashboard route calls, so any future CLI/api surface reads
the SAME predicates rather than re-deriving them. The UX (the "Get started" checklist card)
is web-only — the recorded web+CLI parity exception (SPEC §4.8): parity is owed over the FLAG
(onboarding_dismissed_at), not the visual overlay. Surfacing the flag on the
account export / a CLI reset seam is deferred to the spec's Phase 3.

Design decisions grounded in this codebase (NOT the spec's §4.6/§4.7 assumptions,
which are inaccurate against the real route guards — see the module notes below):

  * ELIGIBILITY (D3, maintainer-approved): only ADMIN and LINGUIST get an auto-tour.
    manager / reviewer / translator / viewer get NO auto-tour (their re-summon
    control points at /help). D7: the auth-off SYSTEM pseudo-admin (id 0, a
    login-walled feature has no per-user row to mark) is NOT eligible.

  * ROLE-AWARE STEP SETS: an admin sees the full 4-step chain
    (customer -> project -> workflow -> provider). A linguist sees the trimmed,
    AI/go-live-focused variant (workflow -> provider) — the customer/project steps
    (manager/write gated) are dropped.

  * DONE-STATE derives from REAL org-scoped records existing (Stripe's model — it
    can never lie or get stuck): a step flips done when the actual record exists in
    the user's visible scope. Resumable + self-healing by construction.

REAL route-guard grounding (cited so the mapping is verifiable, and because the
spec's stated RBAC is wrong in two places — reported to the orchestrator):
  * customer create  POST /customers            -> security.can_manage  (admin+manager)
        [spec §4.6/§4.7 said "admin-gated" — the real guard is can_manage]
  * project create   POST /projects             -> security.can_write   (all but viewer)
        [spec §4.6 said "admin/manager" — the real guard is can_write]
  * workflow author  POST /workflows/author      -> security.can_tune_ai (admin+linguist)
        [via deps._ai_block]
  * provider key set POST /setup-ai/keys/{prov}  -> security.is_admin    (ADMIN ONLY)
        [via deps._admin_block; spec §4.6 said can_tune_ai. A linguist can reach the
         /setup-ai tuning surface (nav is can_tune_ai) and owns workflow authoring,
         but the raw key PASTE is admin-only — so the linguist's "provider" step
         reflects real live-provider state yet may need an admin to complete it.]

Documented in: docs/architecture/project-setup-and-templates.md
"""

from datetime import datetime

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import Customer, Project, Role, User, Workflow
from app.services import access, security
from app.services.providers import provider_is_live

# Roles that get an auto-tour (D3, maintainer-approved). Everyone else is ineligible.
ELIGIBLE_ROLES = (Role.ADMIN, Role.LINGUIST)

# Canonical step order. Each key binds to a real creation surface in the template.
ALL_STEPS = ("customer", "project", "workflow", "provider")
_ADMIN_STEPS = ("customer", "project", "workflow", "provider")
_LINGUIST_STEPS = ("workflow", "provider")


def _is_real_user(user) -> bool:
    """A real, persisted account (not None, not the auth-off SYSTEM sentinel id 0)."""
    return bool(user) and bool(getattr(user, "id", 0)) and getattr(user, "email", "") != "system"


def variant(user):
    """The onboarding variant for this user: 'admin', 'linguist', or None.

    None = ineligible (no auto-tour). D7: with auth OFF session_user is the SYSTEM
    admin (id 0) and the feature is a login-walled one, so we short-circuit to None
    the moment auth is off OR the user is not a real persisted account."""
    if not security.auth_required():
        return None  # D7: auth-off SYSTEM user is ineligible
    if not _is_real_user(user):
        return None
    role = getattr(user, "role", "")
    if role == Role.ADMIN:
        return "admin"
    if role == Role.LINGUIST:
        return "linguist"
    return None  # manager/reviewer/translator/viewer: none


def is_eligible(user) -> bool:
    """True when this user gets an auto-tour (admin/linguist, auth on, real row)."""
    return variant(user) is not None


def _steps_for(variant_name) -> tuple:
    return _ADMIN_STEPS if variant_name == "admin" else _LINGUIST_STEPS


def is_dismissed(user) -> bool:
    """True once the flag is stamped (explicit dismiss or D6 auto-dismiss)."""
    return getattr(user, "onboarding_dismissed_at", None) is not None


# --- done-state predicates (real records in the user's visible scope) ---------
def _has_customer(session: Session, user) -> bool:
    """A customer exists in the user's VISIBLE scope. The admin variant is the only
    one that shows this step and admins are unbound internal staff (so in practice
    this is 'any customer exists'), but a CUSTOMER-BOUND caller is confined to its
    OWN customer ring exactly like _has_project — a bound user must never read
    another customer's existence. Org-confined when the multi-org gate is on."""
    cid = access.current_customer_id(user)  # non-None = customer-bound
    if cid is not None:
        # Bound: the caller sees exactly its own customer (never a foreign one).
        stmt = select(Customer.id).where(Customer.id == cid)
    else:
        stmt = select(Customer.id)
        oid = access.current_org_id(user)  # None unless the multi-org gate is on
        if oid is not None:
            stmt = stmt.where(Customer.org_id == oid)
    return session.exec(stmt.limit(1)).first() is not None


def _has_project(session: Session, user) -> bool:
    """A project exists in the user's VISIBLE scope (access.visible_project_ids —
    the same authority the dashboard uses). None = unrestricted -> any project."""
    visible = access.visible_project_ids(session, user)
    if visible is None:
        return session.exec(select(Project.id).limit(1)).first() is not None
    return len(visible) > 0


def _has_workflow(session: Session, user) -> bool:
    """A workflow exists in the user's VISIBLE scope. A CUSTOMER-BOUND caller sees
    GLOBAL workflows (customer_id NULL, shared) PLUS its own customer's workflows,
    and MUST NOT read a foreign customer's — this replicates the shared
    access.customer_ring_blocks predicate as a query filter (global always visible;
    a scoped row visible only when it matches the caller's customer). Unbound
    callers keep org-wide behavior. Org-confined when the multi-org gate is on."""
    stmt = select(Workflow.id)
    oid = access.current_org_id(user)
    if oid is not None:
        stmt = stmt.where(Workflow.org_id == oid)
    cid = access.current_customer_id(user)  # non-None = customer-bound
    if cid is not None:
        # Mirror customer_ring_blocks: own-customer OR global (shared) only.
        stmt = stmt.where((Workflow.customer_id == cid) | (Workflow.customer_id == None))  # noqa: E711
    return session.exec(stmt.limit(1)).first() is not None


def _has_live_provider() -> bool:
    """The install's DEFAULT provider is LIVE (a real key, a local URL or a registered
    engine), so translation can actually run. This is the same test the app-shell
    banner uses (app_meta()'s mock_fallback), so the checklist can never read DONE
    while the banner says the default engine has no key (#561). The explicit offline
    mock is a demo, not a connected provider, so it leaves the step open."""
    default = (config.DEFAULT_PROVIDER or "").strip().lower()
    if not default or default == "mock":
        return False
    return provider_is_live(default)


def steps_state(session: Session, user):
    """The ordered steps for this user's variant, each {'key', 'done'}. None when
    the user is ineligible. Done-state is computed fresh from real records on every
    call, so the checklist is inherently resumable + self-healing."""
    v = variant(user)
    if v is None:
        return None
    done = {
        "customer": _has_customer(session, user),
        "project": _has_project(session, user),
        "workflow": _has_workflow(session, user),
        "provider": _has_live_provider(),
    }
    return [{"key": k, "done": done[k]} for k in _steps_for(v)]


def dismiss(session: Session, user) -> None:
    """Stamp onboarding_dismissed_at = now on this user's row (explicit dismiss or
    the D6 all-done auto-dismiss). Idempotent-ish: only writes when currently NULL
    so a re-summon-then-revisit never bumps the timestamp. No-op for a non-real
    user (SYSTEM / None)."""
    if not _is_real_user(user):
        return
    row = session.get(User, user.id)
    if row is not None and row.onboarding_dismissed_at is None:
        row.onboarding_dismissed_at = datetime.utcnow()
        session.add(row)
        session.commit()


def reset(user_id: int) -> None:
    """Clear onboarding_dismissed_at (-> NULL) so the account is tour-eligible
    again (SPEC-onboarding-tour Phase 3, the `account reset-onboarding` /
    POST /api/account/onboarding-reset support seam). Opens its own session, since
    the CLI/api callers only hold a user id (not a live session+row like the
    web dashboard's `dismiss()` above). Idempotent: a no-op write when already
    NULL. Silently no-ops for an unknown id — callers resolve/validate the
    target user before calling this."""
    with Session(engine) as s:
        u = s.get(User, user_id)
        if u is not None:
            u.onboarding_dismissed_at = None
            s.add(u)
            s.commit()


def dashboard_panel(session: Session, user, *, force: bool = False):
    """The render context for the dashboard's "Get started" card, or None when it
    should not render.

    Show rules (SPEC-onboarding-tour Phase 1 + D6):
      * ineligible user (variant None)          -> None.
      * D6 auto-dismiss: when all steps read done AND the flag is still NULL, stamp
        it now (a fully-set-up account never re-sees an all-checked panel on the
        auto path). This is the ONE deliberate write on a dashboard GET.
      * auto-show: eligible + not dismissed      -> render (expanded).
      * re-summon (force, from ?tour=1): render REGARDLESS of dismissed/complete —
        this OVERRIDES display for the current view; it does NOT clear the flag, so
        the D6 invariant holds. A completed account's re-summon shows the "you're
        set up" confirmation.
    """
    v = variant(user)
    if v is None:
        return None
    dismissed = is_dismissed(user)
    # Fast path: a dismissed user who is not re-summoning never needs the four
    # done-state queries — every dismissed admin would otherwise pay them on every
    # dashboard GET forever. (The only writes/queries below are gated on force or
    # not-dismissed, so returning here changes nothing observable.)
    if dismissed and not force:
        return None
    steps = steps_state(session, user)
    done_count = sum(1 for s in steps if s["done"])
    total = len(steps)
    all_done = done_count == total
    # D6: freshly all-done -> auto-dismiss so it stops auto-showing.
    if all_done and not dismissed:
        dismiss(session, user)
        dismissed = True
    if not (force or not dismissed):
        return None
    return {
        "variant": v,
        "steps": steps,
        "done_count": done_count,
        "total": total,
        "all_done": all_done,
        "forced": bool(force),
    }
