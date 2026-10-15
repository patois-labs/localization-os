"""Prompt Tap RUN ENVELOPES -- the DB half of the run-envelope spine (Wave A).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _now, _iso, _action_rollup_status,
_action_status_from_runs, status_for_run_status, resolve_finalize_status, _run_is_live,
_paused_leg_terminal, stamp_meta, step_fingerprint from app.services.tap_envelope_common.

PROPOSAL-prompt-tap-run-envelopes turns the Tap into the system of record for what a
translation RUN did. This module owns the two envelope tables (TapAction / TapRun) and
is the SINGLE writer of them. The per-call records stay file-based (app/services/
prompt_tap.py); this module gives each call an action + run to belong to, and stamps
those ids into the tap_meta the orchestrator seams already thread.

THE OBSERVER CONTRACT (identical to prompt_tap.capture(), and load-bearing):
  * BEST-EFFORT. Every public entry point NEVER raises. Envelope recording must never
    fail, slow meaningfully, or alter a translation -- a failure to write an envelope
    degrades to "no envelope", never to a broken run. The 2026-08-06 outage lesson:
    a swallowed failure that hides for two days is worse than a loud one, so a failure
    WARNs (once per process), it does not debug-swallow.
  * ISOLATED SESSION. Every write opens its OWN short Session on app.db.engine and
    commits immediately, so an envelope write can never move the translation's
    transaction boundary or hold the SQLite writer lock across a provider call (the
    QA-1 circular-wait class). Callers invoke begin_*/finalize_* only at points where
    the translation's own session has just committed (lock free).
    THE ONE EXCEPTION is stage_run_finalize_in_session: the run's TERMINAL finalize is
    STAGED onto the RUN's own session (not an isolated one) precisely so the envelope
    reaches terminal in the SAME transaction as run.status -- there is no provider call
    in flight at that seam, and atomicity is the point (fix/tap-finalize-race). It rides
    the caller's commit inside a SAVEPOINT so a failure never poisons the run's
    transaction, and surfaces the caller's own flush errors rather than swallowing them.
  * ALWAYS ON (proposal D9). Envelope recording is on by default and is NOT governed by
    the heavy raw-call capture toggle (prompt_tap_enabled). `envelopes_enabled()` reads
    its own Setting, default "1"; ledger_controls owns the Wave C guarded OFF switch.
    An install with raw capture OFF still records envelopes ("0 calls captured" is
    the alarm the outage needed) -- but with capture OFF no per-call FILE is written.

IDS. ULIDs (proposal section 4 / 11.2): time-sortable, index-friendly, and also the
per-action call FILE name, so they are validated as a filename-safe token before that
path is built (prompt_tap._append_action_file). schema_version is stamped from
models.TAP_SCHEMA_VERSION on every row.

EVAL SEPARATION. Eval Lab traffic never mints envelope rows -- eval_runner builds
its own eval_* tap_meta and calls run_completion directly, so a Lab capture carries no
action_id and belongs to no action, by construction (proposal section 3). Advanced arm E
passes `ledger_envelope=False` to run_mission: its legacy UUID/advrun capture identifiers
do not mint production TapAction/TapRun rows.

Documented in: docs/architecture/connectors.md
"""

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import OperationalError

from app.services import settings
from app.services.tap_envelope_common import (
    _action_rollup_status,
    _action_status_from_runs,
    _iso,
    _now,
    _paused_leg_terminal,
    _run_is_live,
    resolve_finalize_status,
    stamp_meta,
    status_for_run_status,
    step_fingerprint,
)

__all__ = [
    "_action_rollup_status",
    "_action_status_from_runs",
    "_iso",
    "_now",
    "_paused_leg_terminal",
    "_run_is_live",
    "datetime",
    "resolve_finalize_status",
    "stamp_meta",
    "status_for_run_status",
    "step_fingerprint",
    "timezone",
]

log = logging.getLogger("loc_tms.tap_envelope")

ENVELOPES_SETTING = "prompt_tap_envelopes_enabled"

# Orphan reaper (residual sweep: "no reaper for orphaned actions"). A still-RUNNING
# envelope older than this many hours AND with no live backing production run is
# provably dead (a crash / kill / uvicorn-reload killed the thread that owned it) and
# is closed with the ORPHANED terminal marker. Default 24h -- no translation run lives a
# day. "0" disables the reaper. Production may raise it via the setting (a config change,
# not a code change), mirroring prompt_tap_ttl_days.
ORPHAN_REAP_SETTING = "prompt_tap_orphan_reap_hours"
ORPHAN_REAP_DEFAULT_HOURS = 24

# Crockford base32 (ULID alphabet): no I, L, O, U.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Once-per-process WARNING latch (see _warn_once). An envelope write failing on every
# call must be VISIBLE, but identical failures must not spam the log.
_WARNED = False


def _warn_once(msg: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        log.warning(msg, exc_info=True)


def commit_tap_write(session, stage, *, what) -> bool:
    """Commit a best-effort TAP (observability) write, riding out TRANSIENT SQLite
    writer-lock contention with the bounded, jittered P-C5 retry (db.commit_with_retry),
    then DEGRADING cleanly instead of propagating a lock traceback. Returns True if the
    write committed, False if it was shed under sustained contention.

    WHY (R1-F1, the volume-flood finding): the Tap is observability. Under a write flood
    an envelope/enrichment commit can lose the single SQLite writer lock and raise
    `OperationalError: database is locked`. Left to surface, that either dumped a full
    traceback into the server log (the enrich detail-write incident) or -- if a tap seam
    were ever on a request/run thread -- could ride an exception up into work the Tap only
    OBSERVES. A shed tap write must therefore (1) first be RETRIED (most contention is
    transient), and (2) if the retry is exhausted, DEGRADE: log ONE clean WARN naming the
    write (`what`, which callers stamp with the taprun/action id) and drop it -- never a
    traceback, never an exception into the host. This is the observer contract's "warn,
    never crash" applied to the commit itself, and complements the outer per-function
    best-effort guard (which still catches anything unexpected and _warn_once's it).

    `stage` re-applies THIS write's ORM mutation before each attempt: commit_with_retry
    rolls back between tries (expunging pending INSERTs / reverting pending UPDATEs and
    EXPIRING the objects), so `stage` must re-fetch/re-set on the session, carrying no side
    effects. On the happy path it runs exactly once and this is byte-identical to a plain
    commit. A NON-lock error (a genuine bug, e.g. a bad column) is re-raised so the
    caller's own `except` records it loudly -- only transient contention is degraded.

    LATENCY TRADE (deliberate, not lowered for tap writes). WRITE_RETRY_ATTEMPTS=3 means a
    fully-contended commit's worst case is ~3 * BUSY_TIMEOUT_MS (5s) + jittered backoff
    (<2s) ~= 15s PER shed write; the enrich finalize path does two writes (detail +
    manifest), so ~30s worst case sits on the run thread's synchronous finalize/exit. That
    is accepted rather than shortening the tap retry, because the R1-F1 class -- a write
    from a STALE read snapshot -- fails IMMEDIATELY with SQLITE_BUSY (busy_timeout never
    engages on a snapshot conflict), so the realistic cost of the two enrich writes is just
    the ~0.45s of jittered backoff, not 30s; and the rollback between attempts RETURNS the
    pooled connection each pass, so a retrying tap write never holds a connection long
    enough to starve /ready (the whole point of PHASE 1/2 in tap_enrich.enrich). A run that
    genuinely sat ~30s here would be under a flood so sustained that its OWN work is already
    serialized on the single writer -- the tap tail is not the binding constraint.
    """
    from app import db

    try:
        db.commit_with_retry(session, stage=stage)
        return True
    except OperationalError as exc:
        if not db.is_locked_error(exc):
            raise
        # Retry exhausted on a genuinely transient lock: DEGRADE, don't propagate.
        # TRADE: a shed finalize/status write can leave a RUNNING zombie -- a half-chain or
        # an unfinalized envelope that never reaches terminal. reap_orphaned_envelopes
        # sweeps those to ABANDONED/ORPHANED once they age past the threshold with no live
        # backing run, so the orphan reaper is now LOAD-BEARING under contention, not just a
        # crash backstop. (The reaper's own commit degrades the same way, but it re-runs on
        # its next periodic pass, so a shed sweep is retried for free.)
        log.warning(
            "tap: %s shed under sustained write contention "
            "(bounded retry exhausted; observability degraded, the run is unaffected)",
            what,
        )
        try:  # noqa: SIM105
            session.rollback()
        except Exception:  # noqa: S110 -- shed already logged above; observability write only
            pass
        return False


def _encode_ulid(ms: int, rand: int) -> str:
    """48-bit timestamp (ms) + 80-bit randomness -> 26 Crockford base32 chars."""
    value = (ms << 80) | rand
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_id() -> str:
    """A fresh ULID string (26 chars). Not raise-proof by contract because it is pure
    (no I/O) -- but it never touches the DB, so it cannot fail a translation."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    return _encode_ulid(ms, secrets.randbits(80))


def envelopes_enabled() -> bool:
    """True iff envelope recording is on. Default ON (proposal D9). Fails safe to True
    on a settings read error -- envelopes are the always-on history, so a broken read
    must not silently stop recording them (the opposite failure mode from capture())."""
    try:
        val = settings.get(ENVELOPES_SETTING)
        return val != "0"
    except Exception:
        return True


def _session():
    """A fresh isolated Session on the app engine. Imported lazily so this module has
    no import-time dependency on app.db (avoids any import cycle, mirrors prompt_tap's
    lazy provider import)."""
    from sqlmodel import Session

    from app.db import engine

    return Session(engine)


def begin_action(
    *,
    kind,
    initiator="",
    surface="",
    org_id=None,
    customer_id=None,
    project_id=None,
    document_id=None,
    source_locale="",
    requested_locales=None,
    workflow_id=None,
    workflow_name="",
    step_fingerprint="",
    batch_id=None,
):
    """Create ONE TapAction (status=running) and return its id, or None on any failure
    (best-effort). Never raises."""
    if not envelopes_enabled():
        return None
    try:
        from app.models import TAP_SCHEMA_VERSION, TapAction, TapStatus

        aid = new_id()
        now = _now()
        row = TapAction(
            id=aid,
            schema_version=TAP_SCHEMA_VERSION,
            kind=kind,
            initiator=initiator or "",
            surface=surface or "",
            org_id=org_id,
            customer_id=customer_id,
            project_id=project_id,
            document_id=document_id,
            source_locale=source_locale or "",
            requested_locales=list(requested_locales or []),
            workflow_id=workflow_id,
            workflow_name=workflow_name or "",
            step_fingerprint=step_fingerprint or "",
            batch_id=batch_id,
            status=TapStatus.RUNNING,
            started_at=now,
            created_at=now,
        )
        with _session() as s:
            # P-C5: ride out transient contention, then degrade (a shed INSERT -> no
            # action, exactly as the pre-retry except-swallow returned None).
            if not commit_tap_write(s, lambda: s.add(row), what=f"begin_action {aid}"):
                return None
        return aid
    except Exception:
        _warn_once("tap_envelope: begin_action failed (ignored)")
        return None


def begin_run(*, action_id, workflow_run_id=None, target_locale="", source_locale=""):
    """Create ONE TapRun under `action_id` (status=running) and return its id, or None
    on any failure. Never raises. `action_id` None -> None (no run without an action)."""
    if not action_id or not envelopes_enabled():
        return None
    try:
        from app.models import TAP_SCHEMA_VERSION, TapRun, TapStatus

        rid = new_id()
        now = _now()
        row = TapRun(
            id=rid,
            schema_version=TAP_SCHEMA_VERSION,
            action_id=action_id,
            workflow_run_id=workflow_run_id,
            target_locale=target_locale or "",
            source_locale=source_locale or "",
            status=TapStatus.RUNNING,
            started_at=now,
            created_at=now,
        )
        with _session() as s:
            # P-C5: a shed INSERT -> no run (the pre-retry except-swallow's None).
            if not commit_tap_write(s, lambda: s.add(row), what=f"begin_run {rid}"):
                return None
        return rid
    except Exception:
        _warn_once("tap_envelope: begin_run failed (ignored)")
        return None


def find_run_by_workflow_run(workflow_run_id, *, document_id=None, target_locale=None):
    """(action_id, run_id) for the existing TapRun of this WorkflowRun, or None.

    VALIDATED against SQLite ROWID REUSE (H1/R1, cross-tenant + append-only). WorkflowRun.id
    is a rowid alias: deleting the newest run (jobs cleanup, retention, delete_workflow)
    and inserting another REUSES the integer, so a NEW run can carry a deleted run's id.
    Two guards keep that from adopting the wrong envelope:
      * this lookup is ONLY reached on a genuine RESUME -- every launcher now asserts
        is_resume explicitly (start_run / batch legs pass False; resume_* pass True), so a
        FRESH run with a reused id never gets here (begin_workflow_envelope gates on it);
      * a match must AGREE on identity: `target_locale` and the action's `document_id`
        equal the caller's (same tenant); among agreeing rows the NEWEST (created_at desc)
        wins, which is exactly this resume's own envelope.
    (An earlier revision added a created_at >= run-row floor here. It was DROPPED: the run
    row has no immutable creation time -- WorkflowRun.started_at is RE-STAMPED on every
    execution entry, including each resume -- so from the second resume of a multi-pause
    run the floor exceeded the envelope's own created_at and the run rejected its own
    envelope, forking a duplicate + orphaning the first as 'paused'. is_resume closes the
    misclassification the floor was compensating for, so the floor is unnecessary.)
    Never raises."""
    if workflow_run_id is None or not envelopes_enabled():
        return None
    try:
        from sqlmodel import select

        from app.models import TapAction, TapRun

        with _session() as s:
            rows = s.exec(
                select(TapRun)
                .where(TapRun.workflow_run_id == workflow_run_id)
                .order_by(TapRun.created_at.desc(), TapRun.id.desc())
            ).all()
            for row in rows:
                if target_locale is not None and (row.target_locale or "") != target_locale:
                    continue
                if document_id is not None:
                    act = s.get(TapAction, row.action_id)
                    if act is None or act.document_id != document_id:
                        continue
                return row.action_id, row.id
    except Exception:
        _warn_once("tap_envelope: find_run_by_workflow_run failed (ignored)")
    return None


def find_batch_action(
    batch_id, *, project_id=None, document_id=None, customer_id=None, org_id=None
):
    """The id of the ONE TapAction for this multi-locale batch launch that MATCHES the
    caller's tenant identity, or None.

    The GRAIN INVARIANT (proposal D1): a single translate CLICK is one TapAction; a
    batch that fanned out to N language pairs is that ONE action with N TapRuns. The
    batch carries an identity (WorkflowBatch.id == batch_id), so it is the natural action
    identity -- every member leg attaches its TapRun to this action rather than minting
    its own. Created ONCE, eagerly, at the launch seam; a leg only ever LOOKS IT UP here.

    VALIDATED against WorkflowBatch ROWID REUSE (B1, cross-tenant). WorkflowBatch.id is a
    SQLite rowid alias (no AUTOINCREMENT): deleting a batch -- a required operator flow
    (access.py blocks org deletion while batches exist) -- and inserting another REUSES
    the integer, so a NEW batch in tenant B can carry a DELETED batch's id from tenant A.
    Resolving an action from a BARE batch_id would then append tenant B's captures to
    tenant A's action and render them under A's project/doc/customer. So a match must
    AGREE on identity: the action's project_id / document_id / customer_id / org_id must
    equal the caller's (a non-None caller value that DIFFERS rejects the row). On a
    mismatch the caller sees "no action found" and mints a FRESH action for its own tenant
    -- exactly find_run_by_workflow_run's validate-on-read guard, the Wave A review
    meta-lesson (never move to an unenforced reference without validation on read). Among
    agreeing rows the OLDEST (created_at asc) wins so a leg is deterministic. Never
    raises."""
    if batch_id is None or not envelopes_enabled():
        return None
    try:
        from sqlmodel import select

        from app.models import TapAction

        with _session() as s:
            rows = s.exec(
                select(TapAction)
                .where(TapAction.batch_id == batch_id)
                .order_by(TapAction.created_at.asc(), TapAction.id.asc())
            ).all()
            for row in rows:
                if project_id is not None and row.project_id != project_id:
                    continue
                if document_id is not None and row.document_id != document_id:
                    continue
                if customer_id is not None and row.customer_id != customer_id:
                    continue
                if org_id is not None and row.org_id != org_id:
                    continue
                return row.id
    except Exception:
        _warn_once("tap_envelope: find_batch_action failed (ignored)")
    return None


def begin_batch_action(
    *,
    batch_id,
    requested_locales=None,
    initiator="",
    surface="",
    org_id=None,
    customer_id=None,
    project_id=None,
    document_id=None,
    source_locale="",
    workflow_id=None,
    workflow_name="",
    step_fingerprint="",
):
    """Create (idempotently) the ONE TapAction for a multi-locale batch launch (D1) and
    return its id, or None. Called EAGERLY at the batch launch seam, single-threaded,
    BEFORE any member leg dispatches -- so `requested_locales` carries ALL the click's
    target locales and no member-thread race can create a second action.

    ONE-PER-BATCH: re-checks for an existing batch action (find_batch_action, then an
    in-session re-check) and returns it rather than forking, so a repeat call -- or a
    leg's best-effort fallback when the eager write did not land -- is idempotent. Never
    raises."""
    if batch_id is None or not envelopes_enabled():
        return None
    existing = find_batch_action(
        batch_id,
        project_id=project_id,
        document_id=document_id,
        customer_id=customer_id,
        org_id=org_id,
    )
    if existing is not None:
        return existing
    try:
        from sqlmodel import select

        from app.models import TAP_SCHEMA_VERSION, TapAction, TapActionKind, TapStatus

        with _session() as s:
            # In-session re-check bounds the rare fallback race (two legs both finding no
            # eager action). The common path -- one eager create at launch -- never races.
            # Identity-validated (B1): a rowid-reused batch_id from another tenant must not
            # be adopted here either, so a mismatching row is skipped (a fresh action is
            # minted below).
            row = None
            for cand in s.exec(
                select(TapAction)
                .where(TapAction.batch_id == batch_id)
                .order_by(TapAction.created_at.asc(), TapAction.id.asc())
            ).all():
                if project_id is not None and cand.project_id != project_id:
                    continue
                if document_id is not None and cand.document_id != document_id:
                    continue
                if customer_id is not None and cand.customer_id != customer_id:
                    continue
                if org_id is not None and cand.org_id != org_id:
                    continue
                row = cand
                break
            if row is not None:
                return row.id
            aid = new_id()
            now = _now()
            new_row = TapAction(
                id=aid,
                schema_version=TAP_SCHEMA_VERSION,
                kind=TapActionKind.WORKFLOW,
                initiator=initiator or "",
                surface=surface or "",
                org_id=org_id,
                customer_id=customer_id,
                project_id=project_id,
                document_id=document_id,
                source_locale=source_locale or "",
                requested_locales=list(requested_locales or []),
                workflow_id=workflow_id,
                workflow_name=workflow_name or "",
                step_fingerprint=step_fingerprint or "",
                batch_id=batch_id,
                status=TapStatus.RUNNING,
                started_at=now,
                created_at=now,
            )
            # P-C5: a shed INSERT -> no batch action (the pre-retry except-swallow's None).
            if not commit_tap_write(
                s,
                lambda: s.add(new_row),
                what=f"begin_batch_action {aid}",
            ):
                return None
            return aid
    except Exception:
        _warn_once("tap_envelope: begin_batch_action failed (ignored)")
        return None


def begin_workflow_envelope(
    *,
    workflow_run_id,
    is_resume=False,
    initiator="",
    surface="",
    org_id=None,
    customer_id=None,
    project_id=None,
    document_id=None,
    source_locale="",
    target_locale="",
    workflow_id=None,
    workflow_name="",
    step_fingerprint="",
    batch_id=None,
    requested_locales=None,
):
    """(action_id, run_id) for a workflow run over a (document, locale).

    GRAIN (proposal D1). A NON-batch single run mints ONE action + ONE run. A BATCH leg
    (batch_id set) attaches ONLY its TapRun to the ONE action the launch seam created for
    the whole click (find_batch_action) -- so a 17-locale batch is 1 action -> 17 runs,
    not 17 actions. If the eager launch write did not land, the leg find-or-creates the
    batch action as a best-effort fallback (begin_batch_action is idempotent).

    REUSE IS GATED ON `is_resume` (H1/R1). Only a genuine resume may adopt an existing
    envelope -- and even then only one that VALIDATES on (document_id, target_locale) via
    find_run_by_workflow_run. A FRESH run (is_resume False, which the LAUNCHER asserts --
    start_run / batch legs pass False explicitly, resume_* pass True) NEVER looks up an
    envelope by workflow_run_id, so a reused rowid cannot make it adopt a deleted run's
    envelope. Returns (None, None) on failure / off. Never raises."""
    if not envelopes_enabled():
        return None, None
    if is_resume:
        existing = find_run_by_workflow_run(
            workflow_run_id, document_id=document_id, target_locale=target_locale
        )
        if existing is not None:
            # RESUME flips the paused leg back to RUNNING and re-derives the action status
            # (N7): a resume genuinely re-opens the leg, so it must not stay PAUSED in the
            # envelope while it re-executes. finalize_pair at the resume's end transitions
            # it onward (running -> terminal). Best-effort.
            r_aid, r_rid = existing
            _resume_run(r_rid)
            finalize_action_if_all_runs_done(r_aid)
            return existing
    from app.models import TapActionKind

    _locales = requested_locales or ([target_locale] if target_locale else [])
    if batch_id is not None:
        # BATCH grain: this leg only adds its TapRun to the click's one action -- but only
        # the action that VALIDATES on this leg's tenant identity (B1: a rowid-reused
        # batch_id must not adopt another tenant's action).
        aid = find_batch_action(
            batch_id,
            project_id=project_id,
            document_id=document_id,
            customer_id=customer_id,
            org_id=org_id,
        )
        if aid is None:
            aid = begin_batch_action(
                batch_id=batch_id,
                requested_locales=_locales,
                initiator=initiator,
                surface=surface,
                org_id=org_id,
                customer_id=customer_id,
                project_id=project_id,
                document_id=document_id,
                source_locale=source_locale,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                step_fingerprint=step_fingerprint,
            )
    else:
        aid = begin_action(
            kind=TapActionKind.WORKFLOW,
            initiator=initiator,
            surface=surface,
            org_id=org_id,
            customer_id=customer_id,
            project_id=project_id,
            document_id=document_id,
            source_locale=source_locale,
            requested_locales=_locales,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            step_fingerprint=step_fingerprint,
            batch_id=None,
        )
    if aid is None:
        return None, None
    rid = begin_run(
        action_id=aid,
        workflow_run_id=workflow_run_id,
        target_locale=target_locale,
        source_locale=source_locale,
    )
    if rid is None:
        return None, None
    if batch_id is not None and any(
        v is not None for v in (project_id, document_id, customer_id, org_id)
    ):
        # A batch leg attaches to a SHARED action that may already be terminal (a re-run
        # into the same batch_id): re-open it so the re-run's status + totals are live
        # (N1/N2). No-op for a fresh action (still running). GATED on the leg carrying at
        # least one identity predicate (F5): an all-None-identity caller matches a batch
        # action regardless of tenant (find_batch_action skips every None predicate), so it
        # must NOT be able to revive a foreign terminal action. The orchestrator always
        # threads identity, so this never suppresses a legitimate revive.
        _revive_action_if_terminal(aid)
    return aid, rid


def _resume_run(run_id):
    """Flip a resumed TapRun back to RUNNING (N7). Only moves a non-terminal (paused) leg;
    a leg that already reached terminal is left alone. Best-effort; never raises."""
    if not run_id:
        return
    try:
        from app.models import TapRun, TapStatus

        with _session() as s:

            def _stage():
                run = s.get(TapRun, run_id)
                if run is None or run.status in TapStatus.TERMINAL:
                    return
                run.status = TapStatus.RUNNING
                run.finished_at = None
                s.add(run)

            commit_tap_write(s, _stage, what=f"_resume_run {run_id}")
    except Exception:
        _warn_once("tap_envelope: _resume_run failed (ignored)")


def begin_direct_envelope(
    *,
    initiator="",
    surface="",
    org_id=None,
    customer_id=None,
    project_id=None,
    document_id=None,
    source_locale="",
    target_locale="",
):
    """(action_id, run_id) for a non-workflow provider pass (direct MT / ad-hoc
    translate_text). A minimal auto-action of kind 'direct' so every call belongs to
    exactly one action (proposal section 3). Returns (None, None) on failure / off."""
    if not envelopes_enabled():
        return None, None
    from app.models import TapActionKind

    aid = begin_action(
        kind=TapActionKind.DIRECT,
        initiator=initiator,
        surface=surface,
        org_id=org_id,
        customer_id=customer_id,
        project_id=project_id,
        document_id=document_id,
        source_locale=source_locale,
        requested_locales=[target_locale] if target_locale else [],
    )
    if aid is None:
        return None, None
    rid = begin_run(
        action_id=aid,
        workflow_run_id=None,
        target_locale=target_locale,
        source_locale=source_locale,
    )
    if rid is None:
        return None, None
    return aid, rid


def _finalize(model, row_id, status):
    """Transition ONE envelope row's status + stamp finished_at (a TERMINAL status
    stamps finished_at; a non-terminal transition, e.g. paused, leaves it NULL).
    Append-only: only status/finished_at move -- the recorded facts are never edited.
    Never raises.

    WOULD-CHANGE GUARD (L3): if the row is ALREADY at `status`, do nothing -- so an idempotent
    re-finalize (e.g. the wrapper's `finally` re-running after the terminal transition was
    already staged ATOMICALLY with the run's own commit, fix/tap-finalize-race) does NOT
    re-stamp a later finished_at and destroy the transaction's own timestamp. This protects the
    LEG's finished_at (finalize_run) and a single-leg action (finalize_action); the ACTION's
    finished_at still legitimately RE-ANCHORS when a re-run moves the roll-up, because that goes
    through finalize_action_if_all_runs_done (not this function), whose own F1 comment sanctions
    the re-anchor when new legs attach and re-derive the terminal status."""
    if not row_id:
        return
    try:
        from app.models import TapStatus

        finished = _now()  # stable across retries (re-evaluating _now() would drift)
        with _session() as s:

            def _stage():
                row = s.get(model, row_id)
                if row is None or row.status == status:
                    return
                row.status = status
                if status in TapStatus.TERMINAL:
                    row.finished_at = finished
                s.add(row)

            commit_tap_write(
                s, _stage, what="finalize {} {}".format(getattr(model, "__name__", model), row_id)
            )
    except Exception:
        _warn_once("tap_envelope: finalize failed (ignored)")


def finalize_run(run_id, status):
    """Transition a TapRun to `status`. Best-effort; never raises."""
    from app.models import TapRun

    _finalize(TapRun, run_id, status)


def finalize_action(action_id, status):
    """Transition a TapAction to `status`. Best-effort; never raises."""
    from app.models import TapAction

    _finalize(TapAction, action_id, status)


def finalize_action_if_all_runs_done(action_id):
    """IDEMPOTENTLY re-derive the ACTION's status from its TapRun legs and RE-ROLL its
    totals, in ONE transaction, at EVERY leg finalize (D1 grain: a multi-locale action is
    done only when all its language-pair legs are).

    STATUS (M2): mapped coherently from the legs (`_action_status_from_runs`) -- RUNNING
    while any leg runs, PAUSED while any leg is paused (so the viewer's paused filter can
    match), the rolled-up terminal status once all legs are terminal.

    NOT STICKY -- REVIVAL-AWARE (N1/N2). This does NOT early-return on a terminal action.
    A re-run dispatches FRESH member runs INTO THE SAME batch_id (rerun_batch), so a new
    leg attaches to the already-terminal batch action (revived to RUNNING on attach, see
    _revive_action_if_terminal); when that leg finalizes, the status re-derives (all
    terminal again -> terminal) and the totals re-roll to include the re-run's calls. The
    Wave-B/round-1 defect was a sticky terminal guard + a first-terminal-only roll-up: a
    re-run's spend stayed permanently invisible and `total == sum(parts)` broke; an
    ABANDONED zero-leg action could never be reopened by a successful re-run. Idempotent
    recompute from the COMMITTED children means no ordering race and no double-count.

    TOTALS ONLY WHEN TERMINAL (F2). The STATUS re-derive above is cheap (a run-rows query)
    and runs on EVERY leg finalize. The TOTALS roll-up parses the whole per-action CALL
    FILE, so it runs ONLY when the derived status is terminal -- rolling it on every leg
    finalize re-parsed the file once per leg (O(N) full-file passes on the hot finalize
    path, byte-identical I/O to the round-1 defect). The re-run's FINAL leg re-rolls, which
    is all N1 needs; a non-terminal (running/paused) action never touches the file here.

    Best-effort; never raises."""
    if not action_id:
        return
    try:
        from sqlmodel import select

        from app.models import TapAction, TapRun

        with _session() as s:
            action = s.get(TapAction, action_id)
            if action is None:
                return
            runs = s.exec(select(TapRun).where(TapRun.action_id == action_id)).all()
            statuses = [r.status for r in runs]
            new_status, is_terminal = _action_status_from_runs(statuses)
            if new_status is None:
                return  # zero runs -> the batch-finalize path owns this action's close
            # Re-roll totals from the committed children (the ONLY file read; F2). Computed
            # ONCE here (expensive: parses the per-action call file) -- the P-C5 retry below
            # only re-applies the cheap column writes on a lock, never re-parses the file.
            totals = None
            if is_terminal:
                try:
                    from app.services import tap_enrich

                    totals = tap_enrich.rollup_action_totals(s, action_id)
                except Exception:
                    totals = None
            # Decide whether anything moves (against the current snapshot) BEFORE taking the
            # writer lock -- the hot finalize path stays a pure read when nothing changed.
            would_change = (
                action.status != new_status
                or (is_terminal and totals is not None and action.totals != totals)
                or (is_terminal and action.finished_at is None)
            )
            if not would_change:
                return
            finished = _now()  # stable across retries

            def _stage():
                # Re-read the action FRESH (P-C5: a retry's rollback drops the stale write
                # snapshot, so re-selecting here lets the commit target a current snapshot
                # -- the fix for the SQLITE_BUSY the enrich detail-write incident hit).
                a = s.get(TapAction, action_id)
                if a is None:
                    return
                changed = False
                if a.status != new_status:
                    a.status = new_status
                    changed = True
                if is_terminal:
                    if totals is not None and a.totals != totals:
                        a.totals = totals
                        changed = True
                    # (re-)anchor finished_at on (re-)entering terminal / when the roll-up
                    # moved. NON-terminal (revived/running/paused) leaves it as the
                    # LAST-KNOWN finish time (F1) -- clearing it would destroy a fact.
                    if a.finished_at is None or changed:
                        a.finished_at = finished
                        changed = True
                if changed:
                    s.add(a)

            commit_tap_write(
                s,
                _stage,
                what=f"finalize_action_if_all_runs_done {action_id}",
            )
    except Exception:
        _warn_once("tap_envelope: finalize_action_if_all_runs_done failed (ignored)")


def _revive_action_if_terminal(action_id):
    """A NEW leg attached to an already-terminal (or ABANDONED zero-leg) action re-opens it
    (N1/N2). A re-run dispatches into the SAME batch_id, so its fresh leg attaches to the
    existing action -- which must return to RUNNING so the re-run's work is visible while it
    runs and its totals re-roll at the leg's finalize.

    F1: does NOT clear finished_at. The last-known finish time is a historical fact; the
    re-run's final leg re-anchors it via finalize_action_if_all_runs_done. A re-run
    orphaned by a server restart mid-daemon leaves the action RUNNING with a stale finish
    time; it is now closed by reap_orphaned_envelopes (the ORPHANED terminal marker) once
    it ages past the threshold with no live backing run -- the residual "no reaper" gap is
    closed. Idempotent (a non-terminal action is untouched). Best-effort; never raises."""
    if not action_id:
        return
    try:
        from app.models import TapAction, TapStatus

        with _session() as s:

            def _stage():
                action = s.get(TapAction, action_id)
                if action is None or action.status not in TapStatus.TERMINAL:
                    return
                action.status = TapStatus.RUNNING
                s.add(action)

            commit_tap_write(s, _stage, what=f"_revive_action_if_terminal {action_id}")
    except Exception:
        _warn_once("tap_envelope: _revive_action_if_terminal failed (ignored)")


def finalize_batch_action(
    batch_id, *, project_id=None, document_id=None, customer_id=None, org_id=None
):
    """Close the eagerly-created batch TapAction at the batch dispatcher's POST-JOIN
    finalize (batch_runs._finalize). Best-effort; never raises.

    ZERO-LEG BATCH (M1): a batch that launched NO member run (spend gate tripped /
    fail-fast / every locale already-running) still created its TapAction eagerly at
    launch, but no leg ever attaches a TapRun -- so the per-leg finalize seam never fires
    and finalize_action_if_all_runs_done bails on the empty run set, stranding the action
    'running' FOREVER. This runs once, after every launched member thread has joined, and
    closes a zero-run action as ABANDONED (a documented terminal state: the click
    dispatched no work, it did not fail). If legs DID run, it defers to the normal
    all-runs-done rollup (idempotent).

    The action is located by batch_id VALIDATED on the batch's own tenant identity (B1),
    so a rowid-reused batch_id cannot close another tenant's action."""
    if batch_id is None:
        return
    aid = find_batch_action(
        batch_id,
        project_id=project_id,
        document_id=document_id,
        customer_id=customer_id,
        org_id=org_id,
    )
    if aid is None:
        return
    # Normal path first: rolls up status + totals when legs ran (idempotent if already
    # terminal), and assembles the per-locale action manifest race-free.
    finalize_action_if_all_runs_done(aid)
    try:
        from app.services import tap_enrich

        tap_enrich.assemble_batch_manifest(aid)
    except Exception:  # noqa: S110 -- batch manifest is observability; the rollup already ran
        pass
    try:
        from sqlmodel import select

        from app.models import TapAction, TapRun, TapStatus

        with _session() as s:
            action = s.get(TapAction, aid)
            if action is None or action.status in TapStatus.TERMINAL:
                return
            runs = s.exec(select(TapRun).where(TapRun.action_id == aid)).all()
            if not runs:
                finished = _now()  # stable across retries

                def _stage():
                    a = s.get(TapAction, aid)
                    if a is None or a.status in TapStatus.TERMINAL:
                        return
                    a.status = TapStatus.ABANDONED
                    a.finished_at = finished
                    s.add(a)

                commit_tap_write(s, _stage, what=f"finalize_batch_action {aid}")
    except Exception:
        _warn_once("tap_envelope: finalize_batch_action failed (ignored)")


def finalize_pair(action_id, run_id, status):
    """Finalize a run leg and re-derive its action's status (and, when every leg is
    terminal, roll up the action totals). `status` is the RUN's outcome; the action's
    status is mapped from ALL its legs (finalize_action_if_all_runs_done), so a single-run
    action finalizes immediately, a multi-locale action waits for every leg, and a paused
    leg surfaces the action as paused (M2). Best-effort."""
    finalize_run(run_id, status)
    finalize_action_if_all_runs_done(action_id)


def stage_run_finalize_in_session(
    session, *, workflow_run_id, document_id=None, target_locale=None, status
) -> None:
    """STAGE this workflow run's envelope leg terminal transition + its action's re-derived
    status onto the CALLER's OWN session (Core UPDATEs, NO commit) so they persist ATOMICALLY
    with the caller's next commit -- the finalize-race fix (fix/tap-finalize-race).

    WHY the caller's session, not the module's own isolated one. The observer-isolation
    contract (module docstring) keeps every OTHER envelope write on its OWN short session so a
    write can never hold the SQLite writer lock ACROSS A PROVIDER CALL (the circular-wait
    class). At the RUN'S TERMINAL COMMIT there is no provider call in flight, and the goal is
    the OPPOSITE of isolation: the envelope must reach terminal in the SAME transaction as the
    run's own terminal status, so no observer can ever see the run terminal with the envelope
    still 'running'. A SEPARATE write here (proven) adds a connection that contends for the
    writer lock against a batch's concurrent member/dispatcher sessions -> 'database is
    locked'. This stages onto the caller's session -- it adds NO new connection: the leg lookup
    and the Core UPDATEs all run on `session`, riding the run's existing terminal commit.

    Only the status/finished_at columns move (Core UPDATE, so no ORM identity-map churn and no
    optimistic-version StaleData). TOTALS + manifest/detail ENRICHMENT stay on the isolated
    best-effort path (the wrapper's `finally` -> _finalize_run_envelope), which runs after this
    commit at the quiescent seam and remains the reconciling backstop.

    ERROR SEMANTICS (append-only observer, 2026-08-06 loud-not-silent lesson):
      * the CALLER's own pending-write failure surfaces AS ITSELF -- an explicit unguarded
        `session.flush()` first forces it here, so it is never swallowed into a lost-root-cause
        PendingRollbackError at the run's commit;
      * OUR staging runs inside a SAVEPOINT (`begin_nested`), so it is all-or-nothing AND a
        failure rolls back ONLY the savepoint -- the caller's run commit is never poisoned --
        and is WARNed once, not debug-swallowed.
    finished_at is stamped ONLY on a terminal transition and NEVER cleared (mirrors _finalize;
    clearing a non-terminal re-derive would destroy a paused/rerun action's last finish time)."""
    # FLUSH-POINT DIVERGENCE (N5): when envelopes are OFF (or no run id) we return HERE, before
    # the flush below -- so the caller's pending writes flush at ITS OWN commit, exactly as they
    # did before this fix. With envelopes ON, the flush is pulled a few statements earlier (to
    # the stage). Either way the caller's own error surfaces as itself; only the flush POINT
    # moves, and only when recording is on.
    if workflow_run_id is None or not envelopes_enabled():
        return
    # Surface the CALLER's own pending-write failures as themselves, before we touch anything.
    # Unguarded on purpose: a failure here is the caller's and must propagate (it would have
    # raised at the run's commit regardless), never be swallowed by our best-effort guard.
    session.flush()
    try:
        from sqlalchemy import update as _update
        from sqlmodel import select

        from app.models import TapAction, TapRun, TapStatus

        now = _now()
        with session.begin_nested():  # SAVEPOINT: all-or-nothing, caller-txn-safe
            with session.no_autoflush:
                # Locate THIS run's leg, rowid-reuse safe (match target_locale + the action's
                # document_id, newest first) -- the find_run_by_workflow_run logic, on the
                # caller's session (column selects only, so no ORM object is loaded/dirtied).
                rows = session.exec(
                    select(TapRun.id, TapRun.action_id, TapRun.target_locale, TapAction.document_id)
                    .join(TapAction, TapAction.id == TapRun.action_id)
                    .where(TapRun.workflow_run_id == workflow_run_id)
                    .order_by(TapRun.created_at.desc(), TapRun.id.desc())
                ).all()
                run_id = action_id = None
                for rid, aid, tloc, adoc in rows:
                    if target_locale is not None and (tloc or "") != target_locale:
                        continue
                    if document_id is not None and adoc != document_id:
                        continue
                    run_id, action_id = rid, aid
                    break
                if run_id is None:
                    return  # no matching envelope -> nothing to stage
                sib = session.exec(
                    select(TapRun.id, TapRun.status).where(TapRun.action_id == action_id)
                ).all()
            statuses = [status if rid == run_id else st for (rid, st) in sib]
            if not any(rid == run_id for (rid, _st) in sib):
                statuses.append(status)  # defensive: the leg row should already exist
            action_status, action_terminal = _action_status_from_runs(statuses)
            # Stage the leg. finished_at ONLY when terminal, never cleared (M1/F1).
            leg_vals = {"status": status}
            if status in TapStatus.TERMINAL:
                leg_vals["finished_at"] = now
            session.execute(_update(TapRun).where(TapRun.id == run_id).values(**leg_vals))
            if action_status is not None:
                act_vals = {"status": action_status}
                if action_terminal:  # never clear a non-terminal action's finish time
                    act_vals["finished_at"] = now
                session.execute(
                    _update(TapAction).where(TapAction.id == action_id).values(**act_vals)
                )
    except Exception:
        # caller's run write is intact, and the wrapper's `finally` will finalize the envelope.
        _warn_once("tap_envelope: stage_run_finalize_in_session failed (ignored)")


def mark_capture_withheld(action_id) -> None:
    """Flip TapAction.capture_withheld True (disk-budget backstop, H2 / proposal section
    7). Best-effort, idempotent, never raises. The envelope keeps recording; this says
    plainly that its per-call file stopped growing rather than the volume being silently
    eaten."""
    if not action_id:
        return
    try:
        from app.models import TapAction

        with _session() as s:

            def _stage():
                row = s.get(TapAction, action_id)
                if row is None or row.capture_withheld:
                    return
                row.capture_withheld = True
                s.add(row)

            commit_tap_write(s, _stage, what=f"mark_capture_withheld {action_id}")
    except Exception:
        _warn_once("tap_envelope: mark_capture_withheld failed (ignored)")


def orphan_reap_hours() -> int:
    """Age threshold (hours) for the orphan reaper; default 24, "0" disables. Fails safe
    to the default on a bad read -- the reaper is best-effort housekeeping, never a
    translation-breaking path. Never raises."""
    try:
        val = settings.get(ORPHAN_REAP_SETTING, ORPHAN_REAP_DEFAULT_HOURS)
        return max(0, int(str(val if val is not None else ORPHAN_REAP_DEFAULT_HOURS)))
    except (TypeError, ValueError):
        return ORPHAN_REAP_DEFAULT_HOURS


def reap_orphaned_envelopes(now=None, max_age_hours=None) -> dict:
    """Close envelope rows left RUNNING by a crash / kill / thread-death -- the residual
    the _revive_action_if_terminal + TapAction.finished_at comments call out as having
    "no reaper". A RUNNING envelope aged past the threshold AND with no live backing
    production run is PROVABLY dead; it is TRANSITIONED to a terminal status (finished_at
    stamped), never deleted, so the action->run->call hierarchy and the TTL semantics are
    preserved and a reader sees an honest outcome rather than a perpetual 'running'.

    HONEST STATUS, NOT A BLANKET ORPHANED (M1). Each aged, non-live action is closed by
    ROLLING UP its legs (_action_rollup_status): all-completed -> COMPLETED, all-failed ->
    FAILED, mixed with an orphaned leg -> ORPHANED, etc. ORPHANED is stamped on a LEG only
    when there is no terminal evidence for it (a RUNNING leg whose backing run is
    gone/terminal, or a direct/ad-hoc leg with no backing run at all -- reaped on age
    ALONE, L7). A prior revision blanket-stamped the action ORPHANED, which ERASED
    completed/failed leg history.

    PROVABLY DEAD -- an action is reaped only when NO leg is genuinely live:
      * a leg whose backing WorkflowRun is still non-terminal (pending/running/paused)
        means the run is really executing / a human is really mid-review -> the whole
        action is SKIPPED (never reaped mid-flight).
      * a PAUSED tap leg is durable human state (M2): the action is NEVER terminalized
        while such a leg is genuinely pending. Only when its backing WorkflowRun is itself
        terminal (reachable via run-cancel, which closes pending human work) is the LEG
        closed first (to _paused_leg_terminal) and then rolled up; a PAUSED leg with no
        terminal evidence (backing missing) SKIPS the action.

    ZERO-LEG aged action -> ABANDONED (L8), aligning with finalize_batch_action (a click
    that dispatched no work did not fail). Totals are re-rolled from the committed children
    on reap (L6). Runs EVEN WHEN envelope recording is disabled (L5): a frozen RUNNING row
    from before the toggle was flipped must still be closed. Best-effort -- never raises.
    Idempotent (a second pass finds nothing). Returns {"actions": n, "runs": m} closed."""
    result = {"actions": 0, "runs": 0}
    hours = max_age_hours if max_age_hours is not None else orphan_reap_hours()
    if hours <= 0:
        return result  # reaper disabled
    # NOTE (L5): NOT gated on envelopes_enabled() -- disabling recording must not strand a
    # row that was RUNNING when recording was still on. The reaper only ever CLOSES existing
    # rows; it never creates one.
    try:
        from sqlmodel import select

        from app.models import RunStatus, TapAction, TapRun, TapStatus, WorkflowRun

        _RUN_NONTERMINAL = (
            RunStatus.PENDING,
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
        )
        now = now or _now()
        cutoff = now - timedelta(hours=hours)
        with _session() as s:
            # The whole sweep is the P-C5 `stage`: commit_tap_write re-runs it on a fresh
            # snapshot after a transient lock (idempotent -- a re-run re-selects the still-
            # aged rows), so `result` is rebuilt from zero on every attempt. Off the hot
            # path (a periodic housekeeping pass), so re-scanning on a rare lock is cheap.
            def _sweep():
                result["actions"] = 0
                result["runs"] = 0
                # (1) action-driven sweep: an aged RUNNING action with no genuinely-live leg.
                actions = s.exec(
                    select(TapAction).where(
                        TapAction.status == TapStatus.RUNNING,
                        TapAction.started_at < cutoff,
                    )
                ).all()
                for action in actions:
                    runs = s.exec(select(TapRun).where(TapRun.action_id == action.id)).all()
                    skip = False
                    closeable_paused = []  # (leg, backing) -- paused legs safe to close
                    for r in runs:
                        backing = (
                            s.get(WorkflowRun, r.workflow_run_id)
                            if r.workflow_run_id is not None
                            else None
                        )
                        if backing is not None and backing.status in _RUN_NONTERMINAL:
                            skip = True  # a real run is executing / a human is reviewing
                            break
                        if r.status == TapStatus.PAUSED:
                            if backing is not None:  # backing is terminal (not non-terminal)
                                closeable_paused.append((r, backing))
                            else:
                                skip = True  # paused, backing gone -> no terminal evidence
                                break
                    if skip:
                        continue
                    # Close PAUSED-with-terminal-backing legs to their honest terminal (M2).
                    for r, backing in closeable_paused:
                        r.status = _paused_leg_terminal(backing.status)
                        r.finished_at = now
                        s.add(r)
                        result["runs"] += 1
                    # Close still-RUNNING legs (crash / age) with the ORPHANED marker.
                    for r in runs:
                        if r.status == TapStatus.RUNNING:
                            r.status = TapStatus.ORPHANED
                            r.finished_at = now
                            s.add(r)
                            result["runs"] += 1
                    # Roll the ACTION up honestly from ALL its (now-terminal) legs (M1/L8).
                    statuses = [r.status for r in runs]
                    if not statuses:
                        action.status = TapStatus.ABANDONED  # zero-leg click (L8)
                    elif all(st in TapStatus.TERMINAL for st in statuses):
                        action.status = _action_rollup_status(statuses)
                    else:
                        action.status = TapStatus.ORPHANED  # defensive (unreachable)
                    if action.finished_at is None:
                        action.finished_at = now
                    # Re-roll totals from the committed children (L6); best-effort.
                    try:
                        from app.services import tap_enrich

                        totals = tap_enrich.rollup_action_totals(s, action.id)
                        if totals is not None:
                            action.totals = totals
                    except Exception:  # noqa: S110 -- totals re-roll; prior totals are kept
                        pass
                    s.add(action)
                    result["actions"] += 1
                # (2) stray-leg sweep: an aged RUNNING TapRun whose parent action is already
                # terminal/gone (a crash between the leg finalize and the action finalize).
                # Its action is settled, so the leg alone is the orphan. PAUSED legs untouched.
                stray = s.exec(
                    select(TapRun).where(
                        TapRun.status == TapStatus.RUNNING,
                        TapRun.started_at < cutoff,
                    )
                ).all()
                for r in stray:
                    if _run_is_live(s, r.workflow_run_id):
                        continue
                    act = s.get(TapAction, r.action_id)
                    if act is not None and act.status not in TapStatus.TERMINAL:
                        continue  # handled by the action-driven sweep (or still live)
                    r.status = TapStatus.ORPHANED
                    r.finished_at = now
                    s.add(r)
                    result["runs"] += 1

            if not commit_tap_write(s, _sweep, what="reap_orphaned_envelopes"):
                # Shed under sustained contention: nothing persisted -> report honestly.
                result["actions"] = 0
                result["runs"] = 0
    except Exception:
        _warn_once("tap_envelope: reap_orphaned_envelopes failed (ignored)")
    return result
