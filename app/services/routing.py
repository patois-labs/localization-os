"""Content routing (MCP Phase B) — deterministic ext + department routing.

The resolver decides, for an inbound document, WHICH project/provider it should be
translated under, based on the file extension and the submitting actor's
department. Resolution is deterministic and audit-logged so a routing decision is
always explainable after the fact.

Resolution order (routing.resolve):
  1. caller-explicit overrides  — an explicit project_id/provider the caller passed
     always wins (the API/UI already chose; routing must not override a human). This
     is a no-op decision and is NOT audited (Finding 7).
  2. first ACTIVE matching rule  — by priority ascending (lower = higher
     precedence), then id; a rule matches when its match_ext (blank = any) and
     department (blank = any) both match. Audited.
  3. fallback                   — the actor's department inbox project (created on
     demand, Chunk 2) + no provider override (project/config default engine).
     Audited when an inbox is selected/created; when ensure_inbox=False (the
     translate path resolving the PROVIDER dimension only), a no-match is a pure
     no-op and is NOT audited (Finding 7).

Two API entry points feed this (Finding 1):
  * UPLOAD (no caller project_id): resolve with ensure_inbox=True — a matching
    rule's target project places the document, else the department inbox catches it.
  * TRANSLATE (doc already has a project): resolve with explicit_project_id=None +
    ensure_inbox=False for MATCHING only — the project is never reassigned, but a
    matching rule's PROVIDER override wins over the project/config default engine.

v1 caveat (§9.3): routing picks the PROJECT and PROVIDER only. It does NOT switch
the translation to the workflow orchestrator — the per-segment MT pretranslate
path (FULL_TEXT-only orchestrator write-back) stays the real path. workflow_id /
language_set_id / customer_id are carried on the decision for callers/telemetry
but not acted on by the translate path in v1.

Guardrail (validate_rule): a rule may not target a workflow containing a
human_review step for the API path (the API is fire-and-forget; a workflow that
blocks on a human would strand the job). Enforced at rule-save time.

Documented in: docs/architecture/cost-and-budgets.md
"""

from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from app import config
from app.models import (
    LanguageSet,
    Project,
    RoutingRule,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import audit, settings


class RoutingRuleError(ValueError):
    """A routing rule was rejected at save time for a reason the operator can fix
    (a nonexistent workflow/project/language-set id). Both surfaces surface it as a
    clean message: the web route as an inline ?error= redirect, the CLI as a
    CliError(EXIT_USAGE). Raised by create_rule as a fail-closed backstop so a bad
    FK can never reach the DB and dump a raw IntegrityError (with SQL) at the
    operator (#26)."""


@dataclass
class RoutingDecision:
    """The outcome of resolving a routing rule for one inbound document."""

    project_id: int | None = None
    provider: str | None = None
    workflow_id: int | None = None
    language_set_id: int | None = None
    customer_id: int | None = None
    rule_id: int | None = None
    reason: str = ""


def _ext_matches(match_ext: str, ext: str) -> bool:
    """A blank match_ext matches anything; otherwise the doc's ext must be one of
    the comma-separated exts (case-insensitive, dot-insensitive)."""
    match_ext = (match_ext or "").strip().lower()
    if not match_ext:
        return True
    ext = (ext or "").strip().lower().lstrip(".")
    wanted = {e.strip().lstrip(".") for e in match_ext.split(",") if e.strip()}
    return ext in wanted


def _dept_matches(rule_department: str, department: str) -> bool:
    """A blank rule department matches anyone; otherwise it must match exactly
    (case-insensitive)."""
    rd = (rule_department or "").strip().lower()
    if not rd:
        return True
    return rd == (department or "").strip().lower()


def workflow_has_human_review(session: Session, workflow_id: int) -> bool:
    """True if the workflow contains at least one human_review step."""
    if not workflow_id:
        return False
    step = session.exec(
        select(WorkflowStep).where(
            WorkflowStep.workflow_id == workflow_id, WorkflowStep.step_type == StepType.HUMAN_REVIEW
        )
    ).first()
    return step is not None


def _missing_fk(
    session: Session,
    *,
    project_id: int | None,
    workflow_id: int | None,
    language_set_id: int | None,
    org_id: int | None = None,
) -> str | None:
    """First-missing-referent message, or None. A rule's project/workflow/language-set
    ids are FK columns; a nonexistent id would fail closed at INSERT with a raw
    IntegrityError (leaking SQL) — this turns it into a clean, specific error BEFORE
    the write (#26). Blank/None ids (the common "no constraint" case) are fine.

    ORG-SCOPED EXISTENCE (#26's recorded LOW residual, fixed here): when `org_id` is
    given (the creating admin's org under the multi-org gate), a referent in ANOTHER
    org is treated as NONEXISTENT — same "no such X" message as a truly-missing id, so
    a rule can never point at a foreign org's workflow/project/language-set and the id
    is not an existence oracle. org_id=None (gate off / --operator unscoped) is
    byte-identical to before: a bare existence check across the single tenant."""
    for label, model, ident in (
        ("workflow", Workflow, workflow_id),
        ("project", Project, project_id),
        ("language set", LanguageSet, language_set_id),
    ):
        if ident is None:
            continue
        row = session.get(model, ident)
        if row is None or (org_id is not None and getattr(row, "org_id", None) != org_id):
            return f"no such {label}: {ident}"
    return None


def validate_rule(
    session: Session,
    *,
    workflow_id: int | None,
    project_id: int | None = None,
    language_set_id: int | None = None,
    org_id: int | None = None,
) -> str | None:
    """Return an error message if the rule is invalid, else None. Both surfaces call
    this before create_rule to produce their inline error (web ?error= redirect / CLI
    CliError), so keep the checks surface-agnostic.

    Checks: (1) referenced project/workflow/language-set ids must EXIST *within the
    caller's org* (`org_id`; a cross-org referent behaves as nonexistent — #26's
    LOW residual) — a bad/foreign id would otherwise dump a raw FK IntegrityError
    (#26); (2) v1 API guardrail — a rule may not target a workflow with a
    human_review step (the API path is fire-and-forget and would strand a job that
    blocks on a human)."""
    missing = _missing_fk(
        session,
        project_id=project_id,
        workflow_id=workflow_id,
        language_set_id=language_set_id,
        org_id=org_id,
    )
    if missing:
        return missing
    if workflow_id and workflow_has_human_review(session, workflow_id):
        return (
            "This workflow contains a human-review step, which cannot be used "
            "for automated (API) routing."
        )
    return None


# --- reserved-for-v2 rule fields (QW-D trap fix) ----------------------------
# `match_subject`, `workflow_id` and `language_set_id` are authored on every surface
# (web form, CLI `routing`/`admin routing`, /api, MCP) but consulted by NO reader in
# v1: resolve() matches on ext + department ONLY (see _ext_matches/_dept_matches),
# and its two consumers read the decision's project_id (upload) and provider
# (translate) ONLY -- app/web/api.py:1288, :1419. resolve() copies workflow_id/
# language_set_id/customer_id onto the decision for telemetry (see the module
# docstring "v1 caveat"), but nothing acts on them. A rule authored with these fields
# therefore silently does NOTHING; capabilities/routing.md used to advertise them as
# working. So they are REFUSED at input on every authoring surface with a clear
# reserved-for-v2 message, rather than stored as dead config. (`customer_id` is a
# vestigial model column with NO writer on any surface -- not in create_rule's
# signature, rule_to_dict, any flag/form/body field -- so there is nothing to refuse;
# it is documented as reserved.) Refusal lives at the SURFACES, not in create_rule/
# validate_rule: the model keeps the columns so EXISTING stored rules read back
# intact, and a direct service caller (e.g. a migration/backfill or a read-back test)
# can still construct one.
_RESERVED_TAIL = (
    "reserved for v2 and not consulted at run time yet; a routing rule "
    "honors only ext, department, project, provider and priority."
)


def _field_present(value) -> bool:
    """True when a reserved field was actually supplied. Robust to the RAW, untrusted
    shapes the web/api surfaces hand in (MED-2: check presence BEFORE any id coercion, so
    a non-coercible value like 0 / -1 / 'abc' / '1e3' / True / 10**20 is REFUSED, not
    silently swallowed) and to non-string types (LOW-5: a JSON number/list for `subject`
    must not AttributeError a 500). None or an empty/whitespace string = absent (a form
    posts empty fields); every other present value counts as set."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def reserved_fields_set(*, match_subject=None, workflow_id=None, language_set_id=None) -> list:
    """The human names of the reserved-for-v2 rule fields that were actually supplied,
    in a stable order. Empty when none were set. Accepts RAW surface values (strings,
    ints, None, or junk) -- presence, not coercibility, is what refuses (MED-2/LOW-5)."""
    dead = []
    if _field_present(match_subject):
        dead.append("subject")
    if _field_present(workflow_id):
        dead.append("workflow")
    if _field_present(language_set_id):
        dead.append("language set")
    return dead


def reserved_field_error(
    *, match_subject=None, workflow_id=None, language_set_id=None
) -> str | None:
    """A refusal message naming any reserved-for-v2 field the caller tried to set on a
    routing rule, or None when only honored fields were supplied. Pass the RAW surface
    values (pre-coercion) so a non-coercible reserved value still refuses (MED-2). Every
    authoring surface calls this BEFORE create_rule so the message is byte-identical (web
    ?error= redirect, CLI CliError exit 2, /api 400, MCP relay)."""
    dead = reserved_fields_set(
        match_subject=match_subject, workflow_id=workflow_id, language_set_id=language_set_id
    )
    if not dead:
        return None
    if len(dead) == 1:
        subject = f"{dead[0]} is"
    elif len(dead) == 2:
        subject = f"{dead[0]} and {dead[1]} are"
    else:
        subject = f"{', '.join(dead[:-1])} and {dead[-1]} are"
    return f"{subject} {_RESERVED_TAIL}"


def reserved_note(value) -> str:
    """The ' (reserved v2, not consulted)' suffix a read-back / list line appends when a
    reserved field carries authored-but-inert data. Shared by every surface's
    `rule show` so the annotation is byte-identical. Uses the same presence test as the
    refusal (_field_present) so the annotation and the refusal never disagree. Blank/None
    -> no suffix."""
    return " (reserved v2, not consulted)" if _field_present(value) else ""


# --- rule CRUD (extracted from app/web/routing.py so route + CLI share) -------
# These moved out of the /routing route handlers VERBATIM so the web form and the
# CLI drive one code path. The route still owns HTTP concerns (admin gate, CSRF,
# the RedirectResponse); the DB mutation + audit line live here. validate_rule
# stays a caller responsibility (the route calls it before create_rule to produce
# its inline "?error=" redirect; create_rule itself does not re-validate).
def create_rule(
    session: Session,
    *,
    priority=100,
    match_ext="",
    match_subject="",
    department="",
    project_id=None,
    workflow_id=None,
    language_set_id=None,
    provider="",
    actor="system",
    ip: str = "",
    org_id=None,
) -> "RoutingRule":
    """Create a routing rule and audit it. Mirrors the /routing POST body: the
    string fields are trimmed (match_ext lower-cased), a blank provider stores
    None, and priority coerces to int (defaulting to 100 on a bad value). Returns
    the persisted RoutingRule.

    `org_id`, when given (the creating admin's org in multi-org mode — leak vector
    12), stamps the rule so resolve() only ever applies it to the SAME org's
    submissions; None (the gate off) leaves org_id NULL, byte-identical to today."""
    # Fail-closed backstop: validate FK existence HERE too (not only in validate_rule)
    # so a direct service caller or a surface that forgot to pre-validate can never
    # reach the INSERT with a bad id and dump a raw IntegrityError (#26). Org-scoped
    # by the SAME org_id used to stamp the rule (#26 LOW residual) — a cross-org
    # referent is refused as nonexistent, so a rule's target always shares its org.
    missing = _missing_fk(
        session,
        project_id=project_id,
        workflow_id=workflow_id,
        language_set_id=language_set_id,
        org_id=org_id,
    )
    if missing:
        raise RoutingRuleError(missing)
    # A rule's provider is a TRANSLATE slot: a quality-judge engine (jev / jev_local)
    # is refused here, at save, not later as a run-time JudgeEngineNotAProvider (gate L-1).
    from app.services import engines

    try:
        engines.assert_ref_role(provider, "translate")
    except engines.EngineRoleError as exc:
        raise RoutingRuleError(str(exc)) from exc
    try:
        prio = int(str(priority).strip())
    except (TypeError, ValueError):
        prio = 100
    rule = RoutingRule(
        priority=prio,
        match_ext=(match_ext or "").strip().lower(),
        match_subject=(match_subject or "").strip(),
        department=(department or "").strip(),
        project_id=project_id,
        workflow_id=workflow_id,
        language_set_id=language_set_id,
        provider=(provider or "").strip() or None,
        created_by=actor,
        org_id=org_id,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    audit.log(
        actor=actor,
        action="routing.create",
        target=f"rule {rule.id}",
        detail=(
            f"ext={rule.match_ext or '*'} dept={rule.department or '*'} "
            f"prio={rule.priority} project={rule.project_id} "
            f"provider={rule.provider or '-'}"
        ),
        ip=ip,
    )
    return rule


def toggle_rule(
    session: Session, rule_id: int, actor="system", ip: str = "", org_id: int | None = None
) -> Optional["RoutingRule"]:
    """Flip a rule's active flag and audit it. Returns the rule, or None when the
    id does not exist (the route treats a missing id as a harmless no-op).

    Org-scoped (in-process `--org`): a rule in ANOTHER org is treated as NONEXISTENT
    (returns None, no mutation) — indistinguishable from a missing id. org_id=None
    (--operator / gate off) is unscoped, byte-identical to today."""
    rule = session.get(RoutingRule, rule_id)
    if rule is None or (org_id is not None and getattr(rule, "org_id", None) != org_id):
        return None
    rule.active = not rule.active
    session.add(rule)
    session.commit()
    audit.log(
        actor=actor,
        action="routing.toggle",
        target=f"rule {rule_id}",
        detail="active" if rule.active else "inactive",
        ip=ip,
    )
    return rule


def delete_rule(
    session: Session, rule_id: int, actor="system", ip: str = "", org_id: int | None = None
) -> bool:
    """Delete a rule and audit it. Returns True if a row was removed, False when
    the id did not exist (the route treats a missing id as a harmless no-op).

    Org-scoped (in-process `--org`): a rule in ANOTHER org is treated as NONEXISTENT
    (returns False, nothing deleted) — indistinguishable from a missing id. org_id=None
    (--operator / gate off) is unscoped, byte-identical to today."""
    rule = session.get(RoutingRule, rule_id)
    if rule is None or (org_id is not None and getattr(rule, "org_id", None) != org_id):
        return False
    session.delete(rule)
    session.commit()
    audit.log(actor=actor, action="routing.delete", target=f"rule {rule_id}", ip=ip)
    return True


def rule_to_dict(rule: "RoutingRule") -> dict:
    """The ONE stable JSON row shape for a single routing rule, emitted identically by
    `admin routing rule show` (in-process + --remote) and GET /api/routing/rules/{id}
    so the surfaces cannot drift (the #17 row-shape lesson). A superset of the
    `rule list` payload plus the read-back fields (match_subject/language_set_id/
    created_by/created_at) the list omits."""
    return {
        "id": rule.id,
        "priority": rule.priority,
        "match_ext": rule.match_ext,
        "match_subject": rule.match_subject,
        "department": rule.department,
        "project_id": rule.project_id,
        "workflow_id": rule.workflow_id,
        "language_set_id": rule.language_set_id,
        "provider": rule.provider,
        "active": rule.active,
        "created_by": rule.created_by,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


def get_rule(session: Session, rule_id: int, *, org_id: int | None = None) -> "RoutingRule | None":
    """Fetch one routing rule for read-back, or None when it is missing OR (org_id
    given, multi-org gate on) belongs to ANOTHER org. A cross-org id is
    indistinguishable from a truly-missing one (no existence oracle) — the shared
    web+CLI seam for `admin routing rule show` + GET /api/routing/rules/{id}. org_id
    None (gate off / --operator unscoped) = a bare fetch, byte-identical to today."""
    rule = session.get(RoutingRule, rule_id)
    if rule is None:
        return None
    if org_id is not None and getattr(rule, "org_id", None) != org_id:
        return None
    return rule


def resolve(
    session: Session,
    *,
    ext: str,
    department: str = "",
    actor: str = "",
    explicit_project_id: int | None = None,
    explicit_provider: str | None = None,
    ensure_inbox: bool = True,
    org_id=None,
) -> RoutingDecision:
    """Resolve the routing decision for an inbound document.

    - `ext`: the document's file extension / format (e.g. "srt", "json").
    - `department`: the submitting actor's department ("" = none).
    - `explicit_project_id`/`explicit_provider`: caller-chosen overrides that win
      over any rule (the caller already decided).
    - `ensure_inbox`: when no rule/override picks a project, find-or-create the
      actor's department inbox project as the fallback landing spot.

    A decision is audit-logged (action="routing.resolve") only when routing actually
    decided something — a rule matched, or an inbox project was selected/created. A
    pure explicit-override or a no-match-with-ensure_inbox=False is a no-op and is
    NOT audited (Finding 7), so the translate path's provider-dimension resolve does
    not emit a routing row on every call.
    """
    decision = RoutingDecision()

    # 1. caller-explicit overrides win outright. This is a no-op as far as routing
    #    is concerned (the caller already chose the project) — we do NOT audit it
    #    (Finding 7: audit only when routing actually decided something).
    if explicit_project_id is not None:
        decision.project_id = explicit_project_id
        decision.provider = explicit_provider
        decision.reason = "explicit project_id from caller"
        return decision

    # 2. first active matching rule by (priority, id). In multi-org mode (org_id
    #    given) ONLY the acting org's rules are considered (leak vector 12): a rule
    #    of org A must never route org B's submission — even a department name shared
    #    across orgs stays isolated because the rule set itself is org-scoped here.
    #    org_id=None (the gate off) considers every rule, byte-identical to today.
    rq = select(RoutingRule).where(RoutingRule.active == True)  # noqa: E712
    if org_id is not None:
        rq = rq.where(RoutingRule.org_id == org_id)
    rules = session.exec(rq.order_by(RoutingRule.priority, RoutingRule.id)).all()
    for rule in rules:
        if _ext_matches(rule.match_ext, ext) and _dept_matches(rule.department, department):
            decision.rule_id = rule.id
            decision.project_id = rule.project_id
            decision.provider = explicit_provider or rule.provider
            decision.workflow_id = rule.workflow_id
            decision.language_set_id = rule.language_set_id
            decision.customer_id = rule.customer_id
            decision.reason = (
                f"matched rule #{rule.id} "
                f"(ext={rule.match_ext or '*'} dept={rule.department or '*'})"
            )
            # A rule may set only a provider (no project) — fall through to the
            # inbox fallback for the project while keeping the rule's provider.
            if decision.project_id is None and ensure_inbox:
                inbox = ensure_department_inbox(session, department, actor, org_id=org_id)
                decision.project_id = inbox.id
                decision.reason += "; project -> department inbox"
            # A rule matched — a real routing decision, so audit it.
            _audit(actor, decision, ext, department)
            return decision

    # 3. fallback: the actor's department inbox project.
    decision.provider = explicit_provider
    if ensure_inbox:
        inbox = ensure_department_inbox(session, department, actor, org_id=org_id)
        decision.project_id = inbox.id
        decision.reason = "no matching rule -> department inbox"
        # An inbox was selected/created — audit that placement.
        _audit(actor, decision, ext, department)
    else:
        # No rule matched and no inbox selected — a pure no-op (the provider-only
        # match dimension on the translate path). Don't spam an audit row per
        # translate call (Finding 7).
        decision.reason = "no matching rule"
    return decision


def _audit(actor: str, decision: RoutingDecision, ext: str, department: str) -> None:
    audit.log(
        actor=actor or "system",
        action="routing.resolve",
        target=f"project {decision.project_id}" if decision.project_id else "(none)",
        detail=(
            f"ext={ext or '*'} dept={department or '*'} "
            f"provider={decision.provider or '(default)'} "
            f"rule={decision.rule_id or '-'}: {decision.reason}"
        ),
    )


def department_for(ident: dict) -> str:
    """The department of the calling identity, for routing/inbox resolution.

    A per-user API key ("user" kind) carries the User, whose `department` column
    (Chunk 2) is the answer; the shared connector token and auth-off single-operator
    mode have no department (""). Written defensively so it is safe before the
    User.department column exists."""
    if not ident or ident.get("kind") != "user":
        return ""
    user = ident.get("user")
    return (getattr(user, "department", "") or "").strip()


# --- department inbox projects (Chunk 2) -----------------------------------
def _inbox_key(department: str, actor: str) -> str:
    """The STABLE key an inbox project is looked up by (SPEC-rename-delete inbox
    re-key). Renaming an inbox no longer forks a duplicate because resolution keys on
    this column, not on Project.name (which is now display-only).

    A department inbox is shared per department -> "dept:<lower>"; an actor with no
    department gets a per-actor inbox -> "actor:<actor>" (preserving the pre-Phase-B
    per-actor behaviour). The department is lower-cased into the key so "Sales" and
    "sales" resolve to the SAME inbox (the same canonicalisation _inbox_name applies to
    the display name). The migration + db._ensure_columns backfill reproduce exactly
    these keys for pre-existing inbox projects."""
    dept = (department or "").strip().lower()
    if dept:
        return f"dept:{dept}"
    return f"actor:{actor or ''}"


def _inbox_name(department: str, actor: str) -> str:
    """A department inbox is shared per department; when the actor has no
    department we fall back to a per-actor inbox (preserving the pre-Phase-B MCP
    inbox behaviour so auth-off / no-department callers keep one inbox each).

    The department is lower-cased into the inbox name so inbox identity is the ONE
    canonical form (Finding 6): "Sales" and "sales" resolve to the same
    "Inbox: sales" project instead of forking two inboxes. User.department is now
    stored lower-cased too, so this only matters for callers passing raw strings."""
    dept = (department or "").strip().lower()
    if dept:
        return f"Inbox: {dept}"
    return f"MCP Inbox ({actor})"


def ensure_department_inbox(session: Session, department: str, actor: str, org_id=None) -> Project:
    """Find (or create) the inbox project inbound documents land in when no rule /
    caller override names a project. ONE inbox per department (shared across every
    actor in that department, case-insensitively); when the actor has no department,
    one inbox per actor (the pre-Phase-B behaviour — see _inbox_name).

    This is the single inbox authority — api._ensure_inbox_project delegates here.
    Source locale = the install default; provider = the config default (same as the
    original _ensure_inbox_project).

    MULTI-ORG (leak vector 11 — inbox auto-create org stamp): `org_id`, when given
    (the acting API identity's org), is stamped onto a NEWLY-created inbox so an
    auto-provisioned inbox lands in the caller's org rather than NULL (quarantined).
    An existing inbox is returned as-is. `org_id=None` (every caller with the gate
    off) leaves the row unstamped — byte-identical to today. A department inbox
    match is scoped to the caller's org too when org_id is given, so two orgs sharing
    a department name each get their own inbox instead of colliding."""
    # #73 (Q3 gate): org_id here flows from _ident_org_id(ident), which now poisons an
    # org-less principal to access.BLOCKED_ORG_ID (a READ sentinel). This is a WRITE seam
    # (it STAMPS org_id onto a newly-created inbox Project), so collapse the sentinel back
    # to None exactly like the other write seams (content._org_for / security.create_user /
    # llmemory.write_org_id): a broken/unscoped principal stamps NULL, never -1. Real ids
    # pass through unchanged, so every legitimate caller is byte-identical.
    from app.services import access as _access

    org_id = _access.stampable_org_id(org_id)
    name = _inbox_name(department, actor)
    key = _inbox_key(department, actor)
    dept = (department or "").strip()
    # Re-key (SPEC-rename-delete): match on the STABLE inbox_key, never Project.name,
    # so a renamed inbox is still found (name is display-only now). A department inbox
    # is org-scoped when org_id is given (leak vector 11 — a shared department name
    # can't collide across orgs); the per-actor case matches on the actor-bearing key
    # itself (no org filter, preserving the original per-actor behaviour that never
    # org-scoped its lookup). The functional unique index (coalesce(org_id,-1),
    # inbox_key) is the DB backstop against a concurrent double-create.
    q = select(Project).where(Project.inbox_key == key)
    if dept and org_id is not None:
        q = q.where(Project.org_id == org_id)
    existing = session.exec(q).first()
    if existing:
        return existing
    proj = Project(
        name=name,
        inbox_key=key,
        source_locale=(settings.get("default_source_locale") or "en-US").strip() or "en-US",
        default_provider=config.DEFAULT_PROVIDER,
        created_by=actor,
        org_id=org_id,
    )
    session.add(proj)
    session.commit()
    session.refresh(proj)
    # #690 gate: a new inbox project starts with comment translation OFF (no row = a
    # project from before the option, which keeps Word comments on).
    from app.services import comment_option as _comment_option

    _comment_option.stamp_new_project(proj, session)
    return proj
