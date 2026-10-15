"""tap_envelope: shared leaf helpers (layered pure move)

Documented in: docs/architecture/connectors.md
"""

from datetime import datetime, timezone


def _now() -> datetime:
    """UTC wall clock as a naive datetime, matching models._utcnow (the column type is
    a bare DateTime and the whole codebase stores naive-UTC)."""
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    """UTC ISO-8601, for anywhere a string timestamp is wanted (section 4)."""
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _action_rollup_status(statuses):
    """The terminal status for an action whose runs are ALL terminal (caller ensures
    that). Honest priority: FAILED if any leg failed, else ORPHANED if any was reaped as
    orphaned, else ABANDONED if any was dropped, else COMPLETED. A mixed batch surfaces
    the worst honest outcome so a reader is not told a partially-broken click 'completed'.
    (ORPHANED joins TERMINAL, so a leg the reaper closed can appear here if a later
    re-run into the same action finalizes it -- it must not silently roll up to
    COMPLETED.)"""
    from app.models import TapStatus

    if any(st == TapStatus.FAILED for st in statuses):
        return TapStatus.FAILED
    if any(st == TapStatus.ORPHANED for st in statuses):
        return TapStatus.ORPHANED
    if any(st == TapStatus.ABANDONED for st in statuses):
        return TapStatus.ABANDONED
    return TapStatus.COMPLETED


def _action_status_from_runs(statuses):
    """(status, is_terminal) mapped COHERENTLY from an action's TapRun statuses (M2).

    RUNNING if any leg is still running; else PAUSED if any leg is paused (a paused leg
    must surface as a PAUSED action, so the viewer's `paused` filter can match it -- the
    M2 defect was a paused leg leaving the action RUNNING forever); else, every leg being
    terminal, the rolled-up terminal status (`_action_rollup_status`). Returns
    (None, False) for zero runs -- the batch-finalize path (finalize_batch_action) is the
    one that closes a zero-leg action, not this per-leg seam."""
    from app.models import TapStatus

    if not statuses:
        return None, False
    if any(st == TapStatus.RUNNING for st in statuses):
        return TapStatus.RUNNING, False
    if any(st not in TapStatus.TERMINAL for st in statuses):
        # the only non-terminal, non-running status left is PAUSED
        return TapStatus.PAUSED, False
    return _action_rollup_status(statuses), True


def status_for_run_status(run_status):
    """Map a production RunStatus string onto a TapStatus for envelope finalization.

    CANCELLED -> ABANDONED (fix/tap-finalize-race): a run cancelled via cancel_run reaches a
    TERMINAL production status, but cancel_run does not itself finalize the envelope -- the
    live run's own `finally` (_finalize_run_envelope) is what fires, reading the CANCELLED
    row. Mapping CANCELLED to a non-terminal RUNNING here left that envelope stuck 'running'
    (to be reaped 24h later); a cancel is an honest DROP, not a failure, so it finalizes to
    ABANDONED in-band -- the same honest mapping _paused_leg_terminal already uses for a
    cancelled backing. Any status without terminal evidence still degrades to RUNNING."""
    from app.models import RunStatus, TapStatus

    if run_status == RunStatus.COMPLETED:
        return TapStatus.COMPLETED
    if run_status == RunStatus.FAILED:
        return TapStatus.FAILED
    if run_status == RunStatus.PAUSED:
        return TapStatus.PAUSED
    if run_status == RunStatus.CANCELLED:
        return TapStatus.ABANDONED
    return TapStatus.RUNNING


def resolve_finalize_status(run_status, *, exception=False, abandoned=False):
    """The TapStatus to finalize a workflow envelope to, given the OBSERVED OUTCOME
    (M1). Deriving status from the WorkflowRun row alone left an envelope stuck
    'running' whenever an exception propagated mid-run (the row never reached a terminal
    status) or the consumer dropped the stream (abandonment). So the wrapper hands us
    what it saw:
      - abandoned (GeneratorExit before the run finished) -> ABANDONED (dropped, not
        failed);
      - exception propagated -> FAILED, UNLESS the run row already reached a terminal
        status (a step failing does not mean the run is unfinished);
      - otherwise trust the run row (COMPLETED / FAILED / PAUSED; RUNNING only if the
        row genuinely has not finalized, which for a cleanly-exhausted generator it
        has)."""
    from app.models import TapStatus

    mapped = status_for_run_status(run_status)
    if abandoned and mapped not in TapStatus.TERMINAL:
        return TapStatus.ABANDONED
    if exception and mapped not in TapStatus.TERMINAL:
        return TapStatus.FAILED
    return mapped


def _run_is_live(session, workflow_run_id) -> bool:
    """True iff `workflow_run_id` resolves to a production WorkflowRun still in a
    NON-terminal status (pending / running / paused). A workflow leg whose run is gone
    or terminal cannot still be executing -- so an action with no live leg is provably
    dead. A direct/ad-hoc leg (workflow_run_id None) is never 'live' by this check; it
    qualifies for reaping on age alone (it has no backing run to consult)."""
    if workflow_run_id is None:
        return False
    try:
        from app.models import RunStatus, WorkflowRun

        run = session.get(WorkflowRun, workflow_run_id)
        if run is None:
            return False
        return run.status in (
            RunStatus.PENDING,
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
        )
    except Exception:
        # Fail SAFE toward "live" so a probe error never reaps a possibly-live run.
        return True


def _paused_leg_terminal(run_status):
    """The TERMINAL TapStatus for a PAUSED tap leg whose backing WorkflowRun has itself
    reached a terminal status (e.g. the run was CANCELLED via run-cancel, so the paused
    human step will never resume). Honest mapping: a completed/failed backing carries its
    own outcome; a cancelled/stopped backing is a DROP (ABANDONED), never a FAILED."""
    from app.models import RunStatus, TapStatus

    if run_status == RunStatus.COMPLETED:
        return TapStatus.COMPLETED
    if run_status == RunStatus.FAILED:
        return TapStatus.FAILED
    return TapStatus.ABANDONED


def stamp_meta(meta, action_id, run_id):
    """Return `meta` (a NEW dict) with action_id + run_id added when present. The one
    place the orchestrator/translate seams call to attach envelope ids to a tap_meta
    dict; a None id is simply omitted (an honest record with no false provenance).
    Pure, never raises."""
    out = dict(meta or {})
    if action_id:
        out["action_id"] = action_id
    if run_id:
        out["run_id"] = run_id
    return out


def step_fingerprint(steps) -> str:
    """A light, stable fingerprint of a workflow's step graph (keys+types) so two runs
    of 'the same workflow' are distinguishable after the workflow was edited. Pure,
    best-effort ("" on any failure); never raises."""
    try:
        import hashlib

        parts = []
        for st in steps or []:
            parts.append(
                "{}:{}".format(
                    getattr(st, "key", "") or getattr(st, "name", "") or "",
                    getattr(st, "step_type", "") or "",
                )
            )
        blob = "|".join(parts)
        return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        return ""
