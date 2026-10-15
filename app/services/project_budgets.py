"""Per-PROJECT monthly spend budgets in MONEY (requirement CV-2).

The sibling of app.services.budgets (per-DEPARTMENT monthly TOKEN caps). Where the
department cap counts translate_document tokens, this caps a single project's USD
SPEND per calendar month — the money picture the token cap cannot see, and the first
budget that counts BOTH AI model spend AND vendor human spend.

SPEND MODEL (what counts toward a project's monthly figure) — attributed by
document -> project, in the current calendar month, honouring the reports.py cost
seams VERBATIM (never fake $0 for untracked spend):

  ENFORCED (gated at 100%):
    * AI model spend
        - translate_document / connector_translate / backfill_embeddings Jobs ->
          reports._job_cost
          (created this month, whose _job_project_id resolves to this project);
        - workflow StepRun.cost_usd (runs of this project's documents whose run-date
          — finished_at else started_at — falls in the month). A StepRun with a NULL
          cost is UNTRACKED (legacy / pending), contributing nothing (never a fake $0).
    * SETTLED vendor human spend — the vendor_spend 'actual' grade (actual_cost_usd
      reconciled on a delivered/returned leg with no outstanding tranches), attributed
      to the project and dated in the month by returned_at (else dispatched_at).

  COMMITTED (DISPLAYED as a second meter marker, NOT enforced — an overridable
  Maintainer-default: a commitment is not yet a bill):
    * the vendor_spend 'estimate' / 'quote' grade on still-OPEN legs (DISPATCHED /
      RETRY_WAIT / QUOTED / ESCALATED), dated by dispatched_at in the month. A
      'declined' quote prices $0 (the operator refused the price) and never shows.

ENFORCEMENT: project_check() gates a NEW submission BEFORE it enqueues at the same
three MT seams the department cap uses (web api / web documents / CLI doc translate)
plus the vendor-dispatch creation seam; at 100% of monthly_usd_limit it refuses with
the same clean-error posture (a 429 / CliError / policy refusal). Human review and
editing are NEVER blocked.

THRESHOLD ALERTS (maintainer-default bands 80% + 100%): evaluate_thresholds() fires a
budget.threshold audit row + an admin email ONCE per crossing per month, guarded by
the ProjectBudget row's persisted (notified_period, notified_pct) so a burst of
submissions cannot re-send. It is called at the spend-affecting chokepoints (each MT
submission seam, the vendor-dispatch seam, and the job-finished worker hook); being
idempotent against the persisted state, calling it "too often" is harmless — it only
ever fires on a genuinely new crossing.

Scale caveat (same posture as budgets.py): the AI-job leg loads this month's
translate/connector Jobs and prices them in Python — a bounded O(jobs-this-month)
scan, fine at pilot scale. The workflow + vendor legs are project-scoped selects.

Documented in: docs/architecture/cost-and-budgets.md
"""

from datetime import datetime

from sqlmodel import Session, select

from app.db import engine
from app.models import (
    Connection,
    Document,
    Job,
    ProjectBudget,
    StepRun,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
)
from app.services import audit, ids

# The Job kinds that write a priced Job row (workflow runs write StepRun rows, never
# Jobs; see reports._workflow_run_buckets). Matches the reports cost attribution so a
# project's AI-job spend reconciles with its cost report. backfill_embeddings (#364 gate):
# its stored Job.cost_usd is the embedder's real booked spend, which the cost report
# counts for a project-scoped backfill, so the meter counts it too (data_export books $0).
_COST_JOB_KINDS = ("translate_document", "connector_translate", "backfill_embeddings")

# The open (in-flight) leg statuses whose estimate/quote is a COMMITTED-but-unpaid
# marker on the meter. RETURNED/REJECTED are terminal (their settled actual, if any,
# is already ENFORCED via the 'actual' grade); CANCELLED/DEAD_LETTER were never billed.
_OPEN_LEG_STATUSES = frozenset(
    {
        VendorDispatchStatus.DISPATCHED,
        VendorDispatchStatus.RETRY_WAIT,
        VendorDispatchStatus.QUOTED,
        VendorDispatchStatus.ESCALATED,
    }
)

# Threshold bands (maintainer-default) — crossing either alerts once per month.
THRESHOLD_BANDS = (80, 100)

# The message a caller shows when a project's monthly spend budget is used up.
EXHAUSTED_MESSAGE = "This project's spend budget for this month is used up."

# A generous sane ceiling on a USD limit (mirrors budgets.MAX_BUDGET_LIMIT's intent:
# reject a fat-fingered/hostile magnitude before it reaches the DB). $1 trillion/month
# is far beyond any real project and safely finite (also rejects NaN/inf).
MAX_USD_LIMIT = 1_000_000_000_000.0


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


def _next_month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)


def _period_key(now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    return f"{now.year:04d}-{now.month:02d}"


def _in_month(ts, lo: datetime, hi: datetime) -> bool:
    """True when a naive-UTC datetime falls in [lo, hi) (the calendar month). A None
    or non-datetime value fails closed (excluded)."""
    return isinstance(ts, datetime) and lo <= ts < hi


class BudgetLimitError(ValueError):
    """A monthly_usd_limit outside the sane bound (or NaN/inf). Callers catch this and
    surface their own clean error (redirect banner / CliError / 400) rather than a 500."""


def validate_usd_limit(monthly_usd_limit: float) -> float:
    """The one shared bound check for every seam that accepts a USD cap. Returns the
    value unchanged when finite and within [-MAX_USD_LIMIT, MAX_USD_LIMIT]; raises
    BudgetLimitError otherwise. Negative is already meaningless (<= 0 = unlimited) but
    bounded too. NaN/inf are rejected (they would poison every comparison)."""
    try:
        v = float(monthly_usd_limit)
    except (TypeError, ValueError) as e:
        raise BudgetLimitError("Monthly USD limit must be a number.") from e
    if v != v or v in (float("inf"), float("-inf")):
        raise BudgetLimitError("Monthly USD limit must be a finite number.")
    if abs(v) > MAX_USD_LIMIT:
        raise BudgetLimitError(
            f"Monthly USD limit is too large (must be between "
            f"-{MAX_USD_LIMIT:,.0f} and {MAX_USD_LIMIT:,.0f})."
        )
    return v


# --- CRUD --------------------------------------------------------------------
def _project_org_id(session: Session, project_id) -> int | None:
    """The org a project belongs to (the tenant anchor for its budget). None when the
    project is missing or org-less (single-tenant)."""
    from app.models import Project

    proj = session.get(Project, project_id)
    return getattr(proj, "org_id", None) if proj is not None else None


def get_project_budget(session: Session, project_id) -> ProjectBudget | None:
    """The ProjectBudget row for a project, or None. Keyed on the UNIQUE project_id
    ALONE — the ORG is a property of the project, never of the caller (H2 fix). One
    project has exactly one budget row, so enforcement gives the SAME answer at every
    seam regardless of who asks or what org they carry; a caller can never disable a
    cap by presenting a mismatched org. Admin LIST surfaces still org-filter the roster
    (via access.org_filter on the ProjectBudget query) for visibility — that is a
    separate concern from enforcement, and coherent now that row.org_id == project.org_id."""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return None
    return session.exec(select(ProjectBudget).where(ProjectBudget.project_id == pid)).first()


def upsert_project_budget(
    session: Session, project_id, monthly_usd_limit: float, actor="system", ip: str = ""
) -> tuple[ProjectBudget, bool]:
    """Create-or-update one project's monthly USD cap. Returns (row, created). Audits
    budget.create / budget.update. Raises BudgetLimitError (never a raw 500) on a bad
    limit — validated HERE so every seam is protected even if it forgot to pre-check.

    The row's org is stamped from the PROJECT (project.org_id), never the caller (H2),
    so the row's tenant always matches its project. A limit <= 0 keeps the row but means
    UNLIMITED (mirrors DepartmentBudget). When the limit CHANGES, the threshold-alert
    state is RESET (M4) so raising the cap re-arms the 80/100 alerts for a re-crossing
    (a stale notified_pct=100 would otherwise latch alerts off for the month)."""
    monthly_usd_limit = validate_usd_limit(monthly_usd_limit)
    pid = int(project_id)
    org_id = _project_org_id(session, pid)
    existing = get_project_budget(session, pid)
    now = datetime.utcnow()
    if existing:
        if existing.monthly_usd_limit != monthly_usd_limit:
            # M4: a changed cap re-arms alerts (the usage percentage just moved).
            existing.notified_period = None
            existing.notified_pct = 0
        existing.monthly_usd_limit = monthly_usd_limit
        existing.org_id = org_id  # keep the row's org synced to its project
        existing.updated_at = now
        session.add(existing)
        session.commit()
        session.refresh(existing)
        row, created, action = existing, False, "budget.update"
    else:
        row = ProjectBudget(
            project_id=pid,
            monthly_usd_limit=monthly_usd_limit,
            created_by=actor,
            org_id=org_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        created, action = True, "budget.create"
    audit.log(
        actor=actor,
        action=action,
        target=f"project-budget {row.id}",
        detail=f"project={pid} usd_limit={monthly_usd_limit}",
        ip=ip,
    )
    return row, created


def delete_project_budget(session: Session, budget_id, actor="system", ip: str = "") -> bool:
    """Delete a project budget by id and audit it. Returns True if a row was removed,
    False when the id did not exist (a harmless no-op)."""
    row = session.get(ProjectBudget, budget_id)
    if row is None:
        return False
    pid = row.project_id
    session.delete(row)
    session.commit()
    audit.log(
        actor=actor,
        action="budget.delete",
        target=f"project-budget {budget_id}",
        detail=f"project={pid}",
        ip=ip,
    )
    return True


# --- spend accounting --------------------------------------------------------
def _project_document_ids(session: Session, project_id: int) -> list[int]:
    return list(session.exec(select(Document.id).where(Document.project_id == project_id)).all())


def _ai_job_spend(
    session: Session, project_id: int, doc_ids: list[int], lo: datetime, hi: datetime
) -> float:
    """USD of this project's cost-bearing Jobs dated in the month, priced by
    reports._job_cost (the SAME pricing the cost report uses, so the figure reconciles).

    L1: a job is DATED by reports._job_date (finished_at else created_at) — the exact
    convention the cost report buckets on. The SQL prefilter widens to (finished_at >= lo)
    OR (created_at >= lo) and the precise bound is the Python _job_date check.

    ATTRIBUTION IS BOUND-SAFE (gate finding): a job attributes to this project when its
    payload/result names one of THIS project's document ids, this project's connection
    ids, or this project_id directly. We test MEMBERSHIP in precomputed id SETS via
    ids.coerce_id (which returns None for an out-of-range value SQLite would overflow on)
    rather than reports._job_project_id, whose session.get(Document, doc_id) raises
    OverflowError on a job carrying a huge/hostile document_id (e.g. a refusal-probe job)
    — a query that runs on EVERY project-page load, so one such job anywhere would 422 the
    page. Membership-by-set never binds a foreign id to the DB, so it cannot overflow."""
    from app.services import reports

    doc_set = set(doc_ids)
    conn_ids = set(
        session.exec(select(Connection.id).where(Connection.project_id == project_id)).all()
    )
    jobs = session.exec(
        select(Job).where(
            Job.kind.in_(_COST_JOB_KINDS), (Job.finished_at >= lo) | (Job.created_at >= lo)
        )
    ).all()
    total = 0.0
    for j in jobs:
        ts = j.finished_at or j.created_at
        if not _in_month(ts, lo, hi):
            continue
        if _job_attributes_to(j, project_id, doc_set, conn_ids):
            total += reports._job_cost(j)
    return total


def _job_attributes_to(job: Job, project_id: int, doc_set: set, conn_ids: set) -> bool:
    """FIRST-HIT attribution (N4): does this job attribute to `project_id`? The keys are
    tried in the SAME precedence order as reports._job_project_id — payload.project_id,
    then payload.document_id, then payload.connection_id, then result.document_id — and
    the FIRST present key decides (a later key never overrides it). So a job carrying two
    conflicting keys is attributed exactly ONCE, never double-counted, and the
    reconciliation-with-reports claim holds unconditionally. Membership is tested against
    precomputed id SETS via ids.coerce_id (None for an out-of-range value), so a hostile
    id never reaches a session.get / DB bind (the OverflowError-that-422s-the-page bug)."""
    payload, result = (job.payload or {}), (job.result or {})
    pid = ids.coerce_id(payload.get("project_id"))
    if pid is not None:
        return pid == project_id
    did = ids.coerce_id(payload.get("document_id"))
    if did is not None:
        return did in doc_set
    cid = ids.coerce_id(payload.get("connection_id"))
    if cid is not None:
        return cid in conn_ids
    rdid = ids.coerce_id(result.get("document_id"))
    if rdid is not None:
        return rdid in doc_set
    return False


def job_project_id_safe(session: Session, job: Job) -> int | None:
    """The project a single job attributes to, resolved BOUND-SAFELY (N5) — the same
    precedence chain as reports._job_project_id, but every id is passed through
    ids.coerce_id FIRST so an out-of-range value becomes None and NO session.get ever
    binds a too-large id (reports._job_project_id OverflowErrors on such a job, which
    silently killed the job-finish threshold hook on hostile ids). Used by the async
    job-finish chokepoint, which has one job in hand and no precomputed id sets."""
    if job is None:
        return None
    payload, result = (job.payload or {}), (job.result or {})
    pid = ids.coerce_id(payload.get("project_id"))
    if pid is not None:
        return pid
    did = ids.coerce_id(payload.get("document_id"))
    if did is not None:
        doc = session.get(Document, did)
        if doc is not None:
            return doc.project_id
    cid = ids.coerce_id(payload.get("connection_id"))
    if cid is not None:
        conn = session.get(Connection, cid)
        if conn is not None and conn.project_id is not None:
            return conn.project_id
    rdid = ids.coerce_id(result.get("document_id"))
    if rdid is not None:
        doc = session.get(Document, rdid)
        if doc is not None:
            return doc.project_id
    return None


def _ai_workflow_spend(session: Session, doc_ids: list[int], lo: datetime, hi: datetime) -> float:
    """USD of workflow StepRun.cost_usd for runs of this project's documents whose
    run-date (finished_at else started_at) is in the month. NULL costs are UNTRACKED
    and contribute nothing (never a fake $0)."""
    if not doc_ids:
        return 0.0
    runs = session.exec(
        select(WorkflowRun.id, WorkflowRun.finished_at, WorkflowRun.started_at).where(
            WorkflowRun.document_id.in_(doc_ids)
        )
    ).all()
    run_ids = [r.id for r in runs if _in_month(r.finished_at or r.started_at, lo, hi)]
    if not run_ids:
        return 0.0
    total = 0.0
    for _rid, cost in session.exec(
        select(StepRun.workflow_run_id, StepRun.cost_usd).where(
            StepRun.workflow_run_id.in_(run_ids)
        )
    ).all():
        if cost is not None:
            total += float(cost)
    return total


def _advanced_run_spend(session: Session, project_id: int, lo: datetime, hi: datetime) -> float:
    """USD of Advanced Workflows run spend (AdvancedRun.spent_usd) for this project, dated by
    the run's finished_at else started_at -- the same convention as a workflow run (#357).

    Advanced runs write NEITHER Job nor StepRun rows (their spend lives only on the
    AdvancedRun row and the install-wide LLM ledger), so this leg cannot double count the two
    above. Attributed by the run's own project_id (stamped from the mission at launch), never by
    a document, so another project's runs never leak in. An in-flight run counts its live
    spent_usd, exactly as a still-running workflow's StepRun costs do."""
    from app.models import AdvancedRun

    total = 0.0
    for spent, finished_at, started_at in session.exec(
        select(AdvancedRun.spent_usd, AdvancedRun.finished_at, AdvancedRun.started_at).where(
            AdvancedRun.project_id == project_id
        )
    ).all():
        if spent and _in_month(finished_at or started_at, lo, hi):
            total += float(spent)
    return total


def _project_legs(
    session: Session, project_id: int, doc_ids: list[int], run_ids: list[int]
) -> list[VendorDispatch]:
    """Every vendor dispatch leg attributable to this project — by workflow run (the
    reliable run->document->project path) OR directly by the leg's own document_id —
    de-duplicated by leg id."""
    legs: dict[int, VendorDispatch] = {}
    if run_ids:
        for leg in session.exec(
            select(VendorDispatch).where(VendorDispatch.workflow_run_id.in_(run_ids))
        ).all():
            legs[leg.id] = leg
    if doc_ids:
        for leg in session.exec(
            select(VendorDispatch).where(VendorDispatch.document_id.in_(doc_ids))
        ).all():
            legs[leg.id] = leg
    return list(legs.values())


def project_spend_this_month(session: Session, project_id, now: datetime | None = None) -> dict:
    """The money breakdown for one project's current calendar month. Returns:
        {ai_usd, human_settled_usd, enforced_usd, committed_usd, total_display_usd}
    where enforced_usd = ai_usd + human_settled_usd (what project_check gates on) and
    total_display_usd = enforced_usd + committed_usd (the meter's outer figure)."""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return {
            "ai_usd": 0.0,
            "human_settled_usd": 0.0,
            "enforced_usd": 0.0,
            "committed_usd": 0.0,
            "total_display_usd": 0.0,
        }
    from app.services import vendor_spend

    lo, hi = _month_start(now), _next_month_start(now)
    doc_ids = _project_document_ids(session, pid)

    ai_usd = (
        _ai_job_spend(session, pid, doc_ids, lo, hi)
        + _ai_workflow_spend(session, doc_ids, lo, hi)
        + _advanced_run_spend(session, pid, lo, hi)
    )

    # All runs of the project's documents (for leg attribution — not month-limited
    # here; each leg is month-filtered by its OWN date below).
    all_run_ids = (
        list(session.exec(select(WorkflowRun.id).where(WorkflowRun.document_id.in_(doc_ids))).all())
        if doc_ids
        else []
    )
    human_settled = 0.0
    committed = 0.0
    for leg in _project_legs(session, pid, doc_ids, all_run_ids):
        cost, grade = vendor_spend.leg_human_cost(leg)
        if grade == "actual":
            # Settled bill — dated by when it settled (returned_at), else dispatch.
            when = getattr(leg, "returned_at", None) or getattr(leg, "dispatched_at", None)
            if _in_month(when, lo, hi):
                human_settled += cost
        elif grade in ("estimate", "quote") and (leg.status or "") in _OPEN_LEG_STATUSES:
            # Committed-but-unpaid — dated by dispatch, DISPLAY only (not enforced).
            if _in_month(getattr(leg, "dispatched_at", None), lo, hi):
                committed += cost
        # 'declined' -> $0, ignored.

    ai_usd = round(ai_usd, 6)
    human_settled = round(human_settled, 6)
    committed = round(committed, 6)
    enforced = round(ai_usd + human_settled, 6)
    return {
        "ai_usd": ai_usd,
        "human_settled_usd": human_settled,
        "enforced_usd": enforced,
        "committed_usd": committed,
        "total_display_usd": round(enforced + committed, 6),
    }


# --- enforcement -------------------------------------------------------------
def project_check(project_id, now: datetime | None = None) -> tuple[bool, float, str]:
    """Gate a NEW machine-translate submission (or a new vendor dispatch) for a
    project. Returns (allowed, remaining_usd, message):
      * no project / no budget row / limit <= 0 -> (True, -1.0, "")  UNLIMITED
      * enforced spend < limit                  -> (True, remaining, "")
      * enforced spend >= limit                 -> (False, 0.0, EXHAUSTED_MESSAGE)

    Opens its own session (callers need not thread one). Keyed on the project's UNIQUE
    id — NOT the caller's org (H2): one project, one budget, one enforcement answer at
    every seam. Only ENFORCED spend gates; committed-but-unpaid is never enforced."""
    if project_id is None:
        return True, -1.0, ""
    with Session(engine) as s:
        budget = get_project_budget(s, project_id)
        if budget is None or budget.monthly_usd_limit <= 0:
            return True, -1.0, ""
        spent = project_spend_this_month(s, project_id, now)["enforced_usd"]
        remaining = budget.monthly_usd_limit - spent
        if remaining <= 0:
            return False, 0.0, EXHAUSTED_MESSAGE
        return True, round(remaining, 6), ""


# --- shared render state (one source of truth for every surface) -------------
def _clamp_pct(v: float) -> float:
    return 0.0 if v < 0 else (100.0 if v > 100 else v)


def budget_state(session: Session, project_id, now: datetime | None = None) -> dict:
    """The ONE render-ready spend/budget summary every surface (web /routing, the
    project meter, /api, CLI) reads, so they all show the SAME numbers. Returns:

      has_budget          a ProjectBudget row exists
      budget_id           its id (or None)
      monthly_usd_limit   the cap (float; <= 0 = unlimited)
      unlimited           limit <= 0 (kept but not enforced)
      ai_usd/human_settled_usd/enforced_usd/committed_usd/total_display_usd
                          the month's money breakdown (project_spend_this_month)
      pct                 enforced / limit * 100 (0 when unlimited)
      fill_pct            pct floored+clamped to [0,100] (the meter bar fill)
      committed_tick_pct  (enforced+committed)/limit clamped, or None when there is
                          no committed spend / no limit (the second meter marker)
      remaining_usd       max(0, limit - enforced) (0 when unlimited-> not shown)
      over                enforced >= limit (cap reached)
      figure              the visible headline string ("$X / $Y")
      aria                the full screen-reader sentence

    Always safe to call: with no row it returns has_budget=False + the spend it CAN
    see (so a surface can still show untracked money honestly). Keyed on project_id
    (not caller org, H2)."""
    budget = get_project_budget(session, project_id)
    spend = project_spend_this_month(session, project_id, now)
    enforced = spend["enforced_usd"]
    committed = spend["committed_usd"]
    limit = float(budget.monthly_usd_limit) if budget else 0.0
    unlimited = (budget is None) or (limit <= 0)
    pct = 0.0 if unlimited else (100.0 * enforced / limit)
    fill = 0.0 if unlimited else _clamp_pct(pct)
    tick = None
    if not unlimited and committed > 0:
        tick = _clamp_pct(100.0 * (enforced + committed) / limit)
    over = (not unlimited) and enforced >= limit
    if unlimited:
        figure = f"${enforced:,.2f} spent (no cap)"
        aria = (
            f"Project spend this month ${enforced:,.2f}; no monthly budget set."
            if budget is None
            else f"Project spend this month ${enforced:,.2f}; budget set to unlimited."
        )
    else:
        figure = f"${enforced:,.2f} / ${limit:,.2f}"
        aria = (
            f"Project spend this month ${enforced:,.2f} of ${limit:,.2f} "
            f"({pct:.0f}%)"
            + (f", plus ${committed:,.2f} committed" if committed > 0 else "")
            + (" — over budget" if over else "")
            + "."
        )
    return {
        "has_budget": budget is not None,
        "budget_id": (budget.id if budget else None),
        "monthly_usd_limit": limit,
        "unlimited": unlimited,
        "ai_usd": spend["ai_usd"],
        "human_settled_usd": spend["human_settled_usd"],
        "enforced_usd": enforced,
        "committed_usd": committed,
        "total_display_usd": spend["total_display_usd"],
        "pct": round(pct, 2),
        "pct_label": ("—" if unlimited else f"{pct:.0f}%"),
        "fill_pct": round(fill, 2),
        "committed_tick_pct": (round(tick, 2) if tick is not None else None),
        "remaining_usd": (0.0 if unlimited else round(max(0.0, limit - enforced), 6)),
        "over": over,
        "figure": figure,
        "aria": aria,
    }


# --- threshold alerts --------------------------------------------------------
def _band_for_pct(pct: float) -> int:
    """The highest crossed threshold band for a usage percentage (0 = none crossed)."""
    crossed = 0
    for b in THRESHOLD_BANDS:
        if pct >= b:
            crossed = b
    return crossed


def evaluate_thresholds(
    project_id, actor="system", ip: str = "", now: datetime | None = None
) -> int | None:
    """Fire a budget.threshold audit row + admin email if the project has crossed a
    NEW threshold band this month. Idempotent against the row's persisted
    (notified_period, notified_pct): a band is alerted at most once per calendar month,
    and the state resets when the period rolls over. Returns the band alerted (80/100)
    or None when nothing new crossed / no enforceable budget.

    Called at EVERY spend-affecting chokepoint, INCLUDING the blocking submission that
    is refused at 100% (H1: the block IS the moment the 100% band is crossed — the
    caller evaluates BEFORE returning its 429), and the job-finish worker hook (spend
    accrues asynchronously, so a crossing can occur with no new submission at all).

    OWN SESSION (M3): opens its own Session(engine) rather than borrowing the caller's,
    so the crossing state is committed durably even when the caller later rolls back
    (a rolled-back submission that nonetheless pushed real spend over the line must not
    lose its alert), and the caller's own pending writes are never force-committed here.
    Not org-scoped: the project id is the anchor (one budget per project).

    MAIL-FAILURE POSTURE (L8): the crossing state is persisted FIRST and the audit row
    is written, so the crossing is CONSUMED (never re-fires) and the AUDIT ROW is the
    durable record of it; the admin email is best-effort on top — a mail failure is
    logged and swallowed, never re-tried and never unwinds the consumed crossing.

    CONCURRENCY (N1): the "did we already alert this band?" test and the state write are
    ONE ATOMIC compare-and-swap, not a read-then-write. The UPDATE claims the crossing
    only WHERE the stored state does not already cover (period, band); rowcount 1 = this
    caller won the crossing and alerts, rowcount 0 = another thread (or the jobs pool
    racing an explicit call) already claimed it and this caller skips audit+email
    entirely. So N concurrent crossings write exactly ONE audit row + ONE email. The
    WHERE is None-safe and dialect-safe (plain IS NULL / <> / < — no IS DISTINCT FROM)."""
    from sqlalchemy import text

    with Session(engine) as session:
        budget = get_project_budget(session, project_id)
        if budget is None or budget.monthly_usd_limit <= 0:
            return None
        enforced = project_spend_this_month(session, project_id, now)["enforced_usd"]
        limit = budget.monthly_usd_limit
        pct = 100.0 * enforced / limit
        band = _band_for_pct(pct)
        if band <= 0:
            return None
        period = _period_key(now)
        budget_id = budget.id
        org_id = budget.org_id
        # Atomic claim: set (period, band) only if the row does NOT already record this
        # band for this period. A different (or NULL) period re-arms; the same period only
        # advances to a HIGHER band. Rowcount 0 => lost the race / already alerted => skip.
        res = session.execute(
            text(
                "UPDATE projectbudget SET notified_period = :p, notified_pct = :band, "
                "updated_at = :u WHERE id = :id AND ("
                "notified_period IS NULL OR notified_period <> :p OR notified_pct < :band)"
            ),
            {"p": period, "band": band, "u": datetime.utcnow(), "id": budget_id},
        )
        session.commit()
        if res.rowcount != 1:
            return None  # another caller already consumed this crossing

        from app.models import Project

        project = session.get(Project, budget.project_id)
        audit.log(
            actor=actor,
            action="budget.threshold",
            target=f"project-budget {budget_id}",
            detail=(
                f"project={budget.project_id} band={band}% spent={enforced:.2f} limit={limit:.2f}"
            ),
            ip=ip,
            customer_id=getattr(project, "customer_id", None),
        )
        try:
            from app.services import notifications

            notifications.on_project_budget_threshold(
                session, project, band, pct, enforced, limit, org_id=org_id
            )
            from app.services import notify

            notify.notify(
                "run.limit_hit",
                project,
                {
                    "limit": "budget",
                    "band": 100 if band >= 100 else 80,
                    "dedup": f"budget:{budget_id}:{period}:{band}",
                },
            )
        except Exception:
            import logging

            logging.getLogger("loc_tms.project_budgets").exception(
                "budget threshold email failed for project %s", project_id
            )
    return band
