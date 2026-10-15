"""Scheduled connector syncs (Phase 5).

One daemon thread wakes every TICK_SECONDS, finds connections whose schedule
is due, and enqueues the exact same `connector_translate` job the "Translate
now" button uses — progress, history, failure capture, and restart recovery
all stay the job engine's problem.

Safety model (scheduled runs hit real MT and write to live external systems —
losing a scheduled run is always preferable to double-firing one):
- Per-connection OFF by default: no `sync_every_minutes` in the connection's
  config means it never fires.
- Enabling a schedule NEVER fires immediately — the first run is one full
  interval out (there's a "Translate now" button for right-now).
- Fresh re-read per due connection: the interval and next_at are re-derived
  from CURRENT config inside the tick, so a schedule disabled mid-tick (or
  edited by a concurrent request) is honored, never resurrected.
- Consume-slot-first: sync_next_at is advanced a full interval BEFORE the job
  is submitted. A crash between the two skips one run; it can never duplicate
  one.
- Atomic dedup: jobs.submit_unique is the single overlap guard — a due
  connection that already has a QUEUED/RUNNING connector_translate job is
  skipped (its slot already consumed), so a slow sync can't stack a second run.
- Catch-up policy: if the server was down past the due time, at most ONE run
  fires and the next slot is a full interval from now — never a burst.
- Global kill switch: the `sync_scheduler_enabled` setting ("0" disables all
  scheduled fires without touching per-connection config).
- Minimum interval floor (MIN_EVERY_MINUTES) caps how often a live system can
  be hit, enforced service-side for every caller.

Schedule state lives in Connection.config (JSON — no migration needed):
  sync_every_minutes  int > 0 = enabled
  sync_next_at        ISO datetime (naive UTC) of the next fire
  sync_last_at        ISO datetime of the last scheduled fire
  sync_last_job_id    Job id of the last scheduled fire (for the UI)

Project automations (a schedule per PROJECT with times of day, days of the week and delta or
full mode, app/services/project_automation.py) are ticked from the same pass, below the same
kill switch. A connection that an enabled automation runs on is skipped here.

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.db import engine
from app.models import Connection
from app.services import audit, jobs, settings
from app.services import connectors as connectors_registry
from app.services.connectors import store as conn_store  # patch_config / CONFIG_LOCK
from app.services.maintenance import MaintenancePass

log = logging.getLogger("loc_tms.scheduler")

TICK_SECONDS = 30
# Floor on how often a schedule may hit the live MT engine + external system.
# Enforced service-side in set_schedule so it applies to every caller (route,
# API, hand-crafted call), not just the UI's preset list.
MIN_EVERY_MINUTES = 15
# (minutes, label) choices for the connection page's schedule select
PRESETS = [(15, "Every 15 minutes"), (60, "Hourly"), (360, "Every 6 hours"), (1440, "Daily")]

_started = False
_start_lock = threading.Lock()

# One active pass across direct, daemon and manual callers. The admission lock
# protects only the active flag, never sessions, dispatch or audit writes.
_tick_pass = MaintenancePass(lambda: None)
_tick_lock = _tick_pass.lock


def tick_now(now: datetime | None = None) -> tuple[bool, list[int] | None]:
    """Manual entry: (True, job_ids), or (False, None) for the existing 409/busy.
    Direct and daemon ticks use the same non-blocking admission seam."""
    fired = tick(now=now)
    return fired is not None, fired


def enabled_globally() -> bool:
    return settings.get("sync_scheduler_enabled", "1") == "1"


def interval_minutes(conn: Connection) -> int:
    """The connection's schedule interval; 0 = scheduling off."""
    try:
        return max(0, int(str((conn.config or {}).get("sync_every_minutes") or 0)))
    except (TypeError, ValueError):
        return 0


def _parse_dt(raw) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is not None:
        # A hand-edited config may carry a tz-aware stamp; normalize to naive UTC
        # so comparisons with a naive utcnow() can never raise.
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _save_state(
    cid: int,
    next_at: datetime | None = None,
    last_at: datetime | None = None,
    last_job_id: int | None = None,
) -> None:
    """Persist schedule bookkeeping via the atomic config patch. CRITICAL:
    sync_next_at is written only when the FRESHLY-read config still carries
    sync_every_minutes — a schedule disabled mid-tick must stay disabled, never
    resurrected by an in-flight next_at write. last_at/last_job_id are harmless
    on a disabled schedule, so they're always applied."""
    updates: dict = {}
    if last_at is not None:
        updates["sync_last_at"] = last_at.isoformat(timespec="seconds")
    if last_job_id is not None:
        updates["sync_last_job_id"] = last_job_id
    if next_at is not None:
        # Re-read under the same lock CONFIG writers use: skip the next_at write
        # if scheduling was turned off since this tick began.
        with conn_store.CONFIG_LOCK, Session(engine) as s:
            conn = s.get(Connection, cid)
            still_on = bool(conn and (conn.config or {}).get("sync_every_minutes"))
        if still_on:
            updates["sync_next_at"] = next_at.isoformat(timespec="seconds")
    if updates:
        conn_store.patch_config(cid, updates)


def set_schedule(cid: int, minutes: int, actor: str = "") -> None:
    """Write the schedule config. The first fire is one full interval out —
    enabling a schedule must never trigger an immediate run against a live
    system. minutes <= 0 turns scheduling off (state keys removed). A nonzero
    interval is floored at MIN_EVERY_MINUTES for every caller."""
    minutes = max(0, int(minutes))
    if minutes > 0:
        minutes = max(minutes, MIN_EVERY_MINUTES)
    conn = conn_store.get(cid)
    if not conn:
        return
    before = (conn.config or {}).get("sync_every_minutes") or 0
    if minutes > 0:
        conn_store.patch_config(
            cid,
            {
                "sync_every_minutes": minutes,
                "sync_next_at": (datetime.utcnow() + timedelta(minutes=minutes)).isoformat(
                    timespec="seconds"
                ),
            },
        )
    else:
        conn_store.patch_config(cid, remove=("sync_every_minutes", "sync_next_at"))
    audit.log(
        actor=actor or "system",
        action="connector.schedule",
        target=f"connection:{cid}",
        detail=f"every_minutes {before} -> {minutes}",
        customer_id=getattr(conn, "customer_id", None),
    )


@_tick_pass
def tick(now: datetime | None = None) -> list[int] | None:
    """One scheduler pass; returns the job ids it enqueued. The daemon loop
    calls this every TICK_SECONDS; tests call it directly (deterministic,
    no thread involved).

    An overlapping direct, manual or daemon call returns None without executing.

    Piggybacks the project auto-close sweep (P1, item 2) onto the same tick so no
    new daemon is needed. Auto-close runs regardless of enabled_globally() (that
    switch is the CONNECTOR-sync kill switch, not the lifecycle one); it is a
    no-op when project_auto_close_days is "0", and a failing sweep must never
    abort the connector-sync half."""
    try:
        from app.services import lifecycle

        lifecycle.auto_close_tick(now=now)
    except Exception:
        log.exception("Project auto-close tick crashed")
    # M4: the vendor-dispatch EXPIRY sweep (PROPOSAL-external-processing-step decision 3).
    # Piggybacked on the same tick (no new daemon), guarded, and — like auto-close — run
    # regardless of the connector-sync kill switch (expiry escalation is a lifecycle
    # concern, not a connector sync). A no-op until an external leg is parked and expires.
    try:
        from app.db import engine as _eng
        from app.services import vendor_dispatch

        with Session(_eng) as _s:
            vendor_dispatch.sweep_expired(_s, now=now, actor="scheduler")
    except Exception:
        log.exception("Vendor-dispatch expiry sweep crashed")
    # Prompt-tap ORPHAN REAPER (residual sweep: "no reaper for orphaned actions").
    # Piggybacked on the same tick — no new daemon — and, like auto-close + expiry above,
    # run regardless of the connector-sync kill switch: closing envelope rows left RUNNING
    # by a crash/reload is a lifecycle/housekeeping concern, DB-only (no network egress),
    # not a connector sync. Best-effort inside its own guard (its own session), a no-op
    # unless an envelope has been RUNNING past the threshold with no live backing run.
    try:
        from app.services import tap_envelope

        tap_envelope.reap_orphaned_envelopes(now=now)
    except Exception:
        log.exception("Prompt-tap orphan reaper crashed")
    # Prompt-tap CAPTURE-FILE TTL SWEEP (soak finding S1 / proposal section 7, D4). The
    # per-action run-capture FILES are append-only and NOT rotated, so without an age-out they
    # grow linearly forever on a long-lived install (~11 MB/h under load). Piggybacked on the
    # same tick as the orphan reaper above -- no new daemon -- and, like it, run regardless of
    # the connector-sync kill switch: pruning host-local capture files is housekeeping, not a
    # connector sync, and it is NETWORK-free (it does read Settings + envelope status from the
    # local DB -- not "DB-free" -- but never egresses). Called ARGLESS (not with `now`): the
    # sweep compares FILESYSTEM mtimes against time.time(), never the tick's DB wall clock. It
    # self-throttles to ~hourly and is a no-op unless capturing is ON and prompt_tap_ttl_days
    # is a positive opt-in value. Best-effort inside its own guard.
    try:
        from app.services import prompt_tap

        prompt_tap.sweep_expired_captures()
    except Exception:
        log.exception("Prompt-tap capture-file TTL sweep crashed")
    # Scheduled AUDIT-CHAIN VERIFICATION (gap G-10 / AU-05). Piggybacked on the same tick --
    # no new daemon -- and run REGARDLESS of the connector-sync kill switch, like the
    # auto-close / expiry / orphan-reaper sweeps above: verifying the tamper-evident audit
    # chain is a compliance/forensics concern, DB-ONLY (no network egress), not a connector
    # sync. SELF-THROTTLING inside the sweep: it runs at most once per audit_chain_verify_hours
    # (default 24; only 0 disables). NOT gated on audit_enabled -- verify is a pure read and the
    # chain_broken row is force=True, so monitoring keeps watching even when the global audit
    # gate is off (item-3). Best-effort inside its own guard so a failure never aborts the tick.
    try:
        audit.verify_chain_scheduled_sweep(now=now)
    except Exception:
        log.exception("Scheduled audit-chain verification sweep crashed")
    try:
        from app.services import login_throttle

        login_throttle.purge_expired()
        login_throttle.notify_owners()
    except Exception:
        log.exception("Scheduled login throttle cleanup crashed")
    # LLMemory lifecycle housekeeping (SPEC-llmemory-phase2 §5.3). Piggybacked on the same
    # tick -- no new daemon -- and run REGARDLESS of the connector-sync kill switch, like the
    # auto-close / expiry / orphan-reaper / audit-chain sweeps above: decay/expiry/distill are
    # a memory-lifecycle concern, DB-ONLY (no network egress), not a connector sync. Each
    # SELF-THROTTLES inside the sweep (llmemory_distill_hours / llmemory_janitor_hours; 0 =
    # off, the shipped default, so this is a no-op until an operator sets a cadence) and the
    # cadence gate is a cache-backed settings read checked BEFORE any heavy DB read. The
    # scheduled DISTILL passes allow_llm=False, so no LLM spend/egress ever rides the timer
    # path -- that is why these correctly sit ABOVE the sync kill switch. Best-effort inside
    # their own guards so a failure never aborts the tick.
    # The two sweeps are registered by the LLMemory add-on (app/extensions.py "tick_memory");
    # with the add-on absent nothing runs and nothing is logged.
    from app import extensions as _extensions

    _extensions.run_sweeps("tick_memory", log, now=now)
    # Staged data-export ARTIFACT REAPER (Batch 13, Train C; SPEC-data-export §8.4). Piggybacked
    # on the same tick -- no new daemon -- and run REGARDLESS of the connector-sync kill switch,
    # like the auto-close / expiry / audit-chain / llmemory sweeps above: reaping expired PLAINTEXT
    # export artifacts (+ orphaned dataexport_*.tmp files) is a retention/housekeeping concern,
    # DB + filesystem only (no network egress), not a connector sync. SELF-THROTTLING inside the
    # sweep: it runs at most once per data_export_retention_hours via a MAC'd status row (0
    # disables), and deletes each artifact past its expires_at + the DX-206 hard grace ceiling.
    # Best-effort inside its own guard so a failure never aborts the tick.
    try:
        from app.services import data_export

        data_export.reap_scheduled_sweep(now=now)
    except Exception:
        log.exception("Scheduled data-export artifact reaper crashed")
    # Advanced Workflows ORPHANED-RUN sweep (BACKLOG 2026-09-01). Piggybacked on the same tick --
    # no new daemon -- and run REGARDLESS of the connector-sync kill switch AND the advanced flag,
    # like the auto-close / audit-chain / llmemory sweeps above: reaping a run left RUNNING by a
    # dead process is a lifecycle/housekeeping concern, DB-ONLY (no network egress), not a
    # connector sync, and referential integrity is not a feature (a run stuck RUNNING when the
    # tier is later switched off must still be reaped). Best-effort inside its own guard + own
    # session so a failure never aborts the tick.
    _extensions.run_sweeps("tick_runs", log, now=now)
    if not enabled_globally():
        return []
    # ffadmin Wave 3: the two vendor NETWORK sweeps below (return-poll + dispatch-retry)
    # are vendor EGRESS, so they stop when the Vendors feature is switched off -- turning
    # vendors off must close every surface AND stop all new outward vendor traffic (plan
    # §7). The predicate reads the feature registry (features.is_enabled), the SAME source
    # the middleware / route gates use, so this can never split-brain against them. The
    # DB-only vendor_dispatch.sweep_expired lifecycle sweep ABOVE deliberately keeps running
    # even when off (a switched-off feature preserves its data + finishes lifecycle
    # housekeeping; it simply never sends anything outward), exactly like the auto-close +
    # orphan-reaper sweeps that sit above the sync kill switch.
    from app import features as _features

    if _features.is_enabled("vendors"):
        # Wave B: the vendor RETURN POLL (decision 7 — polling ships day one). MEDIUM-6: this
        # is the only above-gate sweep that makes a NETWORK call, so it is now BELOW the global
        # `sync_scheduler_enabled` kill switch — turning sync off must stop ALL outbound egress
        # from the scheduler, not just connector syncs (the DB-only auto-close + expiry sweeps
        # above are lifecycle concerns and correctly run regardless). Piggybacked on the same
        # tick, fully guarded, and INERT unless a vendor declared a `config.poll_url` (an
        # explicit per-vendor opt-in). Throttled/capped per VENDOR (MEDIUM-5) so a 30s tick
        # cannot hammer a vendor, with a short per-poll timeout so a hung vendor can't wedge the
        # single scheduler thread. Resumes async — the scheduler thread never executes inline.
        try:
            from app.db import engine as _eng
            from app.services import vendor_return

            with Session(_eng) as _s:
                vendor_return.poll_due_legs(_s, now=now, actor="scheduler", resume="async")
        except Exception:
            log.exception("Vendor-return poll sweep crashed")
        # Wave C (interop R5): the DEAD-LETTER/BACKOFF retry sweep for parked RETRY_WAIT legs
        # (a transient OUTBOUND dispatch failure). Like the poll sweep this is NETWORK EGRESS,
        # so it sits BELOW the global `sync_scheduler_enabled` kill switch — turning sync off
        # must stop ALL outbound egress from the scheduler. Piggybacked on the same tick, fully
        # guarded, and INERT unless a leg is parked RETRY_WAIT and its backoff is due. Exponential
        # backoff (RETRY_BACKOFF_SCHEDULE) bounds the attempts; exhaustion dead-letters + escalates
        # to a human (decision 3 — never a hard-fail on vendor unreachability).
        try:
            from app.db import engine as _eng
            from app.services import vendor_dispatch

            with Session(_eng) as _s:
                vendor_dispatch.retry_due_legs(_s, now=now, actor="scheduler")
        except Exception:
            log.exception("Vendor-dispatch retry sweep crashed")
    # Project notifications (Part B): the delivery RETRY sweep is outbound egress, so like the
    # vendor sweeps it sits BELOW `sync_scheduler_enabled`; `notify_outbound_enabled` is checked
    # inside. INERT unless an outbox row is due.
    try:
        from app.services import notify

        notify.sweep_async(now=now)  # a daemon thread: the tick never waits on a send
    except Exception:
        log.exception("Notification retry sweep crashed")
    now = now or datetime.utcnow()
    fired: list[int] = []
    with Session(engine) as s:
        candidate_ids = [c.id for c in s.exec(select(Connection)).all()]
    for cid in candidate_ids:
        # (1) Re-read fresh: a schedule disabled/edited since the candidate scan
        # must be honored, never fired on a stale snapshot.
        conn = conn_store.get(cid)
        if not conn:
            continue
        from app.services import access

        if not access.org_active(conn.org_id):
            continue
        # ffadmin Wave 5: a connection whose KIND is disabled on this install is skipped
        # ENTIRELY -- no Airtable webhook keep-alive and no scheduled sync. A disabled
        # kind's connections are preserved + hidden + inert (the kill-switch law per kind);
        # they resume on re-enable. Reads the same features.is_enabled-style predicate
        # (connectors_registry.kind_enabled) the middleware/route/store.build gates use, so
        # the scheduler can never split-brain against them.
        if not connectors_registry.kind_enabled(conn.kind):
            continue
        # Webhook keep-alive (Airtable Phase-1 scaffolding): refresh a registered
        # official webhook before its 7-day expiry. DORMANT until Phase 2 registers
        # one — a no-op for every connection today. Runs on EVERY tick regardless of
        # the connection's sync schedule (keep-alive is not gated on sync interval),
        # and is fully guarded so a refresh failure never aborts the scheduler pass.
        if conn.kind == "airtable":
            try:
                conn_store.refresh_webhooks(cid, now=now)
            except Exception:
                log.exception("Airtable webhook keep-alive crashed for connection %s", cid)
        every = interval_minutes(conn)
        if every <= 0:
            continue
        nxt = _parse_dt((conn.config or {}).get("sync_next_at"))
        if nxt is None:
            # enabled but no valid next_at (hand-edited config): schedule
            # forward rather than firing on an ambiguous state
            _save_state(cid, next_at=now + timedelta(minutes=every))
            continue
        if nxt > now:
            continue
        # A project automation that runs on this connection owns its schedule: the
        # connection's own interval must never fire it as well (one schedule per connection).
        from app.services import project_automation

        if project_automation.owns_schedule(cid):
            continue
        # (2) Consume the slot FIRST: advance next_at (+ last_at) before
        # submitting. A crash after this skips one run; it can never duplicate
        # one — the whole point of the ordering.
        _save_state(cid, next_at=now + timedelta(minutes=every), last_at=now)
        # (3) Submit through the atomic dedup guard. A dup means a previous run
        # is still active: skip (the slot is already consumed) — never stack.
        jid, created = jobs.submit_unique(
            "connector_translate", {"connection_id": cid, "actor": "scheduler"}, actor="scheduler"
        )
        if not created:
            log.info(
                "Scheduled sync skipped for connection %s: previous run still active (job %s)",
                cid,
                jid,
            )
            continue
        # (4) Record the job id for the UI.
        _save_state(cid, last_job_id=jid)
        # (5) Audit last, guarded: an audit failure must never unwind an already
        # consumed slot + submitted job into an inconsistent state.
        try:
            audit.log(
                actor="scheduler",
                action="connector.scheduled_sync",
                target=f"connection:{cid}",
                detail=f"job:{jid} every:{every}m",
                customer_id=getattr(conn, "customer_id", None),
            )
        except Exception:
            log.exception("Audit log failed for scheduled sync of connection %s (job %s)", cid, jid)
        fired.append(jid)
    # Project automations (schedule per PROJECT: times of day, days, interval, delta or full).
    # Below the sync_scheduler_enabled kill switch like every other egress sweep; guarded so a
    # failure here never aborts the connection-level half of the pass.
    try:
        from app.services import project_automation

        fired.extend(project_automation.tick(now))
    except Exception:
        log.exception("Project automation tick crashed")
    return fired


def _loop() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        from app.services import orchestrator

        orchestrator.ADMISSION.supervise()
        # tick's MaintenancePass coalesces overlap with direct/manual callers.
        # A busy pass returns None; the next 30s wake retries the due work.
        try:
            tick()
        except Exception:
            log.exception("Scheduler tick crashed")


def start() -> None:
    """Start the scheduler daemon thread (idempotent)."""
    global _started
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_loop, name="loc-scheduler", daemon=True).start()
        _started = True
