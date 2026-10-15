"""AI-vs-Human spend accounting (PROPOSAL-external-processing-step §9 decision 5;
Wave C surfaces). NOTE the sibling `spend.py` is the unrelated spend-CAP kill switch;
this module is the AI/Human spend SPLIT that the surfaces render.

Three DISJOINT ledgers exist in the system today:
  * StepRun.cost_usd                                   -- AI spend (the model bill for a
                                                          workflow step's provider call).
  * VendorDispatch.estimate_cost_usd / actual_cost_usd -- HUMAN spend (the vendor's
                                                          MSA-priced human post-edit;
                                                          estimate until the vendor
                                                          reconciles an actual on return).
  * TapAction.totals.spend_usd                         -- an AI ROLLUP of the same StepRun
                                                          spend, for the prompt-tap.

The maintainer (decision 5, near-verbatim): "every spend tracked so far is strictly AI spend; this
moves into Human spend. There should be a different track for that. It may be within the
same number but it's now broken down to AI spend and Human spend."

This module is the ONE shared query helper that, given a set of run ids, returns the AI +
Human split so every surface (run detail, reports, envelopes) renders the SAME numbers
with the SAME label. HUMAN = actual_cost_usd when the vendor has reported it, else the
scoping estimate_cost_usd -- LABELED (`human_spend_source`) so a reader knows whether the
human number is settled ('actual') or still projected ('estimate' / 'mixed').

PURE READ. No writes, no network. Every numeric read is None-safe so one malformed/legacy
row prices as 0 rather than 500-ing a report or a run page.

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import StepRun, VendorDispatch, VendorDispatchStatus

# The leg statuses whose cost counts toward Human spend. A CANCELLED leg is work that was
# superseded/aborted and never billed, so it is EXCLUDED. M2 (fix round): a DEAD_LETTER leg
# NEVER REACHED THE VENDOR (its dispatch exhausted the retry window, was refused
# non-retryably, or the endpoint changed) -- no human work was ever commissioned, so it is
# UNBILLED and EXCLUDED too. DISPATCHED / RETRY_WAIT (committed, in flight) / RETURNED
# (delivered) / ESCALATED (a person is taking over the committed work) all count.
#
# Wave E (fix-round finding #2):
#   * REJECTED counts. A rejected delivery was WORK THE VENDOR PERFORMED AND BILLED; the
#     rework leg that supersedes it is a SEPARATE, additional charge. Both count, because
#     the honest human spend is the rejected work PLUS the rework (you pay for both).
#   * QUOTED counts at ESTIMATE grade (its scoping estimate), so a quote-gated leg awaiting
#     approval is VISIBLE on the spend surfaces rather than silently absent. Decision: a
#     QUOTED leg has NO approved figure yet, so it prices at estimate grade ("awaiting
#     approval") -- never as a settled/authorized number.
HUMAN_LEG_STATUSES: frozenset[str] = frozenset(
    {
        VendorDispatchStatus.DISPATCHED,
        VendorDispatchStatus.RETRY_WAIT,
        VendorDispatchStatus.RETURNED,
        VendorDispatchStatus.ESCALATED,
        VendorDispatchStatus.REJECTED,
        VendorDispatchStatus.QUOTED,
    }
)


def _f(value) -> float:
    """A None-safe, non-raising float coercion (a malformed/legacy value -> 0.0)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # NaN -> 0.0


def leg_human_cost(leg: VendorDispatch) -> tuple[float, str]:
    """(cost, grade) for ONE leg, grade in {'actual', 'quote', 'estimate'}. The value is
    priced by preference (finding #9): a SETTLED actual, else an operator-APPROVED quote,
    else the scoping estimate.

      * 'actual'   -- actual_cost_usd is set AND the leg has NO outstanding ids (M3: a
                      partial return may stamp an interim actual while tranches are still
                      out; that is NOT settled and reads as estimate grade, below).
      * 'quote'    -- no actual yet, but the operator APPROVED a quote (quote_approved_at +
                      quoted_cost_usd). A person authorized this figure, so it outranks the
                      raw scoping estimate but is below a reconciled actual.
      * 'estimate' -- everything else: a partial interim actual (value = the interim actual),
                      or the scoping estimate for an unpriced / quote-awaiting leg."""
    from app.services import vendor_return

    # R5 (round 2): a quote-gated leg ESCALATED without ever being APPROVED is a DECLINED (or
    # expired-unapproved) quote -- the operator refused the price, so NO human work was
    # commissioned. Price it $0 with a 'declined' grade so a refused price never bills as Human
    # spend. (A leg that WAS approved and later escalated carries quote_approved_at, so it is
    # NOT matched here and still bills.) No marker column needed -- the state is derivable.
    if (
        getattr(leg, "requires_quote", False)
        and getattr(leg, "quote_approved_at", None) is None
        and (leg.status or "") == VendorDispatchStatus.ESCALATED
    ):
        return 0.0, "declined"
    if leg.actual_cost_usd is not None and not vendor_return.outstanding_ids(leg):
        return _f(leg.actual_cost_usd), "actual"
    if (
        leg.actual_cost_usd is None
        and getattr(leg, "quote_approved_at", None) is not None
        and getattr(leg, "quoted_cost_usd", None) is not None
    ):
        return _f(leg.quoted_cost_usd), "quote"
    return _f(
        leg.actual_cost_usd if leg.actual_cost_usd is not None else leg.estimate_cost_usd
    ), "estimate"


#: The grade vocabulary leg_human_cost emits; every one has a bucket in the summary.
_GRADES: tuple[str, ...] = ("actual", "quote", "estimate", "declined")


def _source_label(counts: dict) -> str:
    """The human-spend provenance label for a bucket of legs: 'none' (no counting legs),
    a single grade name ('actual'/'quote'/'estimate'/'declined') when every counting leg
    shares it, else 'mixed'. Surfaces that pin the field to 'actual'|'estimate' read anything
    else as 'not fully settled', i.e. estimate-grade."""
    present = [g for g in _GRADES if counts.get(g, 0) > 0]
    if not present:
        return "none"
    if len(present) > 1:
        return "mixed"
    return present[0]


def _empty() -> dict:
    return {
        "ai_spend_usd": 0.0,
        "human_spend_usd": 0.0,
        "human_actual_usd": 0.0,
        "human_quote_usd": 0.0,
        "human_estimate_usd": 0.0,
        "human_declined_usd": 0.0,
        "human_spend_source": "none",
        "human_leg_count": 0,
        "total_spend_usd": 0.0,
    }


def _summarize(ai, legs) -> dict:
    """Assemble the summary dict from an AI total + the counting legs of a bucket."""
    sums = dict.fromkeys(_GRADES, 0.0)
    counts = dict.fromkeys(_GRADES, 0)
    for leg in legs:
        cost, grade = leg_human_cost(leg)
        if grade not in sums:
            grade = "estimate"
        sums[grade] += cost
        counts[grade] += 1
    human = round(sum(sums.values()), 6)
    ai = round(_f(ai), 6)
    return {
        "ai_spend_usd": ai,
        "human_spend_usd": human,
        "human_actual_usd": round(sums["actual"], 6),
        "human_quote_usd": round(sums["quote"], 6),
        "human_estimate_usd": round(sums["estimate"], 6),
        "human_declined_usd": round(sums["declined"], 6),
        "human_spend_source": _source_label(counts),
        "human_leg_count": sum(counts.values()),
        "total_spend_usd": round(ai + human, 6),
    }


def ai_only(summary: dict) -> dict:
    """The NON-ADMIN view of a spend summary: every Human-spend field ZEROED, keeping only
    the AI total. R3 (round 2): rebuilt from `_empty()` (the canonical zero shape) and then
    spread with the AI numbers, so a FUTURE human_* key is masked automatically -- never
    hand-list the keys to strip (a forgotten one leaks, which is exactly how human_quote_usd
    survived the old hand-listed mask). The returned dict carries the SAME key set as a real
    summary, so no template read goes missing."""
    masked = _empty()
    ai = _f((summary or {}).get("ai_spend_usd"))
    masked["ai_spend_usd"] = round(ai, 6)
    masked["total_spend_usd"] = round(ai, 6)
    return masked


def _norm_ids(run_ids) -> list[int]:
    if run_ids is None:
        return []
    if isinstance(run_ids, int):
        run_ids = [run_ids]
    out = []
    for r in run_ids:
        try:
            out.append(int(r))
        except (TypeError, ValueError):
            continue
    return out


def _ai_spend_for(session: Session, run_ids: list[int]) -> dict[int, float]:
    """AI spend per run: sum of the run's StepRun.cost_usd (None-safe)."""
    if not run_ids:
        return {}
    rows = session.exec(
        select(StepRun.workflow_run_id, StepRun.cost_usd).where(
            StepRun.workflow_run_id.in_(run_ids)
        )
    ).all()
    out: dict[int, float] = {}
    for rid, cost in rows:
        if cost is None:
            continue
        out[rid] = out.get(rid, 0.0) + _f(cost)
    return out


def _human_legs_for(session: Session, run_ids: list[int]) -> dict[int, list]:
    """The counting Human-spend legs per run (HUMAN_LEG_STATUSES; CANCELLED excluded)."""
    if not run_ids:
        return {}
    rows = session.exec(
        select(VendorDispatch).where(
            VendorDispatch.workflow_run_id.in_(run_ids),
            VendorDispatch.status.in_(tuple(HUMAN_LEG_STATUSES)),
        )
    ).all()
    out: dict[int, list] = {}
    for leg in rows:
        out.setdefault(leg.workflow_run_id, []).append(leg)
    return out


def spend_summary(session: Session, run_ids) -> dict:
    """The AGGREGATE AI/Human spend split across all the given run ids. Returns
    {ai_spend_usd, human_spend_usd, human_actual_usd, human_estimate_usd,
     human_spend_source, human_leg_count, total_spend_usd}."""
    ids = _norm_ids(run_ids)
    if not ids:
        return _empty()
    ai_total = sum(_ai_spend_for(session, ids).values())
    legs = [leg for legs in _human_legs_for(session, ids).values() for leg in legs]
    return _summarize(ai_total, legs)


def spend_by_run(session: Session, run_ids) -> dict[int, dict]:
    """The AI/Human spend summary PER run id (for report buckets). Every requested id is
    present in the result (a run with no recorded spend maps to the zero summary)."""
    ids = _norm_ids(run_ids)
    if not ids:
        return {}
    ai_by = _ai_spend_for(session, ids)
    legs_by = _human_legs_for(session, ids)
    return {rid: _summarize(ai_by.get(rid, 0.0), legs_by.get(rid, [])) for rid in ids}


__all__ = [
    "HUMAN_LEG_STATUSES",
    "ai_only",
    "leg_human_cost",
    "spend_by_run",
    "spend_summary",
]
