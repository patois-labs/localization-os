"""Per-workflow usage + quality stats — the single source for the /workflows
management dashboard (SPEC Workflow Studio Wave A), the CLI `workflow list` stats
columns, and the /api workflows list stats fields. Web, CLI, and /api ALL call this
one seam so the three surfaces cannot drift (the P6 parity guard enforces the CLI
arms; the fields here are the shared contract).

WHAT IT REPORTS, per workflow (the canonical stat dict — identical key set on every
surface, so a headless consumer reads exactly what the dashboard shows):

  runs_total        every WorkflowRun row for the workflow (batch members included —
                    a batch member is an ordinary WorkflowRun with the resolved
                    variant's workflow_id, so it is counted naturally).
  runs_30d          runs whose run-date (finished_at, else started_at — the reports
                    _run_date convention) falls within the last 30 days.
  last_run_at       the most-recent run-date, ISO-8601 string (json-safe) or None.
  projects_served   distinct projects the workflow has run documents in.
  customers_served  distinct customers those projects belong to (a NULL-customer
                    project — an internal/unassigned project — is not counted).
  spend_usd         SUM of the stamped StepRun.cost_usd over the workflow's runs.
                    HONEST NULL HANDLING (mirrors reports._workflow_run_buckets): a
                    workflow whose runs are ALL untracked (every StepRun.cost_usd is
                    NULL — runs finished before spend-tracking, or that spent nothing
                    priced) reports spend_usd = None, never a fake $0. As soon as ANY
                    run carries a real cost the sum is that real (possibly partial) total.
  spend_partial     True when spend_usd is a REAL number BUT at least one of the
                    workflow's runs is untracked (all its StepRun costs NULL — legacy,
                    or a run that has not stamped any cost yet). So the displayed total
                    is a floor, not the full spend — the surfaces mark it (a "+" suffix /
                    tooltip) so a mixed workflow's number never reads as complete. A
                    RUN is "tracked" iff ANY of its StepRuns stamped a non-NULL cost
                    (the reports run-granularity convention); a mock run stamps an honest
                    0.0 so it counts as tracked. False when every run is tracked, and
                    False when spend_usd is None (nothing to qualify).
  active            True if any run is currently PENDING/RUNNING (drives the
                    signal-dot on the dashboard).

  QUALITY SIGNAL — two honest, RUN/StepRun-derivable rates (no schema added):

  lqa_pass_rate     share of the workflow's LQA gate outcomes that PASSED, in 0..1,
                    or None when the workflow produced no LQA gate. DEFINITION: every
                    StepRun of a StepType.LQA step that recorded a non-NULL gate
                    ('passed' | 'failed' — the durable Phase-1 outcome the orchestrator
                    stamps) counts as one outcome; the rate is passed / (passed+failed).
                    Per-VISIT by design: a correction loop's repeated LQA judgements are
                    each a real quality reading, so each is weighed. QE-gate llm_agent
                    steps also stamp a gate, but this rate is LQA-ONLY (joined on
                    step_type == LQA) so it means exactly "how often the LQA judge
                    passed", not a blended gate figure. lqa_gate_count is the honest
                    denominator (surface it so a 100% off one outcome is not mistaken
                    for a track record).
  needs_review_rate share of the workflow's runs that ended UNRESOLVED at a human
                    checkpoint, in 0..1, or None when the workflow has no runs.
                    DEFINITION: a run counts when its status is PAUSED — the
                    orchestrator pauses a run at a human_review step and leaves it
                    PAUSED until a human resumes it, so a PAUSED run is precisely an
                    unresolved needs-review run (RunStatus has no distinct
                    'needs_review' run state — PAUSED IS that state at the run level).
                    This is deliberately the RUN-level signal from WorkflowRun.status,
                    NOT the per-SEGMENT QE needs_review routing (which lives on
                    Segment.status/qa_flags and is a different, per-content metric — a
                    completed run can still have routed some segments to review). A
                    COMPLETED run resolved cleanly and a FAILED run is an error (its own
                    signal), so neither counts here. needs_review_count is the numerator.

SCOPING — always through app/services/access.py predicates, never hand-rolled: the
runs considered are confined to the caller's VISIBLE projects
(access.visible_project_ids). So a customer-bound linguist viewing a GLOBAL workflow
sees stats only from runs on projects in their own customer ring — projects_served /
customers_served / spend / rates never leak another customer's activity through a
shared workflow. An unbound/internal caller (user None or see-all) gets the
unrestricted view. The WORKFLOW LIST itself is scoped by the caller's route (org +
customer ring) before ids reach here; this adds the run-side confinement on top.

EFFICIENCY: three grouped queries total (runs+project join, StepRun costs, LQA gate
join), NOT N+1 per workflow. collect() fetches once for the whole id set and buckets
by workflow_id, so the dashboard's N rows cost the same three queries as one.

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.models import (
    Document,
    Project,
    RunStatus,
    StepRun,
    StepType,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access

# Chunk size for IN(...) clauses over run ids (mirrors reports._workflow_run_buckets).
_CHUNK = 150
# QUEUED counts as active (gate A round 3): a fan-out row waiting for a slot is work in
# flight on this workflow, and a stats page that omitted it would under-report a busy install.
_ACTIVE = (RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING)

# The canonical stat-dict keys, in a stable order. Exposed so a consumer (and the
# empty-workflow zero row) can rely on the exact contract without guessing.
STAT_KEYS = (
    "runs_total",
    "runs_30d",
    "last_run_at",
    "projects_served",
    "customers_served",
    "spend_usd",
    "spend_partial",
    "active",
    "lqa_pass_rate",
    "lqa_gate_count",
    "needs_review_rate",
    "needs_review_count",
)


def zero_stats() -> dict:
    """The stat dict for a workflow with no (visible) runs — every count 0, every
    rate/last-run/spend None, active False. Used for the genuine empty state and for
    any workflow id absent from a collect() bundle, so a caller never KeyErrors and
    a never-run workflow reads as an honest blank rather than a fabricated zero-rate."""
    return {
        "runs_total": 0,
        "runs_30d": 0,
        "last_run_at": None,
        "projects_served": 0,
        "customers_served": 0,
        "spend_usd": None,
        "spend_partial": False,
        "active": False,
        "lqa_pass_rate": None,
        "lqa_gate_count": 0,
        "needs_review_rate": None,
        "needs_review_count": 0,
    }


@dataclass
class _Accum:
    """Mutable per-bucket accumulator holding the raw sets/counters so an arbitrary
    SUBSET of workflows (a multilingual group's variants) can be merged into one row
    with correct DISTINCT (union) semantics — a plain sum of two variants'
    customers_served would double-count a shared customer. `finalize()` collapses it
    to the canonical scalar stat dict."""

    runs_total: int = 0
    runs_30d: int = 0
    last_run: datetime | None = None
    project_ids: set = field(default_factory=set)
    customer_ids: set = field(default_factory=set)
    active: bool = False
    needs_review_count: int = 0
    # Spend tracked at RUN granularity (the reports convention): cost_sum is the total
    # over TRACKED runs; runs_with_cost/runs_without_cost let finalize() distinguish
    # all-untracked (spend None) from mixed (spend real BUT partial) from fully-tracked.
    cost_sum: float = 0.0
    runs_with_cost: int = 0
    runs_without_cost: int = 0
    lqa_passed: int = 0
    lqa_failed: int = 0

    def add_run(self, run_date, status, project_id, customer_id, cutoff):
        self.runs_total += 1
        if run_date is not None and run_date >= cutoff:
            self.runs_30d += 1
        if run_date is not None and (self.last_run is None or run_date > self.last_run):
            self.last_run = run_date
        if project_id is not None:
            self.project_ids.add(project_id)
        if customer_id is not None:
            self.customer_ids.add(customer_id)
        if status in _ACTIVE:
            self.active = True
        if status == RunStatus.PAUSED:
            self.needs_review_count += 1

    def add_tracked_run(self, cost):
        """A run with >=1 non-NULL StepRun cost: fold its total in and count it tracked."""
        self.cost_sum += cost
        self.runs_with_cost += 1

    def add_untracked_run(self):
        """A run whose StepRun costs are ALL NULL (legacy / nothing stamped yet)."""
        self.runs_without_cost += 1

    def add_lqa_gate(self, gate):
        if gate == "passed":
            self.lqa_passed += 1
        elif gate == "failed":
            self.lqa_failed += 1

    def finalize(self) -> dict:
        lqa_total = self.lqa_passed + self.lqa_failed
        tracked = self.runs_with_cost > 0
        return {
            "runs_total": self.runs_total,
            "runs_30d": self.runs_30d,
            "last_run_at": self.last_run.isoformat() if self.last_run else None,
            "projects_served": len(self.project_ids),
            "customers_served": len(self.customer_ids),
            "spend_usd": (round(self.cost_sum, 6) if tracked else None),
            # Real number, but some run contributed no stamped cost -> the total is a floor.
            "spend_partial": bool(tracked and self.runs_without_cost > 0),
            "active": self.active,
            "lqa_pass_rate": (round(self.lqa_passed / lqa_total, 4) if lqa_total else None),
            "lqa_gate_count": lqa_total,
            "needs_review_rate": (
                round(self.needs_review_count / self.runs_total, 4) if self.runs_total else None
            ),
            "needs_review_count": self.needs_review_count,
        }


class StatsBundle:
    """The result of one collect() pass: raw per-workflow accumulators plus helpers to
    read a single workflow's stats OR an aggregate over a set of workflow ids (a group's
    variants). Aggregation unions the DISTINCT sets, so a group row's customers/projects
    served is the true distinct count across its variants, never a double-counting sum."""

    def __init__(self, accums: dict[int, _Accum]):
        self._accums = accums

    def for_workflow(self, workflow_id: int) -> dict:
        acc = self._accums.get(workflow_id)
        return acc.finalize() if acc is not None else zero_stats()

    def for_workflows(self, workflow_ids: Iterable[int]) -> dict:
        """Aggregate stats over several workflows as ONE bucket (union semantics) —
        the row for a multilingual group, summed across its variant workflow ids."""
        merged = _Accum()
        seen = False
        for wid in workflow_ids:
            acc = self._accums.get(wid)
            if acc is None:
                continue
            seen = True
            merged.runs_total += acc.runs_total
            merged.runs_30d += acc.runs_30d
            if acc.last_run is not None and (
                merged.last_run is None or acc.last_run > merged.last_run
            ):
                merged.last_run = acc.last_run
            merged.project_ids |= acc.project_ids
            merged.customer_ids |= acc.customer_ids
            merged.active = merged.active or acc.active
            merged.needs_review_count += acc.needs_review_count
            merged.cost_sum += acc.cost_sum
            merged.runs_with_cost += acc.runs_with_cost
            merged.runs_without_cost += acc.runs_without_cost
            merged.lqa_passed += acc.lqa_passed
            merged.lqa_failed += acc.lqa_failed
        return merged.finalize() if seen else zero_stats()


def _chunks(seq, n=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def collect(
    session: Session,
    workflow_ids: Iterable[int],
    *,
    user=None,
    now: datetime | None = None,
    unrestricted: bool = False,
    org_id: int | None = None,
) -> StatsBundle:
    """Compute stats for every workflow id in `workflow_ids` in three grouped queries.

    Runs are confined to the caller's VISIBLE projects via
    access.visible_project_ids (None = unrestricted) — the org/customer scoping seam,
    never hand-rolled. `now` is injectable so a test can pin the 30-day window
    deterministically (defaults to utcnow, matching the run timestamps' clock).

    `unrestricted=True` (issue #49) skips the project confinement entirely. It exists for
    ONE caller shape: the LOCAL in-process CLI (`require_local`), which has no signed-in
    identity and whose scope is already decided upstream by its own `--org` filter over
    the workflow list. Passing `user=None` there was NOT unrestricted — with the login
    wall ON, `can_see_all(None)` is False and `visible_project_ids` returns [], so every
    workflow reported zero runs on any auth-on install. NO request-serving caller may
    pass this: a web/API request always has a user, and that user's ring is the answer.

    `org_id` (R10, adversarial review) narrows an `unrestricted` collect to ONE org's
    projects. The CLI's `--org` already confines which WORKFLOWS are listed; without this
    the RUN rollup beside them stayed global, so `workflow list --org 2` printed org 2's
    workflows with run counts including org 1's runs on a shared/legacy workflow id.
    Ignored unless `unrestricted` (a real user's own ring is always the stronger
    answer)."""
    wf_ids = [w for w in dict.fromkeys(workflow_ids) if w is not None]
    accums: dict[int, _Accum] = {wid: _Accum() for wid in wf_ids}
    if not wf_ids:
        return StatsBundle(accums)

    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=30)

    # access seam: the projects this caller may see (None = unrestricted). A concrete
    # empty list means the caller sees nothing -> no runs contribute (never a leak).
    if unrestricted:
        # R10: an --org'd local CLI run is unrestricted WITHIN that org, not globally.
        visible = (
            None
            if org_id is None
            else list(session.exec(select(Project.id).where(Project.org_id == org_id)).all())
        )
    else:
        visible = access.visible_project_ids(session, user)
    if visible is not None and not visible:
        return StatsBundle(accums)
    visible_set = set(visible) if visible is not None else None

    # (1) runs joined to their document's project — one row per run carrying the
    # bucketing date, status, and the project/customer it served. Chunk the workflow-id
    # IN clause defensively (id count is small, but keep it bounded).
    run_workflow: dict[int, int] = {}  # run_id -> workflow_id (for the later joins)
    run_ids: list[int] = []
    for chunk in _chunks(wf_ids):
        rows = session.exec(
            select(
                WorkflowRun.id,
                WorkflowRun.workflow_id,
                WorkflowRun.status,
                WorkflowRun.finished_at,
                WorkflowRun.started_at,
                Document.project_id,
                Project.customer_id,
            )
            .join(Document, Document.id == WorkflowRun.document_id)
            .join(Project, Project.id == Document.project_id)
            .where(WorkflowRun.workflow_id.in_(chunk))
        ).all()
        for rid, wid, status, finished_at, started_at, project_id, customer_id in rows:
            if visible_set is not None and project_id not in visible_set:
                continue
            run_date = finished_at or started_at
            accums[wid].add_run(run_date, status, project_id, customer_id, cutoff)
            run_workflow[rid] = wid
            run_ids.append(rid)

    if not run_ids:
        return StatsBundle(accums)

    # (2) spend: aggregate stamped StepRun.cost_usd at RUN granularity first, then feed
    # each run to its workflow's accumulator as tracked (>=1 non-NULL cost) or untracked
    # (all NULL). This is what lets a MIXED workflow report a real-but-PARTIAL total
    # (spend_partial) instead of a bare number that reads as complete. A run with no
    # StepRun rows at all (never dispatched a step) is untracked — honest.
    run_cost: dict[int, float] = {}
    run_tracked: dict[int, bool] = {}
    for chunk in _chunks(run_ids):
        for rid, cost in session.exec(
            select(StepRun.workflow_run_id, StepRun.cost_usd).where(
                StepRun.workflow_run_id.in_(chunk)
            )
        ).all():
            if cost is not None:
                run_cost[rid] = run_cost.get(rid, 0.0) + cost
                run_tracked[rid] = True
    for rid in run_ids:
        wid = run_workflow.get(rid)
        if wid is None:
            continue
        if run_tracked.get(rid):
            accums[wid].add_tracked_run(run_cost.get(rid, 0.0))
        else:
            accums[wid].add_untracked_run()

    # (3) quality: LQA gate outcomes. Join StepRun -> WorkflowStep, keep only
    # StepType.LQA steps that recorded a non-NULL gate, bucket by the run's workflow.
    for chunk in _chunks(run_ids):
        for rid, gate in session.exec(
            select(StepRun.workflow_run_id, StepRun.gate)
            .join(WorkflowStep, WorkflowStep.id == StepRun.step_id)
            .where(StepRun.workflow_run_id.in_(chunk))
            .where(WorkflowStep.step_type == StepType.LQA)
            .where(StepRun.gate.is_not(None))
        ).all():
            wid = run_workflow.get(rid)
            if wid is not None:
                accums[wid].add_lqa_gate(gate)

    return StatsBundle(accums)


def stats_for(
    session: Session,
    workflow_ids: Iterable[int],
    *,
    user=None,
    now: datetime | None = None,
    unrestricted: bool = False,
    org_id: int | None = None,
) -> dict[int, dict]:
    """Convenience: {workflow_id: stat_dict} for a flat list of workflows (the CLI /
    api list callers, which have no group collapsing). One collect() pass.
    `unrestricted` is the local in-process CLI escape hatch, and `org_id` narrows it to
    one org's projects (the CLI's --org) — see `collect`."""
    ids = [w for w in dict.fromkeys(workflow_ids) if w is not None]
    bundle = collect(session, ids, user=user, now=now, unrestricted=unrestricted, org_id=org_id)
    return {wid: bundle.for_workflow(wid) for wid in ids}
