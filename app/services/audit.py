"""Audit log — append-only, tamper-evident record of every mutating action.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports AUDIT_WRITE_ATTEMPTS, enabled, fail_open, _mac_key,
_compute_hash, _legacy_hash, _anchor_mac, _as_id, _parse_iso, _chain_status_mac,
_CHAIN_BROKEN_ACTION, _ANCHOR_RESET_ACTION, _format_chain_broken_detail, _parse_break_class from
app.services.audit_break_class_helpers_log_.

Each row stores prev_hash (the row_hash of the previous entry) and
row_hash = HMAC-SHA256(key, canonical JSON array including customer_id), forming a
keyed hash chain. The MAC key is retained in a dedicated audit keyfile (a file
OUTSIDE the database), so an attacker with database access alone cannot rewrite
a row and recompute a chain that verifies.

Tail truncation ("delete my last N rows") is caught by a head anchor: every
write records "<head_id>:<head_hash>:<mac>" in the Setting table; verify_chain
fails when the newest row id has moved backwards. The anchor is itself MAC'd,
so it cannot be forged without the key either.

Honest limits: historical pipe-joined rows retain field-boundary ambiguity and
unsigned customer attribution. The filesystem-pinned .audit_format boundary
limits all legacy verification to pre-upgrade row ids, before any v2 row. Within
that epoch, empty leading hashes are skipped and unkeyed sha256 is accepted only
before the first keyed row (also bounded by the anchor).
An attacker who obtains the keyfile as well as the DB can defeat all of this;
that requires filesystem access, at which point nothing app-level survives.

Documented in: docs/architecture/audit-chain.md
"""

import hashlib
import hmac as _hmac
import json
import logging
import threading
import time
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps

from sqlalchemy import func
from sqlmodel import Session, desc, select

from app import config, db
from app import db as _db_relief
from app.db import engine
from app.models import AuditLog, Setting, User
from app.services import audit_state, settings
from app.services.audit_break_class_helpers_log_ import (
    _ANCHOR_RESET_ACTION,
    _CHAIN_BROKEN_ACTION,
    AUDIT_WRITE_ATTEMPTS,
    LEGACY_RESTORE_HINT,
    _anchor_mac,
    _as_id,
    _chain_status_mac,
    _compute_hash,
    _format_chain_broken_detail,
    _legacy_hash,
    _mac_key,
    _parse_break_class,
    _parse_iso,
    _v1_hash,
    enabled,
    fail_open,
)

__all__ = [
    "AUDIT_WRITE_ATTEMPTS",
    "LEGACY_RESTORE_HINT",
    "_ANCHOR_RESET_ACTION",
    "_CHAIN_BROKEN_ACTION",
    "_anchor_mac",
    "_as_id",
    "_chain_status_mac",
    "_compute_hash",
    "_format_chain_broken_detail",
    "_legacy_hash",
    "_mac_key",
    "_parse_break_class",
    "_parse_iso",
    "config",
    "enabled",
    "fail_open",
    "hashlib",
    "timezone",
]

_logger = logging.getLogger("loc_tms.audit")

# Writes arrive from request threads and job worker threads; the
# read-latest-hash + insert must be atomic or two rows could chain off the
# same predecessor and the chain would fork.
_chain_lock = threading.RLock()
_WITNESS_ERROR = False
_WITNESS_NOTE = ""


def _witness_failed(exc):
    global _WITNESS_ERROR, _WITNESS_NOTE
    if not _WITNESS_ERROR:
        _logger.error(
            "AUDIT TRUST STATE UNREADABLE: %s. Audit chain is BROKEN. "
            "Restore matching key files; stop the server before reset-anchor if needed.",
            exc,
        )
    _WITNESS_ERROR = True
    _WITNESS_NOTE = "Audit trust state unreadable; restore files or reset-anchor."


def _witness_initialized():
    global _WITNESS_NOTE
    _WITNESS_NOTE = "Audit witness initialized while a DB anchor already exists."
    _logger.warning(_WITNESS_NOTE)


def _trust_write(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            with audit_state.process_lock(), audit_state.buffered_witness(_chain_lock):
                return function(*args, **kwargs)
        except audit_state.ProcessLockError:
            _signing_drop(kwargs.get("action", ""), reason="audit process lock unavailable")
        except audit_state.TrustStateError as exc:
            # No signing material: do not invent a new signing key or an unsigned row.
            _witness_failed(exc)
            _signing_drop(kwargs.get("action", ""))

    return guarded


def _advance_witness(head_id, head_hash, **kwargs):
    try:
        audit_state.advance(head_id, head_hash, **kwargs)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _witness_failed(exc)


_ANCHOR_KEY = "audit_anchor"  # Setting row: "<head_id>:<head_hash>:<keyed_from>:<mac>"
_VERIFY_BATCH = 500  # rows per DB round-trip in verify_chain

# Scheduled-verification monitoring (gap G-10 / AU-05). The last scheduled verify_chain()
# result + the broken-chain re-alert LATCH live in ONE Setting row holding a JSON snapshot,
# read/written DIRECTLY (like _ANCHOR_KEY) so no settings-cache side effect / apply_to_config()
# runs on the scheduler tick. Shape: {"state": "ok"|"broken", "ok": bool, "first_bad_id":
# int|None, "rows_checked": int, "last_checked": iso, "last_alert": iso|None}.
_CHAIN_STATUS_KEY = "audit_chain_status"
# While the chain stays BROKEN at the same first_bad_id, re-emit the forced chain_broken row at
# most this often (hours). Below this window the alert is latched to a single row per break; a
# state change (first_bad_id moves, or broken->ok) always re-alerts regardless. "Re-alert only
# on state change or daily" — so a persistent break never spams a row on every scheduled run.
_CHAIN_REALERT_HOURS = 24

# R3: process-local re-alert latch for the STATUS-ROW-TAMPERED path. The tampered handler
# re-runs a full O(n) verify_chain + writes a chain_status_tampered row; without a latch a
# persistently re-forged status row would trigger that on EVERY 30s tick. The latch cannot live
# in the status row itself (that is exactly what the attacker overwrites), so it is a
# process-local wall-clock stamp: at most one tamper-handling per _CHAIN_REALERT_HOURS per
# process. Resets on restart (fail-safe: a restart re-checks immediately).
_CHAIN_TAMPER_HANDLED_AT = None
# #433: the tamper re-verify hit lock contention after the tampered row was written. The
# latch is set, so a retry is owed WITHOUT writing a second tampered row every tick.
_CHAIN_TAMPER_REVERIFY_PENDING = False

# Once-per-process WARNING latch for a FAILED posture snapshot read (see log()). A
# non-transient failure — a renamed/removed `audit_fail_open` key, a settings-layer breakage
# — must be VISIBLE (we silently assumed fail-OPEN, possibly overriding an operator's
# deliberate fail-CLOSED choice), but identical failures on every mutating request must not
# spam the log. Mirrors tap_envelope._warn_once.
_POSTURE_WARNED = False

# #235: process-local count of audit rows DROPPED (shed under writer-lock contention with the
# fail-open posture, or refused with fail-closed). Every drop also logs at ERROR with the
# running total, the line a host log alert keys on. Read with dropped_writes().
_DROPPED_WRITES = 0

# Gate round 1 L1 (fix-audit-settings): True inside a request whose own audit row the
# middleware already wrote BEFORE the handler (the fail-closed pre-record). A later row in
# that request (a handler's detail row) that sheds is then handled fail-open: logged at ERROR
# and counted, never raised, because the request is already on record and its change may
# have landed. Set and reset by app/main.py::_security_audit only.
REQUEST_RECORDED: ContextVar = ContextVar("audit_request_recorded", default=False)
_DROPPED_LOCK = threading.Lock()
_SIGNING_DROP_LOG_AT = None


def dropped_writes() -> int:
    """How many audit rows this process has failed to write since start."""
    with _DROPPED_LOCK:
        return _DROPPED_WRITES


def _count_dropped() -> int:
    global _DROPPED_WRITES
    with _DROPPED_LOCK:
        _DROPPED_WRITES += 1
        return _DROPPED_WRITES


def _signing_drop(action, *, reason="signing material unavailable"):
    """Count every unsigned drop, with an ERROR summary at most once per second."""
    global _SIGNING_DROP_LOG_AT
    total = _count_dropped()
    now = time.monotonic()
    with _DROPPED_LOCK:
        report = _SIGNING_DROP_LOG_AT is None or now - _SIGNING_DROP_LOG_AT >= 1
        if report:
            _SIGNING_DROP_LOG_AT = now
    if report:
        _logger.error(
            "audit: DROPPED row for action %r: %s "
            "(%d total drops since process start; rate limited to one log per second).",
            action,
            reason,
            total,
        )
    try:
        open_posture = fail_open()
    except Exception as exc:
        _warn_posture_read_failed(exc)
        open_posture = True
    if not open_posture and not REQUEST_RECORDED.get():
        raise db.WriteContentionError(f"audit.log {action}")


def _engine():
    """The engine this scope must use.

    Normally the module's pooled `engine`, byte-identically. Inside a RELIEF scope — a
    cancel or `/settings` request the capacity doorman admitted at a fully checked-out pool
    (gate A round 3, HIGH-4) — the POOL-LESS engine, so the identity/audit reads on the way
    to the one action that ENDS in-flight work cannot themselves wait on the pool that
    in-flight work has exhausted. The pooled engine is passed IN (not read from app.db) so a
    suite that rebound this module's `engine` keeps its own."""
    return _db_relief.relief_engine_or(engine)


def _warn_posture_read_failed(exc: Exception) -> None:
    global _POSTURE_WARNED
    if not _POSTURE_WARNED:
        _POSTURE_WARNED = True
        _logger.warning(
            "audit: could not read the audit_fail_open posture setting (%s: %s) — assuming "
            "fail-OPEN. If a fail-CLOSED posture was configured it is NOT being honoured; "
            "this is likely a settings-layer breakage or a renamed key, not transient "
            "contention. (Logged once per process.)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )


def _read_anchor():
    """(head_id, head_hash, keyed_from) or None (no anchor yet) or "invalid"
    (present but malformed / bad MAC — i.e. tampered).

    The Setting row is read/written directly (NOT via settings.get/set_many):
    the settings cache + its apply_to_config() side effect must not run on
    every audit write."""
    with Session(_engine()) as s:
        row = s.get(Setting, _ANCHOR_KEY)
    raw = row.value if row else ""
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 4 or not parts[0].isdigit() or not parts[2].isdigit():
        return "invalid"
    head_id, head_hash, keyed_from, mac = int(parts[0]), parts[1], int(parts[2]), parts[3]
    if not _hmac.compare_digest(mac, _anchor_mac(head_id, head_hash, keyed_from)):
        return "invalid"
    return (head_id, head_hash, keyed_from)


def _write_anchor(
    head_id: int,
    head_hash: str,
    allow_regress: bool = False,
    *,
    external=audit_state._UNREAD,
    prior=audit_state._UNREAD,
) -> bool:
    """(Re)write the tamper-evident head anchor. Returns True on commit, False if the
    update was SHED under sustained writer-lock contention. Callers hold `_chain_lock`, so
    the shed WARN is emitted by the CALLER after the lock releases (pure I/O under an
    app-wide lock is a serialization point).

    MONOTONIC (F2): a normal write NEVER moves the stored head BACKWARDS. A lower head_id
    means tail rows were deleted (truncation) — e.g. the sweep's own forced chain_broken row
    gets a lower id than a truncated-away head — so keeping the higher stored head preserves
    the truncation evidence and verify_chain's tail check (last_id < anchor head) keeps
    firing. Only `reset_anchor` (audit reset-anchor) deliberately re-baselines to a lower
    head after a LEGITIMATE `db restore` to an older backup, via allow_regress=True."""
    # `prior` lets log() hand over the anchor it already read under the same chain and
    # process locks (nothing can change it in between), saving one session and SELECT per row.
    if prior is audit_state._UNREAD:
        prior = _read_anchor()
    if not allow_regress and isinstance(prior, tuple) and prior[0] > head_id:
        _advance_witness(prior[0], prior[1], prior=external)
        # Refuse the regression: keep the evidence-bearing higher anchor (truncation stays
        # detected). A no-op "success" — nothing to shed, nothing to warn about.
        return True
    keyed_from = prior[2] if isinstance(prior, tuple) else head_id
    if keyed_from > head_id:
        # Only reachable on a deliberate regress (restore to an older head): clamp so
        # keyed_from never points past the head. Post-restore rows are keyed and set
        # seen_keyed on the first row, so the permissive legacy window is inert in practice.
        keyed_from = head_id
    value = f"{head_id}:{head_hash}:{keyed_from}:{_anchor_mac(head_id, head_hash, keyed_from)}"
    with Session(_engine()) as s:

        def _stage():
            row = s.get(Setting, _ANCHOR_KEY)
            if row:
                row.value = value
            else:
                row = Setting(key=_ANCHOR_KEY, value=value)
            s.add(row)

        # Flood R3-F1: ride out transient writer-lock contention, then DEGRADE. The anchor
        # is the tail-truncation guard; a shed update leaves it pointing at the PREVIOUS
        # head, which verify_chain still ACCEPTS (the newest real row id is now GREATER than
        # the stored head, and the tail check only fails when last_id < stored head — i.e.
        # on genuine deletion), and which self-heals on the next successful audit write. So a
        # persistent lock here must never raise a 500 up through the middleware. attempts=1
        # (fold-in): because the anchor is stale-TOLERANT and self-healing, it is the LEAST
        # valuable retry while costing the SECOND-most lock time — so it gets a single try,
        # no jittered retry, dropping the worst-case _chain_lock hold from ~4×busy_timeout
        # (2 row + 2 anchor) to ~2×+1 (2 row + 1 anchor). The caller warns (outside the lock).
        committed = db.commit_or_shed(s, stage=_stage, attempts=1)
    if committed:
        if external is None and prior is not None:
            _witness_initialized()
        _advance_witness(head_id, head_hash, allow_regress=allow_regress, prior=external)
    return committed


_refusal_lock = threading.Lock()
_refusal_window = 0.0
_refusal_count = 0
_refusal_dropped = 0


def allow_refusal_record() -> bool:
    """Bound anonymous refusal rows to 60 per minute per worker, with constant memory."""
    global _refusal_window, _refusal_count, _refusal_dropped
    dropped = 0
    with _refusal_lock:
        now = time.monotonic()
        if now - _refusal_window >= 60:
            dropped, _refusal_dropped = _refusal_dropped, 0
            _refusal_window, _refusal_count = now, 0
        if _refusal_count >= 60:
            _refusal_dropped += 1
            return False
        _refusal_count += 1
    # Write through the normal audit chain, outside the sampling lock. The next
    # request after a window closes flushes its count without a timer or worker.
    if dropped:
        log(action="request.refused.suppressed", detail=f"dropped={dropped}")
    return True


@_trust_write
def log(
    actor: str = "system",
    action: str = "",
    target: str = "",
    detail: str = "",
    ip: str = "",
    force: bool = False,
    customer_id: int | None = None,
    org_id: int | None = None,
) -> None:
    """Append an audit row. Normally a no-op when audit_enabled="0" (the global,
    admin-toggleable gate). `force=True` (Findings 4/5) writes the row
    UNCONDITIONALLY — used for the OPERATOR verbs, so that whoever can turn the
    global gate off cannot then perform an un-audited install-wide write. The class
    is `org.*`, `user.move_org` and (since 2026-09-20) `template.delete` when it
    comes through `cli template delete --operator`, which is the only surviving door
    to a GLOBAL template. `impersonate.*` was in this class until the back-office was
    removed; the rationale then was a super-admin disabling audit before a cross-org
    write, and there is no such account any more, but the rule outlives it because an
    operator with shell access is exactly as able to flip the gate. A forced row is
    written through the SAME HMAC-chained path (identical chaining + anchor), so it
    is tamper-evident exactly like a normal row — only the enabled() short-circuit
    is skipped.

    `customer_id` (R2-3, item 3) ties the row to one customer so a customer-bound
    user's /audit view shows ONLY its own customer's rows. NULL (the default) = a
    SYSTEM / internal event (auth, admin, settings, org). Callers with
    a project / connection / customer subject stamp it — usually via the
    customer_for_* derivation helpers below (under-stamping is SAFE: a NULL row is
    simply hidden from customers, never leaked). New rows authenticate customer_id
    in the versioned canonical payload; historical v1 rows did not.

    `org_id` (#487) is the org the row belongs to. Left None it is stamped from the ACTOR'S
    account at WRITE time (`_actor_org_id`), so a user later moved to another org leaves
    the old rows behind in the old org. NULL = system / unauthenticated / no account, never
    shown to any org. Like customer_id it sits outside the hashed payload."""
    if not force and not enabled():
        return
    if org_id is None:
        org_id = _actor_org_id(actor)
    # SNAPSHOT the contention posture NOW — before _chain_lock and before the write flood
    # the degrade branch reacts to. Reading the posture at DEGRADE time — deep inside the
    # writer-lock contention that triggered the degrade, holding _chain_lock — would be
    # lock-exposed: the settings cache is PER-KEY, and a MISS on `audit_fail_open` opens a DB
    # session (settings.get's miss path is unguarded). Snapshot it here so the shed-handling
    # branch reads a LOCAL bool and never touches the DB. The read is GUARDED: with force=True
    # the enabled() short-circuit above is skipped, so on a forced row this is the ONLY settings
    # read — a cold key against a locked DB would otherwise raise a raw OperationalError out of
    # the audit tail (a 500 on an otherwise-successful mutating request, BEFORE any write). A
    # locked/cold read therefore defaults to owner-ratified fail-open posture, never raises.
    try:
        fail_open_posture = fail_open()
    except Exception as exc:
        fail_open_posture = True  # the ratified default posture
        _warn_posture_read_failed(exc)  # once-per-process: a NON-transient failure stays visible

    # Fold-in: the two degrade WARNs (pure I/O) are emitted AFTER `_chain_lock` releases —
    # logging under an app-wide lock is a serialization point every mutating request would
    # queue behind. Inside the lock we only decide; outside it we warn (and, fail-closed,
    # raise). `row_shed`/`anchor_shed` carry the two decisions across the boundary.
    row_shed = False
    anchor_shed = False
    with _chain_lock, audit_state.cached_trust(), audit_state.snapshot():
        # Preserve a detected rollback BEFORE a new row can reuse a deleted id.
        try:
            external = audit_state.read(trust_own_publish=True)
        except audit_state.TrustStateError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _witness_failed(exc)
            external = audit_state._UNREAD
        anchor_prior = audit_state._UNREAD
        if isinstance(external, dict):
            with Session(_engine()) as check_session:
                witness = check_session.get(AuditLog, external["id"])
            anchor_prior = _read_anchor()
            if witness is None or witness.row_hash != external["hash"] or anchor_prior is None:
                external = {**external, "broken": True}
                try:
                    audit_state.write(external)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    _witness_failed(exc)
        with Session(_engine()) as s:
            last = s.exec(select(AuditLog).order_by(desc(AuditLog.id)).limit(1)).first()
            try:
                audit_state.legacy_through(last.id if last else 0)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise audit_state.TrustStateError("Audit format boundary unavailable") from exc
            prev_hash = last.row_hash if last else ""
            row = AuditLog(
                actor=actor or "system",
                action=action,
                target=target,
                detail=detail,
                ip=ip,
                prev_hash=prev_hash,
                customer_id=customer_id,
                org_id=org_id,
            )
            row.row_hash = _compute_hash(
                prev_hash,
                row.ts.isoformat(),
                row.actor,
                row.action,
                row.target,
                row.detail,
                row.ip,
                customer_id=row.customer_id,
            )
            # Flood R3-F1: ride out transient writer-lock contention, then DEGRADE. Under a
            # sustained write flood this INSERT can lose the single SQLite writer lock; left
            # to surface it raised OperationalError('database is locked') straight up through
            # the middleware's `_audit_mutating_request` -> a 500 on an otherwise-successful
            # mutating request (two such 500s in the R3 flood). `_chain_lock` is held here, so
            # `last`/prev_hash cannot shift under us across the retry (no other audit writer
            # interleaves — only NON-audit writers contend for the lock), which makes
            # re-staging the same row on each attempt correct. `keep_loaded` reads the new
            # row's PK for the anchor WITHOUT a lock-prone post-commit refresh SELECT (created_at
            # etc. are Python-side default_factory values, already populated).
            if db.commit_or_shed(
                s, stage=lambda: s.add(row), attempts=AUDIT_WRITE_ATTEMPTS, keep_loaded=True
            ):
                head_id, head_hash = row.id, row.row_hash
            else:
                row_shed = True
        if not row_shed:
            # Still inside `_chain_lock` (the anchor must reflect the head just written, with
            # no other audit writer interleaving), but its shed WARN is deferred below.
            anchor_shed = not _write_anchor(
                head_id, head_hash, external=external, prior=anchor_prior
            )
    if row_shed and not fail_open_posture and REQUEST_RECORDED.get():
        fail_open_posture = True  # L1: the request's own row is already written
    if row_shed:
        # SHED after the bounded retry. ONE loud WARN naming the dropped action either way;
        # the posture (snapshotted above as `fail_open_posture`, from the audit_fail_open
        # Setting) decides whether the request then proceeds unaudited or raises.
        _logger.warning(
            "audit: DROPPED row for action %r (actor=%r) — write shed under sustained "
            "SQLite writer contention (bounded retry exhausted). The audit trail is "
            "fail-%s: the request %s.",
            action,
            actor,
            "OPEN" if fail_open_posture else "CLOSED",
            "proceeds unaudited (availability over audit completeness)"
            if fail_open_posture
            else "surfaces an honest retryable 503 (audit completeness over availability)",
        )
        _logger.error(
            "audit: audit write %s under database contention (%d since this process "
            "started). Action %r by %r.%s",
            "DROPPED" if fail_open_posture else "REFUSED",
            _count_dropped(),
            action,
            actor,
            " The request ran unrecorded; set audit_fail_open to 0 on the host to refuse "
            "such requests instead."
            if fail_open_posture
            else "",
        )
        if not fail_open_posture:
            # Fail-CLOSED posture: surface an honest retryable error instead of proceeding
            # unaudited (the middleware renders it as a 503).
            raise db.WriteContentionError(f"audit.log {action}")
    elif anchor_shed:
        _logger.warning(
            "audit: anchor update shed under sustained write contention "
            "(tail-truncation guard trails by one write; self-heals on next audit)"
        )


# --- customer derivation helpers (R2-3, item 3) ------------------------------ #
# The audit SEAM's single source of truth for "which customer does this event
# belong to?". Callers pass a subject id and get back a customer_id (or None =
# system) to hand straight to log(customer_id=...). Deriving here — rather than
# hand-looking-up per route — keeps stamping uniform and NEVER guesses: any lookup
# that can't resolve a customer returns None (system), the SAFE direction (a NULL
# row is hidden from customers, never leaked to the wrong one). All reads are their
# own short session (no settings side effects; safe from any thread).
def customer_for_project(project_id) -> int | None:
    """The customer that owns a project, or None (unowned project / missing id =
    a system-visible event). Accepts an int/digit id; anything else -> None."""
    from app.models import Project

    pid = _as_id(project_id)
    if pid is None:
        return None
    with Session(_engine()) as s:
        p = s.get(Project, pid)
    return getattr(p, "customer_id", None) if p is not None else None


def customer_for_connection(connection_id) -> int | None:
    """The customer a connection is scoped to, or None (global/unassigned
    connection / missing id = a system-visible event)."""
    from app.models import Connection

    cid = _as_id(connection_id)
    if cid is None:
        return None
    with Session(_engine()) as s:
        c = s.get(Connection, cid)
    return getattr(c, "customer_id", None) if c is not None else None


def customer_for_document(document_id) -> int | None:
    """The customer that owns a document (via its project). None when the document
    or its project is missing/unowned."""
    from app.models import Document

    did = _as_id(document_id)
    if did is None:
        return None
    with Session(_engine()) as s:
        d = s.get(Document, did)
    if d is None:
        return None
    return customer_for_project(d.project_id)


class _VerifyFlight:
    """One full verification that concurrent callers share (#608). `pred` is the walk that
    must finish before this one may start; `started` is claimed by exactly one caller."""

    __slots__ = ("done", "error", "pred", "result", "started")

    def __init__(self, pred=None):
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None
        self.pred = pred
        self.started = False


_flight_lock = threading.Lock()
_flight: _VerifyFlight | None = None  # the walk running now
_pending: _VerifyFlight | None = None  # the next walk, not started yet: late arrivals share it


def verify_chain(limit: int | None = None):
    """Walk the log in id order recomputing hashes, in bounded batches (the
    table is unbounded — never materialize it whole).

    Returns (ok, checked, first_bad_id). With `limit`, only the first `limit`
    rows are walked (a sampled check for cheap monitoring); the walk starts
    from the first hashed row onward. With no limit the
    newest row is also checked against the stored head anchor, so deleting the
    tail of the log is detected too.

    SHARED WALKS (#608): a full verification re-walks every row (about 0.25 s per 7,000 rows
    of pure CPU), so N concurrent callers did N full walks and each took N times as long
    (p50 430 ms alone, 3.1 s at 50 clients in the production pressure test). Callers now share
    walks, but ONLY a walk that STARTS AFTER their call: a caller that arrives while a walk is
    running joins the single pending walk that starts when the running one ends (so N
    concurrent callers cost at most two walks, never N). It never takes the answer of a walk
    that began before its call, because that walk may already be past a row the caller has
    just tampered with or written (a tamper followed by a verify must report it, and the
    reset-anchor re-verify must see the reset). Nothing is cached after a walk ends. A limited
    verification (`limit=N`, the sampled check) is never shared. Callers that hold the chain
    lock (rotate_key, reset-anchor) call `_verify_chain_once` directly."""
    global _flight, _pending
    if limit is not None:
        return _verify_chain_once(limit)
    lead = False
    with _flight_lock:
        if _pending is not None:
            run = _pending
        elif _flight is not None:
            run = _pending = _VerifyFlight(pred=_flight)
        else:
            run = _flight = _VerifyFlight()
            run.started = lead = True
    if not lead:
        run.pred.done.wait()
        with _flight_lock:
            if not run.started:
                run.started = lead = True
                if _pending is run:
                    _pending = None
                _flight = run
    if lead:
        try:
            run.result = _verify_chain_once(None)
        except BaseException as exc:  # shared with the joiners, then re-raised for the leader
            run.error = exc
        finally:
            with _flight_lock:
                _flight = None
            run.done.set()
    else:
        run.done.wait()
    if run.error is not None:
        raise run.error
    return run.result


def _verify_chain_once(limit: int | None = None):
    try:
        with ExitStack() as stack:
            with audit_state.process_lock(), _chain_lock:
                stack.enter_context(audit_state.snapshot())
                if _WITNESS_ERROR:
                    return (False, 0, None)
                anchor = _read_anchor()
                external = audit_state.read()
                with Session(_engine()) as s:
                    snapshot_head = s.exec(select(func.max(AuditLog.id))).one() or 0
                legacy_through = audit_state.legacy_through(snapshot_head)
            result = _verify_chain_locked(limit, anchor, external, snapshot_head, legacy_through)
            return result
    except audit_state.ProcessLockError as exc:
        # A busy writer is not evidence of corrupt trust files. Retry verification.
        raise db.WriteContentionError("audit.verify_chain") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _witness_failed(exc)
        return (False, 0, None)


def legacy_restore_hint(first_bad) -> str | None:
    """The delete-.audit_format remediation, ONLY when the break is explained by a validly
    keyed pre-v2 (v1-encoded) row above the pinned legacy boundary. Real tampering or
    truncation (no such row) returns None and must get the normal tamper message (#401)."""
    if first_bad is None:
        return None
    try:
        with audit_state.process_lock(), _chain_lock:
            with Session(_engine()) as s:
                head = s.exec(select(func.max(AuditLog.id))).one() or 0
                r = s.get(AuditLog, first_bad)
            if r is None or not r.row_hash:
                return None
            legacy_through = audit_state.legacy_through(head)
            if r.id <= legacy_through:
                return None
            args = (r.prev_hash, r.ts.isoformat(), r.actor, r.action, r.target, r.detail, r.ip)
            if r.row_hash == _compute_hash(*args, row_id=r.id, customer_id=r.customer_id):
                return None
            if r.row_hash == _v1_hash(*args, row_id=r.id):
                return LEGACY_RESTORE_HINT
    except (audit_state.ProcessLockError, OSError, ValueError, KeyError, TypeError):
        return None
    return None


def _verify_chain_locked(limit, anchor, external, snapshot_head, legacy_through):
    epochs = audit_state.epoch_table()
    if external and limit is None:
        with Session(_engine()) as s:
            witness = s.get(AuditLog, external["id"])
        if (
            external.get("broken")
            or anchor is None
            or witness is None
            or witness.row_hash != external["hash"]
        ):
            return (False, 0, None)
    if anchor == "invalid":
        return (False, 0, None)  # the anchor itself was tampered with
    keyed_from = anchor[2] if anchor else None

    checked = 0
    prev_hash = ""
    started = False  # have we seen the first hashed row yet?
    seen_keyed = False  # once a keyed row verifies, legacy is over
    seen_v2 = False
    last_id = 0  # newest row id encountered (for the tail check)
    remaining = limit
    cursor = 0
    while remaining is None or remaining > 0:
        batch_n = _VERIFY_BATCH if remaining is None else min(_VERIFY_BATCH, remaining)
        with Session(_engine()) as s:
            # Columns only, never ORM instances: the walk reads exactly the fields that are hashed
            # plus the chain links, and building 7,000 AuditLog objects per call was a third of
            # the time (#608).
            rows = s.exec(
                select(
                    AuditLog.id,
                    AuditLog.prev_hash,
                    AuditLog.row_hash,
                    AuditLog.ts,
                    AuditLog.actor,
                    AuditLog.action,
                    AuditLog.target,
                    AuditLog.detail,
                    AuditLog.ip,
                    AuditLog.customer_id,
                )
                .where(AuditLog.id > cursor, AuditLog.id <= snapshot_head)
                .order_by(AuditLog.id)
                .limit(batch_n)
            ).all()
        if not rows:
            break
        for r in rows:
            cursor = last_id = r.id
            if remaining is not None:
                remaining -= 1
            if not started:
                if not r.row_hash and r.id <= legacy_through:
                    continue  # legacy pre-hashing row; skip
                started = True
            if r.prev_hash != prev_hash:
                return (False, checked, r.id)
            args = (prev_hash, r.ts.isoformat(), r.actor, r.action, r.target, r.detail, r.ip)
            historical = r.id <= legacy_through and not seen_v2
            if r.row_hash == _compute_hash(*args, row_id=r.id, customer_id=r.customer_id):
                seen_v2 = True
                seen_keyed = True
            elif historical and r.row_hash == _v1_hash(*args, row_id=r.id):
                seen_keyed = True
            elif (
                historical
                and not seen_keyed
                and (keyed_from is None or r.id < keyed_from)
                and r.row_hash == _legacy_hash(*args)
            ):
                pass  # pre-upgrade row, unkeyed hash accepted
            else:
                return (False, checked, r.id)
            for after, material in epochs:
                if r.id == after:
                    key_id = hashlib.sha256(material).hexdigest()
                    proof = _hmac.new(
                        material, (r.prev_hash + key_id).encode(), hashlib.sha256
                    ).hexdigest()
                    expected = f"key_id={key_id} predecessor={r.prev_hash} proof={proof}"
                    if r.action != "audit.key_rotated" or r.detail != expected:
                        return (False, checked, r.id)
            checked += 1
            prev_hash = r.row_hash
            if remaining is not None and remaining == 0:
                break
    if limit is None and anchor is not None and last_id < anchor[0]:
        return (False, checked, None)  # tail rows were deleted
    # F8: the anchor is written on EVERY log() (alongside the row), so a chain that contains a
    # KEYED row but has NO anchor means the anchor Setting row was DELETED — the exact move a
    # tail-truncation attacker makes to dodge the check above. Missing anchor + keyed rows =
    # tampered, not a fresh install. (A pre-keying legacy-only chain has no keyed row and
    # predates the anchor, so seen_keyed stays False and it still verifies.)
    # A genuinely first, shed anchor has no witness yet. Once anchored, even a single
    # survivor is rejected by the external witness check above (#278).
    if limit is None and anchor is None and seen_keyed and (external is not None or checked > 1):
        return (False, checked, None)
    if limit is None and last_id and external is None and anchor is not None:
        with audit_state.process_lock(), _chain_lock:
            if audit_state.read() is None:
                _witness_initialized()
                audit_state.advance(last_id, prev_hash, prior=None)
    return (True, checked, None)


# --- scheduled-verification monitoring (gap G-10 / AU-05) -------------------- #
_DEFAULT_VERIFY_HOURS = 24
_VERIFY_HOURS_WARNED = False  # once-per-process latch: garbage cadence value (see verify_hours)


def _warn_verify_hours(raw) -> None:
    global _VERIFY_HOURS_WARNED
    if not _VERIFY_HOURS_WARNED:
        _VERIFY_HOURS_WARNED = True
        _logger.warning(
            "audit: audit_chain_verify_hours=%r is not an integer — falling back to the default "
            "%dh cadence (NOT disabling monitoring). Only a literal non-positive integer (e.g. "
            "'0') turns scheduled chain verification off. (Logged once per process.)",
            raw,
            _DEFAULT_VERIFY_HOURS,
        )


def verify_hours() -> int:
    """Scheduled audit-chain verification cadence in HOURS (`audit_chain_verify_hours`,
    default 24). ONLY a literal non-positive integer (``0``, ``-1``) disables the scheduled
    verification; an UNPARSEABLE value ('24h', '1.5', 'abc') falls back to the DEFAULT (24h)
    with a once-per-process WARNING — a typo must never silently switch monitoring OFF (F5).
    Read through the settings cache like every other cadence knob."""
    raw = settings.get("audit_chain_verify_hours", str(_DEFAULT_VERIFY_HOURS))
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        _warn_verify_hours(raw)
        return _DEFAULT_VERIFY_HOURS  # garbage -> default, NOT off
    return n if n > 0 else 0  # only a literal non-positive integer disables


def _read_chain_status():
    """The persisted scheduled-verify snapshot/latch: a parsed dict, None (no scheduled
    verification has run yet), or the string "invalid" (present but malformed / bad or
    MISSING MAC — i.e. a DB-only attacker forged/froze it). MAC-verified exactly like
    _read_anchor(): without the keyfile the row cannot be rewritten to a value that
    verifies, so an attacker cannot forge {"ok":1,"last_checked":"2099-…"} to freeze the
    sweep + fake a green readout. Read DIRECTLY (not via settings.get) — no settings-cache
    side effect."""
    with Session(_engine()) as s:
        row = s.get(Setting, _CHAIN_STATUS_KEY)
    raw = row.value if row else ""
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except (ValueError, TypeError):
        return "invalid"
    if not isinstance(outer, dict):
        return "invalid"
    body, mac = outer.get("v"), outer.get("mac")
    if not isinstance(body, str) or not isinstance(mac, str):
        return "invalid"  # missing MAC (e.g. a bare forged {"ok":1,...}) is NOT trusted
    try:
        if not _hmac.compare_digest(mac, _chain_status_mac(body)):
            return "invalid"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _witness_failed(exc)
        return "invalid"
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "invalid"
    return data if isinstance(data, dict) else "invalid"


def _write_chain_status(data: dict) -> None:
    """Persist the scheduled-verify snapshot/latch inside a MAC envelope
    ({"v": <body>, "mac": <hmac(body)>}). Written directly (like _write_anchor) — a single
    Setting upsert, no settings-cache write / apply_to_config()."""
    body = json.dumps(data, separators=(",", ":"), sort_keys=True)
    try:
        value = json.dumps(
            {"v": body, "mac": _chain_status_mac(body)}, separators=(",", ":"), sort_keys=True
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _witness_failed(exc)
        return
    with Session(_engine()) as s:
        row = s.get(Setting, _CHAIN_STATUS_KEY)
        if row:
            row.value = value
        else:
            row = Setting(key=_CHAIN_STATUS_KEY, value=value)
        s.add(row)
        s.commit()


def chain_status_snapshot() -> dict | None:
    """Read-only view of the last SCHEDULED audit-chain verification — the source both the
    Prometheus gauges (observability._AuditChainGaugeCollector) and the admin /audit
    one-line readout read. Keys: state ("ok"/"broken"), ok (bool), first_bad_id (int|None),
    rows_checked (int), last_checked (iso), last_alert (iso|None). None until the first
    scheduled verification runs — AND None on a tamper (bad/missing MAC), so a forged status
    row can NEVER make the gauges/page report OK (they report UNKNOWN = no data). The sweep
    itself uses the tri-state _read_chain_status directly to react to the tamper."""
    if _WITNESS_ERROR:
        return {
            "state": "broken",
            "ok": False,
            "break_class": "trust",
            "first_bad_id": None,
            "rows_checked": 0,
            "note": _WITNESS_NOTE,
        }
    try:
        st = _read_chain_status()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _witness_failed(exc)
        return chain_status_snapshot()
    if _WITNESS_ERROR:
        return chain_status_snapshot()
    if _WITNESS_NOTE:
        return {**(st if isinstance(st, dict) else {}), "note": _WITNESS_NOTE}
    return st if isinstance(st, dict) else None


def chain_monitor_view(now=None) -> dict:
    """The admin /audit readout's view of the scheduled-verify state (F7). Returns
    ``{disabled, hours, snapshot, stale}``:
      * ``disabled`` — the cadence is 0 (scheduled checking is OFF).
      * ``snapshot`` — chain_status_snapshot(): None when it has never run OR the status row
        was tampered (bad MAC), so the page renders UNKNOWN, never a false OK.
      * ``stale`` — a valid snapshot whose ``last_checked`` is missing/garbage, in the FUTURE,
        or older than 2x the cadence: the scheduled check has silently stopped (or been
        frozen) and the "OK" it shows can no longer be trusted, so the page flags a warning.
    Centralized here (not in the template) so the time math is testable and single-sourced."""
    hours = verify_hours()
    snap = chain_status_snapshot()
    stale = False
    if snap and hours > 0:
        last = _parse_iso(snap.get("last_checked"))
        now = now or datetime.utcnow()
        stale = last is None or last > now or (now - last) > timedelta(hours=2 * hours)
    return {
        "disabled": hours == 0,
        "hours": hours,
        "snapshot": snap if snap and "state" in snap else None,
        "stale": stale,
        "note": _WITNESS_NOTE,
    }


def _anchor_break_open() -> bool:
    """R6: whether an ANCHOR-class break is currently OPEN — RE-DERIVED FROM THE AUDIT LOG
    ITSELF, never from the (deletable) audit_chain_status Setting row. True iff the newest
    forced ``audit.chain_broken`` row carrying ``class=anchor`` is NEWER (higher id) than the
    newest ``audit.anchor_reset`` row.

    Both row types are FORCED writes INSIDE the MAC'd chain, so an attacker cannot remove them
    without either breaking a prev_hash mid-chain or truncating the tail — both already caught
    by verify_chain. This is what makes the sticky truncation posture survive deletion of the
    status Setting row (the R6 probe: truncate -> refill -> delete status row -> a naive check
    would report ok again).

    COST: ``auditlog.action`` is deliberately NOT indexed — an index would tax EVERY audit
    write on the mutation hot path to speed a once-per-cadence read. So this is O(n) in the log
    size, but it runs inside the SAME (daily) sweep as verify_chain, which ALREADY walks the
    whole log O(n) — no new complexity class. chain_broken/anchor_reset are rare, latched
    actions, so the result sets are tiny; the ``id > reset_id`` bound uses the PK index."""
    with Session(_engine()) as s:
        reset_id = (
            s.exec(
                select(func.max(AuditLog.id)).where(AuditLog.action == _ANCHOR_RESET_ACTION)
            ).one()
            or 0
        )
        rows = s.exec(
            select(AuditLog.id, AuditLog.detail)
            .where(AuditLog.action == _CHAIN_BROKEN_ACTION, AuditLog.id > reset_id)
            .order_by(desc(AuditLog.id))
        ).all()
    return any(_parse_break_class(detail) == "anchor" for _id, detail in rows)


def run_chain_verification(*, record: bool, actor: str = "scheduler", now=None):
    """THE single service function behind BOTH the scheduled sweep and the CLI `audit
    verify` leaf (they must never diverge). Runs verify_chain() and returns its
    (ok, checked, first_bad_id) tuple.

    record=False (the CLI / manual spot check): no audit row or status write; the first
    successful check initializes the filesystem witness. record=True records the result on the audit
    trail and persists the monitoring status + re-alert latch:

      * SUCCESS -> a NON-forced ``audit.chain_verified`` heartbeat row (detail: checked
        count; ``recovered=1`` on a broken->ok transition). NOT forced ON PURPOSE: the
        sweep only runs while ``audit_enabled=1``, so a normal row always persists, and
        ``force`` is reserved for the operator/impersonation verbs that must survive a
        deliberately-OFF global gate. A missed heartbeat is fail-SAFE — its very absence is
        the signal — so it needs no gate-bypass. (The success row extends the chain it just
        verified; that is fine — the next verify simply includes it as one more valid link.)
      * FAILURE -> a FORCED ``audit.chain_broken`` row (detail: first_bad_id + checked) plus
        an ERROR-level log. FORCED ON PURPOSE: a break must be recorded even if the global
        gate is flipped OFF between the sweep's gate check and this write (a race). A missed
        break is fail-DANGEROUS, so it must never be maskable by the enabled() short-circuit.
        LATCHED: while the chain stays broken at the SAME first_bad_id the forced row is
        re-emitted only on a state change or at most once per ``_CHAIN_REALERT_HOURS`` — a
        persistent break never spams a row on every scheduled run.
    """
    ok, checked, first_bad = verify_chain()
    if not record:
        return (ok, checked, first_bad)

    now = now or datetime.utcnow()
    prior = _read_chain_status()
    if not isinstance(prior, dict):
        prior = None  # absent OR tampered ("invalid") -> treat as no prior; we overwrite it

    # R1: the BREAK CLASS separates ANCHOR-class breaks (truncation / invalid anchor /
    # missing-anchor-with-rows — verify_chain returns first_bad_id=None) from MID-CHAIN tampers
    # (an int row id). It is derived HERE and stored EXPLICITLY in the status envelope (never
    # re-inferred later).
    break_class = None if ok else ("anchor" if first_bad is None else "tamper")
    if not ok and _WITNESS_ERROR:
        break_class = "trust"  # A process latch clears on a healthy restart.

    # R1/R6 (STICKY ANCHOR BREAK): a prior ANCHOR-class break must NOT silently self-recover. A
    # truncation leaves verify_chain broken via the tail/anchor check, but each daily forced
    # chain_broken row consumes one truncated id, so after enough days head==anchor and a naive
    # verify would pass again — a FALSE recovered=1 that hides real row loss. So while an
    # anchor-class break is OPEN, a now-passing verify is REFUSED here: state stays broken (the
    # 24h latch still re-alerts) until a human runs `audit reset-anchor`, the SINGLE recovery
    # path. R6: "open" is RE-DERIVED FROM THE LOG (_anchor_break_open — newest chain_broken
    # class=anchor newer than newest anchor_reset), NOT from the deletable status cache, so
    # deleting the audit_chain_status row cannot clear the stick. Mid-chain tampers
    # (break_class=="tamper") keep normal recovery — a healed chain lost no rows, so it may flip
    # back to ok.
    if ok and _anchor_break_open():
        ok = False
        first_bad = None
        break_class = "anchor"
        _logger.warning(
            "audit: verify now passes but an ANCHOR-class break is still OPEN in the log "
            "(truncation/anchor) — REFUSING the auto-recovery. Truncation stays RED until "
            "`audit reset-anchor` is run after investigation (deploy/aws-hosted/LAUNCH.md)."
        )

    new_state = "ok" if ok else "broken"
    last_alert = None

    if ok:
        recovered = bool(prior and prior.get("state") == "broken")
        detail = f"checked={checked}" + (" recovered=1" if recovered else "")
        # NON-forced heartbeat (see docstring). Doubles as the recovery notice.
        log(actor=actor, action="audit.chain_verified", detail=detail)
        if recovered:
            _logger.info(
                "audit: chain RECOVERED — scheduled verification passed (%d rows).", checked
            )
    else:
        prior_state = prior.get("state") if prior else None
        prior_bad = prior.get("first_bad_id") if prior else None
        prior_alert = _parse_iso(prior.get("last_alert")) if prior else None
        state_changed = (
            prior_state != "broken"
            or prior_bad != first_bad
            or (prior and prior.get("break_class") != break_class)
        )
        stale = prior_alert is None or (now - prior_alert) >= timedelta(hours=_CHAIN_REALERT_HOURS)
        if state_changed or stale:
            # FORCED — the break must be recorded even if audit_enabled flipped off mid-sweep.
            log(
                actor=actor,
                action=_CHAIN_BROKEN_ACTION,
                detail=_format_chain_broken_detail(first_bad, checked, break_class),
                force=True,
            )
            _logger.error(
                "AUDIT CHAIN BROKEN (%s) — scheduled verification FAILED after %d row(s) (first "
                "bad row id: %s). The tamper-evident audit log integrity is compromised; follow "
                "docs/BACKUP.md and deploy/aws-hosted/LAUNCH.md.%s",
                break_class,
                checked,
                first_bad,
                (" " + hint) if (hint := legacy_restore_hint(first_bad)) else "",
            )
            last_alert = now.isoformat(timespec="seconds")
        else:
            # Latched: same break, still inside the re-alert window — keep the prior alert
            # stamp and suppress a duplicate row, but stay VISIBLE at WARNING.
            last_alert = prior.get("last_alert") if prior else None
            _logger.warning(
                "audit: chain still BROKEN (%s) at first_bad_id=%s (checked=%d) — chain_broken "
                "row suppressed by the re-alert latch (state unchanged, <%dh since last alert).",
                break_class,
                first_bad,
                checked,
                _CHAIN_REALERT_HOURS,
            )

    _write_chain_status(
        {
            "state": new_state,
            "ok": bool(ok),
            "break_class": break_class,
            "first_bad_id": first_bad,
            "rows_checked": int(checked),
            "last_checked": now.isoformat(timespec="seconds"),
            "last_alert": last_alert,
        }
    )
    return (ok, checked, first_bad)


def verify_chain_scheduled_sweep(now=None) -> bool:
    """Defer lock contention without recording a verification result or a crash."""
    from app.db import WriteContentionError

    try:
        return _verify_chain_scheduled_sweep(now)
    except WriteContentionError:
        _logger.warning("Audit chain verification deferred: lock busy; retry next tick")
        return False


def _verify_chain_scheduled_sweep(now=None) -> bool:
    """Piggybacked scheduler sweep (called from scheduler.tick): run a scheduled audit-chain
    verification when it is DUE. SELF-THROTTLING — the scheduler ticks every ~30s but this
    runs at most once per ``audit_chain_verify_hours`` (default 24; only 0 disables). Returns
    True IFF a verification ran this call. Best-effort: the caller wraps it so a failure never
    aborts the tick.

    NOT gated on ``audit_enabled`` (gate item-3): verify_chain is a pure READ and the
    chain_broken row is ``force=True``, so monitoring must keep watching even when the global
    audit gate is off — a tamper while auditing is disabled is exactly when a forced break row
    matters most. (The success heartbeat row stays gated/non-forced — it simply does not
    persist while audit is off, but the status + gauges still update.) The cadence gate is
    cache-backed and checked BEFORE the status-row DB read, so a monitoring-off install
    (hours=0) does zero DB work per tick.

    Reacts to three "run now" conditions beyond a normal elapsed cadence:
      * a TAMPERED status row (bad/missing MAC, F1) — a DB-only attacker tried to freeze the
        sweep / fake a green readout: record a forced ``audit.chain_status_tampered`` row and
        re-verify NOW (which self-heals the status row with a fresh MAC).
      * a FUTURE ``last_checked`` (forged far-future stamp, or a backward wall-clock/host
        clock jump, F1) — treated as due, never trusted to defer the next run.
      * never run before (no status row)."""
    from app.db import WriteContentionError

    global _CHAIN_TAMPER_HANDLED_AT, _CHAIN_TAMPER_REVERIFY_PENDING
    hours = verify_hours()
    if hours <= 0:
        return False
    now = now or datetime.utcnow()
    status = _read_chain_status()
    if status == "invalid":
        # R3: a persistently re-forged status row would otherwise re-run the full O(n) verify +
        # write a chain_status_tampered row on EVERY 30s tick. Latch it to at most once per
        # _CHAIN_REALERT_HOURS per process (the latch can't live in the forgeable status row).
        latched = _CHAIN_TAMPER_HANDLED_AT is not None and (
            now - _CHAIN_TAMPER_HANDLED_AT
        ) < timedelta(hours=_CHAIN_REALERT_HOURS)
        if latched and not _CHAIN_TAMPER_REVERIFY_PENDING:
            _logger.warning(
                "audit: audit_chain_status still failing its MAC — tamper handling suppressed "
                "by the re-alert latch (<%dh since last handled).",
                _CHAIN_REALERT_HOURS,
            )
            return False
        # F1: the status row failed its MAC — forged/tampered. Record it loudly and re-verify
        # NOW; run_chain_verification rewrites a fresh MAC'd status, self-healing the row.
        if not latched:
            log(
                actor="scheduler",
                action="audit.chain_status_tampered",
                detail="audit_chain_status failed its MAC — re-verifying now",
                force=True,
            )
            _logger.error(
                "AUDIT CHAIN STATUS TAMPERED — the audit_chain_status monitoring row failed its "
                "MAC (a DB-only forge/freeze attempt); re-running verification now. Follow "
                "docs/BACKUP.md and deploy/aws-hosted/LAUNCH.md."
            )
            _CHAIN_TAMPER_HANDLED_AT = now
        try:
            run_chain_verification(record=True, actor="scheduler", now=now)
        except WriteContentionError:
            _CHAIN_TAMPER_REVERIFY_PENDING = True  # retry next tick, no second tampered row
            raise
        _CHAIN_TAMPER_REVERIFY_PENDING = False
        return True
    last = _parse_iso(status.get("last_checked")) if status else None
    # Due unless we have a valid, NON-FUTURE last_checked whose cadence has not elapsed. A
    # future stamp (last > now) fails the `last <= now` clause and therefore runs (F1 clamp).
    if last is not None and last <= now and (now - last) < timedelta(hours=hours):
        return False  # not due yet
    _alert_if_logging_off()
    run_chain_verification(record=True, actor="scheduler", now=now)
    return True


def _alert_if_logging_off() -> None:
    """#220: an intact chain says nothing about rows that were never written. When audit
    logging is OFF, every scheduled verification logs it at ERROR, so the daily check no
    longer reports a clean bill while the trail records nothing. (`audit verify` prints the
    same fact as an "AUDIT OFF:" line, which the hosted alert timer emails.)"""
    try:
        if not enabled():
            _logger.error(
                "audit: audit logging is OFF (audit_enabled=0): the chain verifies, but "
                "mutating actions are not being recorded. Turn it on with "
                "`config set audit_enabled 1`."
            )
    except Exception:
        _logger.debug("audit: could not read audit_enabled for the off alert", exc_info=True)


def rotate_key(actor: str = "cli") -> str:
    """Host maintenance operation; stop other writer processes before calling.

    The old key signs the rotation row; the new key countersigns its predecessor
    and key id in the detail. Retired keys and boundaries remain outside the DB.
    """
    with audit_state.process_lock(), _chain_lock:
        # A private fresh walk: this thread holds the chain lock, so it must not wait on a
        # shared walk that needs that lock (#608).
        if not _verify_chain_once(None)[0]:
            raise ValueError("Repair the audit chain before rotating its key")
        material, key_id = audit_state.new_epoch()
        with Session(_engine()) as s:
            previous = s.exec(select(AuditLog).order_by(desc(AuditLog.id)).limit(1)).first()
        predecessor = previous.row_hash if previous else ""
        proof = _hmac.new(material, (predecessor + key_id).encode(), hashlib.sha256).hexdigest()
        log(
            actor=actor,
            action="audit.key_rotated",
            detail=f"key_id={key_id} predecessor={predecessor} proof={proof}",
            force=True,
        )
        with Session(_engine()) as s:
            row = s.exec(select(AuditLog).order_by(desc(AuditLog.id)).limit(1)).first()
        if row is None or row.action != "audit.key_rotated" or key_id not in row.detail:
            raise ValueError("Audit rotation record could not be written; key unchanged")
        status = chain_status_snapshot()
        audit_state.save_epoch(row.id, material)
        if not _write_anchor(row.id, row.row_hash):
            raise ValueError("Key rotated but anchor write was deferred; run audit verify")
        # Pin the new epoch count even though rotation keeps the same head id.
        audit_state.advance(row.id, row.row_hash, allow_regress=True)
        if status is not None:
            _write_chain_status(status)
        return key_id


def reset_anchor(actor: str = "") -> tuple[int | None, int | None]:
    """Deliberately RE-BASELINE the tail-truncation anchor to the current real head — the
    escape hatch for a LEGITIMATE `db restore` to an OLDER backup, where the monotonic
    _write_anchor guard (F2) would otherwise keep the anchor pinned at the pre-restore
    (higher) head and make verify_chain report a false truncation forever. HOST-ONLY,
    double-confirmed at the CLI (`audit reset-anchor`).

    Writes a FORCED ``audit.anchor_reset`` row (detail: old_head -> new_head) so the
    re-baseline is itself on the tamper-evident record, then force-writes the anchor at the
    restored data head (allow_regress=True). ``new_head`` is the true current head of the
    restored DB (captured BEFORE the self-recording anchor_reset row is appended), so the
    detail, the return value, and the stored anchor all name the SAME id.

    R1/R6: this is the SINGLE recovery path from a STICKY anchor-class break. The forced
    ``audit.anchor_reset`` row it writes IS the clear signal — _anchor_break_open() re-derives
    "open" from the log by comparing that row's id against the newest chain_broken class=anchor,
    so once this row lands (higher id) the break reads as closed. It ALSO rewrites the
    audit_chain_status CACHE (a fresh verify result, last_alert cleared) for the gauges/page. On
    an EMPTY log (R5) it is a no-op: returns (old_head, None) and writes NOTHING (the CLI reports
    "nothing to re-baseline"). NOTE: this trusts the operator — it is exactly the "erase
    truncation evidence" move an attacker wants, which is why it is host-only, double-confirmed,
    and self-audited."""
    with audit_state.process_lock(), _chain_lock, audit_state.snapshot():
        return _reset_anchor_locked(actor)


def _reset_anchor_locked(actor):
    global _WITNESS_ERROR, _WITNESS_NOTE
    witness_replaced = _WITNESS_ERROR
    try:
        audit_state.read()
    except (OSError, ValueError, KeyError, TypeError):
        witness_replaced = True
    prior = _read_anchor()
    old_head = prior[0] if isinstance(prior, tuple) else None
    with Session(_engine()) as s:
        pre = s.exec(select(AuditLog).order_by(desc(AuditLog.id)).limit(1)).first()
    if pre is None:
        return (old_head, None)  # empty log: nothing to re-baseline onto (R5 — no row written)
    new_head, new_hash = pre.id, pre.row_hash
    epochs = audit_state.epochs()
    if epochs and new_head < epochs[-1]["after"]:
        raise ValueError("Restore a backup containing the latest audit key rotation first")
    # Forced audit row FIRST (self-audits the reset even if the global gate is off). It becomes
    # the new newest row; log()'s own _write_anchor is monotonic and will NOT move the anchor
    # backward off the stale high head — the explicit re-baseline below does that.
    log(
        actor=actor or "system",
        action=_ANCHOR_RESET_ACTION,
        detail=f"old_head={old_head} new_head={new_head} witness_replaced={int(witness_replaced)}",
        force=True,
    )
    with Session(_engine()) as s:
        reset_row = s.exec(select(AuditLog).order_by(desc(AuditLog.id)).limit(1)).first()
    if reset_row is None or reset_row.id <= new_head or reset_row.action != _ANCHOR_RESET_ACTION:
        raise ValueError("Audit reset record could not be written; witnesses unchanged")
    # Re-baseline the anchor to the restored data head. The anchor_reset row itself sits one
    # beyond it and is picked up by the next normal write (monotonic); verify_chain passes
    # (last_id >= anchor head).
    with _chain_lock:
        if not _write_anchor(new_head, new_hash, allow_regress=True):
            raise ValueError("Anchor reset write deferred; retry reset-anchor")
        audit_state.advance(reset_row.id, reset_row.row_hash, allow_regress=True)
        if witness_replaced:
            _logger.warning(
                "Audit reset-anchor replaced an unreadable witness; replacement audited."
            )
        _WITNESS_ERROR = False
        _WITNESS_NOTE = ""
    # R1: CLEAR the sticky/latched status so the anchor-class break can finally flip back to ok.
    # Re-verify from the re-baselined anchor and write a FRESH status with the latch reset.
    ok2, checked2, fb2 = _verify_chain_once(None)  # fresh: must see the reset (#608)
    _write_chain_status(
        {
            "state": "ok" if ok2 else "broken",
            "ok": bool(ok2),
            "break_class": None if ok2 else ("anchor" if fb2 is None else "tamper"),
            "first_bad_id": fb2,
            "rows_checked": int(checked2),
            "last_checked": datetime.utcnow().isoformat(timespec="seconds"),
            "last_alert": None,
        }
    )
    return (old_head, new_head)


def _actor_org_id(actor) -> int | None:
    """The org the actor's account belongs to RIGHT NOW (called at write time, so it is the
    org the row is written in), or None for a system / unauthenticated / unknown actor or
    any read failure. Never raises: stamping is a view column, not a reason to lose a row."""
    who = (actor or "").strip().lower()
    if not who or who == "system":
        return None
    try:
        with Session(_engine()) as s:
            return s.exec(select(User.org_id).where(func.lower(User.email) == who)).first()
    except Exception:
        _logger.debug("audit: actor org lookup failed", exc_info=True)
        return None


def _filtered(q, actor=None, action=None, org_id=None, customer_id=None):
    if actor:
        q = q.where(func.lower(AuditLog.actor) == actor.lower())
    if action:
        q = q.where(AuditLog.action.contains(action))
    # Multi-org P8 / #487: when org_id is set (customer /audit + GDPR export + workspace
    # export), confine the rows to those WRITTEN in that org: a hard equality on the
    # org_id stamped at write time, NOT the actor's current membership, so a user moved
    # between orgs never takes the former org's history along. NULL-org rows (system,
    # unauthenticated) never match. The audit service itself is gate-agnostic; the
    # CUSTOMER surface passes the admin's org, the unscoped operator view passes None.
    if org_id is not None:
        q = q.where(AuditLog.org_id == org_id)
    # R2-3 (item 3): a CUSTOMER-BOUND user's /audit view is confined to rows stamped
    # with EXACTLY their customer_id. This is a hard column equality (NOT actor-set
    # membership like org above): system/NULL rows are EXCLUDED and another
    # customer's rows can never appear, so there is "absolutely no bleed over". The
    # route passes this ONLY for a bound user; internal staff pass None (unscoped by
    # customer — the existing admin/role gate governs their view). Combined with any
    # other filter as an intersection.
    if customer_id is not None:
        q = q.where(AuditLog.customer_id == customer_id)
    return q


def recent(
    limit: int = 250,
    offset: int = 0,
    actor: str | None = None,
    action: str | None = None,
    org_id: int | None = None,
    customer_id: int | None = None,
):
    with Session(_engine()) as s:
        q = _filtered(
            select(AuditLog), actor=actor, action=action, org_id=org_id, customer_id=customer_id
        )
        return s.exec(
            q.order_by(desc(AuditLog.ts), desc(AuditLog.id)).offset(offset).limit(limit)
        ).all()


def count(
    actor: str | None = None,
    action: str | None = None,
    org_id: int | None = None,
    customer_id: int | None = None,
) -> int:
    with Session(_engine()) as s:
        q = _filtered(
            select(func.count()).select_from(AuditLog),
            actor=actor,
            action=action,
            org_id=org_id,
            customer_id=customer_id,
        )
        return s.exec(q).one()


def iter_export(
    actor: str | None = None,
    action: str | None = None,
    since=None,
    batch: int = 500,
    org_id: int | None = None,
    customer_id: int | None = None,
):
    """Yield rows as SIEM-ready dicts, oldest first, in bounded batches
    (the table is unbounded — never materialize it whole).

    `since` is a naive-UTC datetime lower bound (inclusive) for incremental
    pulls. prev_hash/row_hash let a downstream store check linkage only. Integrity
    verification requires the installation's HMAC key and canonical row data;
    the export alone is not independently verifiable. Never share the audit key
    with a SIEM; use the installation's audit verification result.

    `org_id` (multi-org P8) confines the stream to actors in that org — used by the
    customer /audit export + GDPR export so an org-admin never sees another org's
    rows. None (the single-tenant path, and the host operator CLI) is unscoped.
    The back-office was the other unscoped caller until it was removed 2026-09-20.

    `customer_id` (R2-3) confines the stream to rows stamped with that customer. Callers:
    the CLI `admin audit export --customer <id>` filter (matching the web /audit
    customer-scoped view), GET /api/audit/export, and (#586, defense in depth) the web
    /audit/export, which passes a customer-bound admin's own customer.
    """
    cursor = 0
    while True:
        with Session(_engine()) as s:
            q = _filtered(
                select(AuditLog).where(AuditLog.id > cursor),
                actor=actor,
                action=action,
                org_id=org_id,
                customer_id=customer_id,
            )
            if since is not None:
                q = q.where(AuditLog.ts >= since)
            rows = s.exec(q.order_by(AuditLog.id).limit(batch)).all()
        if not rows:
            return
        for r in rows:
            cursor = r.id
            ts = r.ts.isoformat()
            if r.ts.tzinfo is None:
                ts += "Z"  # stored naive-UTC; be explicit for SIEM parsers
            yield {
                "id": r.id,
                "ts": ts,
                "actor": r.actor,
                "action": r.action,
                "target": r.target,
                "detail": r.detail,
                "ip": r.ip,
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
            }
