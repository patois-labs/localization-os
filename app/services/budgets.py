"""Per-department monthly translation budgets (MCP Phase B chunk 4).

A department may be capped at N translation tokens per calendar month. Spend is
the sum of tokens_in+tokens_out recorded on translate_document jobs attributed to
the department during the current month; check() gates a new submission before it
enqueues.

Attribution choice — by the STAMPED department (Finding 4):
  A translate_document Job records `created_by` (the submitting actor's email) and,
  since Phase-B-review, a `payload.department` stamped at SUBMIT time. Spend is
  attributed by that stamped department, so a mid-month department change never
  migrates a job's spend to the new department (the job already burned tokens
  against the department it was submitted under). Rationale:
    * The stamp is the SAME key budgets.check() gated on at submit time
      (routing.department_for(ident) == the actor's then-current User.department),
      so the check and the accounting can never disagree about which budget a job
      counts against — even after the user moves departments.
    * A routing rule can send a document to ANY project (not just the department
      inbox), and non-inbox projects carry no department tag — so "the doc's project
      == the department" is not robust, whereas the submit-time stamp always is.
  Fallback for jobs predating the stamp (no `payload.department` key): attribute by
  the actor's CURRENT User.department (created_by -> User.department), the pre-review
  behaviour. Auth-off / connector-token / no-department jobs have department "" and
  are never budgeted (check("") is always allowed) — existing installs see no change.

In-flight spend counts (Finding 2): to stop a burst of concurrent submissions from
all passing the check before any finishes, spend_this_month includes QUEUED/RUNNING
jobs' STAMPED `payload.est_tokens` estimate (source chars / 4, stamped at submit) in
addition to COMPLETED/FAILED jobs' real tokens_in+tokens_out. So N queued jobs whose
estimates already exceed the cap make the next submit 429 before any of them finish.

Failed jobs COUNT their REAL tokens: a FAILED translate_document job may still have
burned provider tokens before failing (partial translation), so its recorded
tokens_in/out are real spend. QUEUED/RUNNING jobs have no final stats yet, so their
est_tokens estimate stands in until they reach a terminal state and real stats
replace it.

Scale caveat: spend_this_month() loads this month's translate_document jobs and
sums their JSON `result`/`payload` token stats in Python. That is a bounded, O(jobs-
this-month) scan — fine at pilot scale (tens/hundreds of jobs a month). If job
volume ever grows large, denormalize the per-job token total into an indexed column
and SUM() it in SQL instead.

Documented in: docs/architecture/connectors.md
"""

from datetime import datetime

from sqlmodel import Session, select

from app.db import engine
from app.models import DepartmentBudget, Job, JobStatus, User
from app.services import access, audit


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


def _spend_scope(org_id):
    """The concrete org a spend sum is confined to, or None for "every org".

    #503: under an ACTIVE multi-org gate a department name is per-org (two orgs may both have
    a "Marketing"), so its spend must be too: org B's usage may never count against org A's
    cap, and org A's admin must not see org B's volume. UNSCOPED and a bare None mean "no
    narrowing" (the key-verified operator door, or a caller with no org to narrow to). With
    the gate OFF this is always None, whatever the budget row's stamp says, so a single-tenant
    install (whose rows are a mixture of NULL and the Default org) is byte-identical."""
    if not access.multi_org_active():
        return None
    return access.org_scope_value(org_id)


def _dept_emails(session: Session, department: str, org_id=None) -> set[str]:
    """Emails of every user in `department` (lower-cased for matching). With `org_id` (a
    concrete org, multi-org on) only that org's users count, so the legacy actor-membership
    fallback never attributes another org's job to this org's department (#503)."""
    dept = (department or "").strip().lower()
    if not dept:
        return set()
    users = session.exec(select(User)).all()
    return {
        (u.email or "").lower()
        for u in users
        if (u.department or "").strip().lower() == dept
        and u.email
        and (org_id is None or u.org_id == org_id)
    }


def _job_tokens(job: Job) -> int:
    """tokens_in + tokens_out recorded on a translate_document job's result."""
    res = job.result or {}
    try:
        return int(res.get("tokens_in", 0) or 0) + int(res.get("tokens_out", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _job_est_tokens(job: Job) -> int:
    """The estimated token cost stamped on a job's payload at submit (Finding 2),
    used for QUEUED/RUNNING jobs that have no final stats yet. 0 for pre-stamp jobs."""
    try:
        return int((job.payload or {}).get("est_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _job_department(job: Job, emails: set[str]) -> str:
    """The department a job counts against (Finding 4). The submit-time stamp
    (payload.department) is authoritative; jobs predating the stamp fall back to
    'does this job's actor currently belong to `department`?' via the emails set."""
    stamped = ((job.payload or {}).get("department") or "").strip().lower()
    if stamped:
        return stamped
    # legacy fallback: attribute by the actor's CURRENT department membership
    return "_current_member" if (job.created_by or "").lower() in emails else ""


def _job_in_org(job: Job, org_id: int, user_org: dict) -> bool:
    """Does this job belong to `org_id`? A Job has no org column: a real user's job is that
    user's org (authoritative, even when the user's org is NULL, which is then never `org_id`);
    a pseudo-actor's job (webhook, scheduler) resolves from the content it references, exactly
    as jobs.job_org_id does. Unresolvable means foreign (fail closed)."""
    actor = (job.created_by or "").strip().lower()
    if actor in user_org:
        return user_org[actor] == org_id
    from app.services import jobs as _jobs

    return _jobs.job_org_id(job) == org_id


def spend_this_month(
    session: Session, department: str, now: datetime | None = None, org_id=None
) -> int:
    """Total translation tokens spent by `department` this calendar month.

    `org_id` (#503): under multi-org only that org's jobs and users count (see _spend_scope);
    None, or the gate off, sums the whole install as before.

    Sums, for jobs created on/after the first of the current month and attributed
    to `department` (by the submit-time stamp, else by the actor's current
    membership — see _job_department):
      * COMPLETED/FAILED jobs -> real tokens_in+tokens_out (FAILED count: partial
        translations really burned tokens).
      * QUEUED/RUNNING jobs -> the stamped est_tokens estimate (in-flight spend,
        Finding 2), so a burst of pending jobs can exhaust the budget before any
        completes.
    Blank department -> 0 (never budgeted)."""
    dept = (department or "").strip().lower()
    if not dept:
        return 0
    scope = _spend_scope(org_id)
    emails = _dept_emails(session, dept, scope)
    user_org: dict = {}
    if scope is not None:
        user_org = {
            (u.email or "").strip().lower(): u.org_id
            for u in session.exec(select(User)).all()
            if u.email
        }
    since = _month_start(now)
    # Bounded scan: this month's translate_document jobs (see module caveat).
    jobs = session.exec(
        select(Job).where(
            Job.kind == "translate_document",
            Job.created_at >= since,
            Job.status.in_(
                [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.QUEUED, JobStatus.RUNNING]
            ),
        )
    ).all()
    total = 0
    for j in jobs:
        job_dept = _job_department(j, emails)
        # A stamped job matches its stamped department; a legacy (unstamped) job
        # matches via the "_current_member" sentinel (its actor is in `dept` now).
        if job_dept not in (dept, "_current_member"):
            continue
        if scope is not None and not _job_in_org(j, scope, user_org):
            continue
        if j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            total += _job_tokens(j)
        else:  # QUEUED / RUNNING -> estimate
            total += _job_est_tokens(j)
    return total


def get_budget(session: Session, department: str, org_id=None) -> DepartmentBudget | None:
    """The DepartmentBudget row for a department (case-insensitive), or None.

    `org_id`, when given (a scoped caller's org in multi-org mode — leak vector
    12), restricts the match to that org's budget rows so a department name shared
    across orgs can't make org A's cap gate org B's submissions; None (the gate
    off) matches any org's row, byte-identical to today."""
    dept = (department or "").strip().lower()
    if not dept:
        return None
    q = select(DepartmentBudget)
    if org_id is not None:
        q = q.where(DepartmentBudget.org_id == org_id)
    for b in session.exec(q).all():
        if (b.department or "").strip().lower() == dept:
            return b
    return None


# --- shared bound check (task_cca6bb89: huge-int budget value -> 500) --------
# DepartmentBudget.monthly_token_limit is a plain SQLite INTEGER column. Python's
# int() has no overflow ceiling, so a fat-fingered or malicious value (e.g. a stray
# extra zero, or "999999999999999999999999999999") parses fine at the form/CLI
# layer and only blows up at the DB bind: sqlite3 raises a raw OverflowError
# ("Python int too large to convert to SQLite INTEGER") for anything outside a
# signed 64-bit range, which every uncaught caller turned into a raw 500 (web) or
# an unhandled traceback (CLI). MAX_BUDGET_LIMIT is a generous sane ceiling --
# 1 quadrillion tokens/month, comfortably beyond any real installation's spend but
# safely inside SQLite's ~9.22e18 int64 ceiling (a huge margin so this never
# collides with a legitimate huge cap) -- enforced in upsert_budget() itself so
# EVERY seam that stores a limit (web form, CLI, and any future /api route) is
# protected even if a caller forgets to validate first.
MAX_BUDGET_LIMIT = 10**15


class BudgetLimitError(ValueError):
    """A monthly_token_limit value outside the sane bound. Callers (the web route,
    the CLI verb) catch this and surface their own clean error (a redirect banner /
    CliError) instead of letting the raw OverflowError reach the user as a 500."""


def validate_limit(monthly_token_limit: int) -> int:
    """The one shared bound check for every seam that accepts a budget/cap number.
    Returns `monthly_token_limit` unchanged when it is within [-MAX_BUDGET_LIMIT,
    MAX_BUDGET_LIMIT]; raises BudgetLimitError with a friendly message otherwise.
    Negative values are already meaningless (<=0 means unlimited) but are bounded
    too, since an absurd negative magnitude would ALSO overflow the SQLite bind."""
    if abs(monthly_token_limit) > MAX_BUDGET_LIMIT:
        raise BudgetLimitError(
            f"Monthly token limit is too large (must be between "
            f"-{MAX_BUDGET_LIMIT:,} and {MAX_BUDGET_LIMIT:,})."
        )
    return monthly_token_limit


# --- budget CRUD (extracted from app/web/routing.py so route + CLI share) -----
# The /routing/budgets POST body moved here VERBATIM: one row per department,
# saving an existing department updates it in place (upsert). A limit <= 0 means
# unlimited (the row is kept but ignored by check). The route still owns the HTTP
# concerns (admin gate, CSRF, the "Department is required" redirect + the int
# coercion of the form field); this owns the DB mutation + audit.
def upsert_budget(
    session: Session,
    department: str,
    monthly_token_limit: int,
    actor="system",
    ip: str = "",
    org_id=None,
) -> tuple[DepartmentBudget, bool]:
    """Create-or-update one department's monthly token cap. Returns (row, created):
    created is True when a new DepartmentBudget row was inserted, False when an
    existing department's limit was updated in place. Audits budget.create /
    budget.update exactly as the route did.

    `org_id`, when given (the creating admin's org in multi-org mode), scopes the
    upsert to that org's rows so each org keeps its OWN row for a shared department
    name, and stamps a newly-created row; None (the gate off) is single-tenant,
    byte-identical to today.

    Raises BudgetLimitError (never reaches the DB bind, never a raw 500/crash) when
    `monthly_token_limit` is outside the sane bound (task_cca6bb89) -- validated
    HERE so every caller is protected even if it forgot to pre-validate."""
    monthly_token_limit = validate_limit(monthly_token_limit)
    dept = (department or "").strip()
    existing = get_budget(session, dept, org_id=org_id)
    if existing:
        existing.monthly_token_limit = monthly_token_limit
        session.add(existing)
        session.commit()
        session.refresh(existing)
        row, created, action = existing, False, "budget.update"
    else:
        row = DepartmentBudget(
            department=dept,
            monthly_token_limit=monthly_token_limit,
            created_by=actor,
            org_id=org_id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        created, action = True, "budget.create"
    audit.log(
        actor=actor,
        action=action,
        target=f"budget {row.id}",
        detail=f"dept={dept} limit={monthly_token_limit}",
        ip=ip,
    )
    return row, created


def delete_budget(session: Session, budget_id: int, actor="system", ip: str = "") -> bool:
    """Delete a department budget by id and audit it. Returns True if a row was
    removed, False when the id did not exist (a harmless no-op, as the route
    treated it)."""
    row = session.get(DepartmentBudget, budget_id)
    if row is None:
        return False
    dept = row.department
    session.delete(row)
    session.commit()
    audit.log(
        actor=actor,
        action="budget.delete",
        target=f"budget {budget_id}",
        detail=f"dept={dept}",
        ip=ip,
    )
    return True


def department_for_actor(session: Session, email: str) -> str:
    """Best-effort department lookup by a plain actor EMAIL (T4-02 CLI parity).

    The web/API surfaces carry a rich `ident` dict (routing.department_for reads
    `ident["user"].department`); the in-process CLI's `--actor` flag is just a
    free-text email for audit attribution, not an authenticated identity. This
    mirrors routing.department_for for that case: "" (never budgeted) when the
    email is blank or matches no User row — exactly like an API key with no
    bound user, or auth-off single-operator mode."""
    email = (email or "").strip().lower()
    if not email:
        return ""
    user = session.exec(select(User).where(User.email == email)).first()
    return (getattr(user, "department", "") or "").strip()


# The message a caller shows when a department's monthly budget is used up.
EXHAUSTED_MESSAGE = "Your department's translation budget for this month is used up."


def check(department: str, now: datetime | None = None, org_id=None) -> tuple[bool, int, str]:
    """Gate a new translation submission for `department`.

    Returns (allowed, remaining, message):
      * no department / no budget row / limit <= 0  -> (True, -1, "")  UNLIMITED
      * spend < limit                                -> (True, remaining, "")
      * spend >= limit                               -> (False, 0, EXHAUSTED_MESSAGE)

    `remaining` is -1 for "unlimited", else max(0, limit - spend). Opens its own
    session so callers (the API handler, tests) need not thread one through.

    `org_id`, when given (the submitting identity's org in multi-org mode — leak
    vector 12), scopes the budget lookup to that org so org A's cap never gates
    org B's submission; None (the gate off) is single-tenant, byte-identical to
    today."""
    dept = (department or "").strip()
    if not dept:
        return True, -1, ""
    with Session(engine) as s:
        budget = get_budget(s, dept, org_id=org_id)
        if budget is None or budget.monthly_token_limit <= 0:
            return True, -1, ""
        spent = spend_this_month(s, dept, now, org_id=org_id)
        remaining = budget.monthly_token_limit - spent
        if remaining <= 0:
            return False, 0, EXHAUSTED_MESSAGE
        return True, remaining, ""


# --- cross-process reservation (#622) ------------------------------------------
# check() above is a plain read. jobs.py serializes check + Job insert under an in-process
# lock, which stops threads of ONE process racing but not two app processes or workers
# (LOC_TMS_JOB_WORKERS=4 in production): each sees the same remaining budget, each inserts.
# The functions below move the serialization into the database. A reservation opens ONE
# session, takes a write lock on the budget rows as its FIRST statement, then reads the
# spend and (in the caller) inserts the Job in that same transaction. A competing process
# blocks on the lock until the winner commits, then reads the winner's committed Job, so
# exactly one of two racers for the last of a budget gets it.
#   * Postgres: SELECT ... ORDER BY id FOR UPDATE on the DepartmentBudget rows (the id
#     order keeps two reservations from deadlocking on each other).
#   * SQLite: a no-op UPDATE of those rows. It must be the transaction's first statement:
#     the writer lock is taken before any read, so there is no stale-snapshot upgrade
#     failure, and the 5 s busy_timeout makes the loser wait instead of erroring.
# Budget rows are one per department (a handful per install), so every reservation locks all of
# them: no org narrowing, which also keeps the lock independent of the multi-org scope helpers.


def has_cap(department: str, org_id=None) -> bool:
    """True when `department` has a budget row with a positive limit (so a reservation must
    take the lock). Unbudgeted departments and auth-off callers skip it entirely."""
    dept = (department or "").strip()
    if not dept:
        return False
    with Session(engine) as s:
        budget = get_budget(s, dept, org_id=org_id)
        return budget is not None and budget.monthly_token_limit > 0


def lock_budget_rows(session: Session) -> None:
    """Take the database-level reservation lock. Must be the FIRST statement of the
    session's transaction (see the block comment above); the lock lasts until the caller
    commits or rolls back."""
    from sqlalchemy import update

    if session.get_bind().dialect.name == "postgresql":
        session.exec(
            select(DepartmentBudget.id).order_by(DepartmentBudget.id).with_for_update()
        ).all()
        return
    session.execute(
        update(DepartmentBudget).values(monthly_token_limit=DepartmentBudget.monthly_token_limit)
    )


def check_locked(
    session: Session, department: str, now: datetime | None = None, org_id=None
) -> tuple[bool, int, str]:
    """check() inside a caller-owned session that holds the reservation lock. The caller
    inserts its Job in the SAME session and commits (or rolls back), which releases the lock.
    `session` must be fresh: nothing may have been read or written on it yet. Same return
    contract as check(). Fails closed: a department with a positive cap is always checked
    under the lock."""
    dept = (department or "").strip()
    if not dept or not has_cap(dept, org_id=org_id):
        return True, -1, ""
    lock_budget_rows(session)
    budget = get_budget(session, dept, org_id=org_id)
    if budget is None or budget.monthly_token_limit <= 0:
        return True, -1, ""
    remaining = budget.monthly_token_limit - spend_this_month(session, dept, now, org_id=org_id)
    if remaining <= 0:
        return False, 0, EXHAUSTED_MESSAGE
    return True, remaining, ""
