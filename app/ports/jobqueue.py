"""JobQueue port -- the named background-job DISPATCH seam (sprint Train S, STRETCH).

This module NAMES the seam Localization OS uses to hand a submitted ``Job`` to a worker thread
and to recover work across a restart. Before this train that seam was un-named: it was the
bare ``queue.Queue`` + ``recover_interrupted`` body inside ``app.services.jobs``. This port
introduces the MINIMAL interface those operations already had -- ``enqueue`` / ``claim`` /
``release`` / ``recover`` -- adapts the existing in-memory implementation to it as the
DEFAULT in-process adapter, and adds a DURABLE, DB-backed adapter (the install default since
S2, ``job_queue_backend="db"``).

THE SEAM, AND WHAT SITS ABOVE IT
--------------------------------
``app.services.jobs`` is ABOVE the seam and is UNCHANGED in shape: ``submit`` still writes a
``Job`` row then enqueues; ``_worker_loop`` still claims a job id, runs ``_execute(jid)`` (the
one authority on the Job's RUNNING/COMPLETED/FAILED transition, notifications, budget eval),
then releases; ``recover_interrupted`` still runs at boot. The ONLY change is that those four
verbs route through ``jobqueue.get()`` instead of touching a module-level ``queue.Queue``
directly. Every handler, route, report and CLI verb above ``jobs`` is untouched.

WHY ``release`` INSTEAD OF SEPARATE ``complete`` / ``fail``
----------------------------------------------------------
``jobs._execute`` SWALLOWS a handler failure (it records FAILED on the ``Job`` row and does
NOT re-raise), so the worker loop never learns success-vs-failure as an exception. The
authority on a job's terminal state is therefore the ``Job.status`` column, not a signal the
queue could observe. So the port exposes a single ``release(job_id)`` the worker calls in its
``finally``; the DB adapter DERIVES the queue entry's terminal state (done/failed) from
``Job.status``. A pair of ``complete``/``fail`` methods would force the worker loop to
re-derive that same fact and risk mis-pairing it -- ``release`` keeps the loop a plain
try/finally that is byte-identical for the in-memory adapter (``release`` == ``task_done``).

WORKER POOLS (S2)
-----------------
Jobs run on two pools, short and long (``POOLS``, short first). ``claim(worker_id, timeout,
pools)`` takes the pools a worker serves in priority order: a short worker passes
``("short",)``, a long worker ``("long", "short")`` so it still drains the short backlog when
no long job is waiting. ``enqueue(job_id, pool)`` names the pool for the in-memory adapter;
the DB adapter derives it from the job's kind against ``jobs.LONG_KINDS`` at claim time (a
join on ``job.kind``, no extra column). ``pools=None`` keeps the old take-anything order.

THE ATOMIC CLAIM (works on BOTH sqlite and postgres)
----------------------------------------------------
``DbJobQueue.claim`` selects the lowest pending row, then flips it with
``UPDATE jobqueue SET state='running', ... WHERE id=:qid AND state='pending'`` and checks
``rowcount``. That conditional UPDATE is a compare-and-swap: if two workers pick the SAME
candidate, exactly one UPDATE matches ``state='pending'`` (rowcount 1 -> it won) and the
other matches zero rows (rowcount 0 -> it lost and retries). SQLite serializes writers (WAL:
one writer at a time) so the loser sees ``state='running'`` by the time its UPDATE runs;
Postgres row-locks it identically. The guard is the ``state='pending'`` predicate, portable
to both. On Postgres the candidate pick additionally takes ``FOR UPDATE OF q SKIP LOCKED``
so concurrent claimers skip a row another node is mid-claiming instead of queuing on it.
The ``attempts`` counter is bumped on each
successful claim (the HA retry count).

RESTART RECOVERY / THE HA LEASE
-------------------------------
Each claim stamps ``worker`` + ``lease_expires_at`` (a generous ``_LEASE_SECONDS`` window).
``recover`` resets interrupted ``running`` rows to ``pending`` + their ``Job`` row (RUNNING ->
QUEUED, ``started_at`` cleared) so ``_execute`` re-runs them -- the durable-queue win over the
in-memory adapter, which fails such jobs. It takes an ``at_boot`` flag that decides whether the
lease is consulted:

  * ``at_boot=True`` (server ``_startup`` ONLY): reclaim own-host dead PIDs,
    this PID, legacy/unparsable workers, and NULL/expired leases. Other-host or
    own-host live-peer fresh leases remain running, ERROR-logged and latched for
    /health. UUIDs only disambiguate logs. PID reuse can preserve a dead worker's
    fresh lease until expiry and the next boot or manual ``db recover-jobs``.
  * ``at_boot=False`` (DEFAULT; the ``db recover-jobs`` CLI sweep, documented to run WITHOUT
    bouncing a LIVE server): reclaim ONLY EXPIRED/NULL-lease rows. A job a live worker is still
    running carries a fresh FUTURE lease and MUST NOT be reclaimed, or the server's workers
    re-claim + re-run it (round-2 point-2 DOUBLE-RUN). This is the "reaper that runs WHILE
    workers are live" the lease exists for.

Because ``_execute`` re-checks ``Job.status == QUEUED`` before running, a job that actually
COMPLETED before the crash is never re-run even if its queue row is re-dispatched: the guard is
load-bearing here, not redundant.

LEASE HEARTBEAT (S1): the lease is short (``_LEASE_SECONDS``, 5 minutes) and a process-wide
renewer thread extends the lease of every job THIS process's workers hold, every
``min(LEASE_RENEW_SECONDS, lease/3)`` seconds, via ``renew``. So a live long job always carries
a fresh future lease and the live sweep (``recover(at_boot=False)``, lease-only) never reclaims
it; a dead process stops renewing and its rows expire within one lease. A claim registers the
job with the renewer; ``release`` / ``requeue`` / ``hand_back`` unregister it.

RETRY AND DRAIN (S1): ``requeue(job_id, delay, error)`` puts a claimed job back to ``pending``
with ``not_before`` = now + delay (transient-retry backoff; ``claim`` skips rows whose
``not_before`` is in the future). ``hand_back(job_ids)`` returns this process's still-running
rows to ``pending`` at graceful shutdown (``jobs.drain``).
ONE app process per database is supported; the fence detects evidence of another process
at boot and surfaces it on /health, but does not prevent a second process from starting.

DB IS THE DEFAULT BACKEND (S2; the s3_blobstore fail-loud shape)
--------------------------------------------------------------
The port-level default adapter (self-registered at import, used by processes that never call
``configure_at_boot``) is ``InMemoryJobQueue``. ``configure_at_boot`` reads
``job_queue_backend``, which defaults to ``"db"``; ``"memory"`` is the opt-out and the
FIRST-statement no-op -- the DB adapter is never built and the ``jobqueue`` table is never
touched. ``"db"`` registers ``DbJobQueue`` as the override, FAILING THE BOOT
(``JobQueueError``) if the ``jobqueue`` table is absent (a half-migrated install must never
boot into a state where it silently drops durable work). Any other value fails the boot
loudly.

NO ``ports.get()`` AT MODULE SCOPE (frozen-registry docstring rule). This module
self-registers its default as its LAST statement and resolves nothing at import. It imports
only ``app.ports`` + stdlib at module scope -- ``app.db`` / ``app.models`` / ``settings`` are
lazy-imported inside the methods, so importing this module (which ``app.main`` and
``app.services.jobs`` both do) costs nothing on a default install and cannot form an import
cycle with ``jobs``.

Documented in: docs/architecture/kernel-and-ports.md
"""

import collections
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app import ports

__all__ = [
    "DB_BACKEND_ARMED",
    "FOREIGN_INSTANCE_ROWS",
    "POOLS",
    "DbJobQueue",
    "InMemoryJobQueue",
    "JobQueue",
    "JobQueueError",
    "configure_at_boot",
    "get",
]

log = logging.getLogger("loc_tms.jobqueue")
DB_BACKEND_ARMED = False
FOREIGN_INSTANCE_ROWS = 0

#: How long a claimed job's lease is valid before ``recover`` may re-dispatch it. Short now
#: that a live worker renews it (see LEASE_RENEW_SECONDS): a dead process's work is handed
#: back within about this long.
_LEASE_SECONDS = 300

#: How often the process-wide renewer extends held leases (capped at lease/3 for short leases).
LEASE_RENEW_SECONDS = 30.0

#: The DB adapter's idle poll interval -- how long ``claim`` sleeps between empty polls. A
#: durable single-node queue does not need sub-second dispatch latency; 1s keeps two idle
#: worker threads at ~2 lightweight SELECTs/sec.
_POLL_SECONDS = 1.0

#: S7b: most wake-ups a DB queue keeps for later per pool (a burst enqueued while every claimer
#: is busy). Each one left over costs one claim query when a claimer next goes idle.
_SIGNAL_CAP = 64

#: The worker pools, in priority order (S2): short jobs (exports, notifications, sweeps) before
#: long ones (translate / workflow). A job's pool is decided by its kind (``jobs.LONG_KINDS``).
POOLS = ("short", "long")


def _pid_alive(pid: int) -> bool:
    """Probe without signalling; access denied/unknown failures conservatively mean live."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _boot_worker_rule(worker: str | None) -> str:
    """Classify fresh leases; a reused PID reads as live until lease expiry.

    Legacy/unparsable identities are own-host dead for the first upgrade boot.
    UUIDs disambiguate logs only. This process's PID is reclaimable at startup;
    another live PID on this host is protected, as is any other hostname.
    """
    match = re.fullmatch(r"([^:/]+):([0-9]+):[0-9a-f]{8}/[^/]+", worker or "")
    if match is None:
        return "legacy-own"
    host, pid_text = match.groups()
    if len(pid_text) > 10:
        return "legacy-own"
    pid = int(pid_text)
    max_pid = 0xFFFFFFFF if os.name == "nt" else 0x7FFFFFFF
    if not 0 < pid <= max_pid:
        return "legacy-own"
    if host != socket.gethostname():
        return "foreign-live: other hostname, fresh lease"
    if pid == os.getpid():
        return "own-pid"
    if _pid_alive(pid):
        return "foreign-live: same hostname, live PID, fresh lease"
    return "own-host dead PID"


class JobQueueError(RuntimeError):
    """A LOUD boot failure: ``job_queue_backend`` selects the DB backend but the ``jobqueue``
    table is missing, or the value is not one of ``memory`` / ``db``. Distinct so the boot can
    attribute the failure precisely -- never a silent degrade that drops durable work at
    dispatch time (the s3_blobstore anti-footgun)."""


@runtime_checkable
class JobQueue(Protocol):
    """The background-job dispatch seam. ``enqueue`` makes a submitted job claimable;
    ``claim`` blocks until it can atomically take the next job id (or returns ``None`` after
    ``timeout``); ``release`` marks the finished job's queue entry (the worker calls it in a
    ``finally`` -- the terminal state is derived from ``Job.status``, see the module
    docstring); ``recover`` re-queues never-started / interrupted work at boot and returns the
    count touched."""

    def enqueue(self, job_id: int, pool: str = "long") -> None: ...
    def claim(
        self,
        worker_id: str,
        timeout: float | None = None,
        pools: tuple[str, ...] | None = None,
        *,
        idle: bool = False,  # S7b: the caller's last claim came back empty (see DbJobQueue)
    ) -> int | None: ...
    def release(self, job_id: int) -> None: ...
    def requeue(  # #613: a claimed job back to the tail (S1: optionally after a delay)
        self, job_id: int, delay: float = 0.0, error: str | None = None
    ) -> None: ...
    def renew(self, job_id: int, worker_id: str) -> None: ...  # extend a held lease
    def hand_back(self, job_ids: list[int]) -> None: ...  # graceful drain: running -> pending
    def recover(self, at_boot: bool = False) -> int: ...


#: ADR 0105: job kinds whose runs are SERIALIZED per key, and the payload field that names the key:
#: at most one RUNNING job per ``<kind>:<payload[field]>``, the others wait in submission order
#: (without holding a worker slot: the claim just does not pick them). Today only a project's TM
#: imports: concurrent imports into one project raced on the TM's unique key (S7c).
SERIAL_KINDS: dict[str, str] = {"tm_import": "project_id"}


_SERIAL_ID_MAX = (
    9 * 10**18
)  # inside every database's bigint; larger ids get no key (never an error)


def _serial_id(value) -> int | None:
    """The integer a payload id means: a JSON number truncated toward zero, or a string of digits.
    ``_serial_key_sql`` implements exactly this rule on both dialects (pinned by the suite)."""
    if isinstance(value, bool):
        return None
    try:
        digits = isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18
        if not (isinstance(value, (int, float)) or digits):
            return None
        ident = int(value)
    except (OverflowError, ValueError):
        return None
    return ident if abs(ident) < _SERIAL_ID_MAX else None


def serial_key_for(kind: str | None, payload) -> str | None:
    """The serialization key of a job, or None (every kind not in ``SERIAL_KINDS``, or a payload
    without a usable id). Documented in: docs/architecture/jobs-and-scheduler.md"""
    field = SERIAL_KINDS.get(kind or "")
    if not field or not isinstance(payload, dict):
        return None
    ident = _serial_id(payload.get(field))
    return None if ident is None else f"{kind}:{ident}"


def _serial_key_sql(postgres: bool) -> str:
    """The SQL twin of ``serial_key_for`` over the ``job`` table's ``kind`` and ``payload`` columns,
    so ``enqueue`` writes the key in the same INSERT ... SELECT that creates the row (a worker can
    never claim a keyed row before its key is stored). Same rule as ``_serial_id``: a JSON number
    is cast to an integer (truncated), a digit-only string is its number, anything else is NULL."""
    whens = []
    for kind, field in SERIAL_KINDS.items():
        if postgres:
            t = f"jsonb_typeof((payload)::jsonb->'{field}')"
            txt = f"((payload)::jsonb->>'{field}')"
            num = f"CAST(trunc(CAST({txt} AS numeric)) AS bigint)"
            txt_ok = f"{t} = 'string' AND {txt} ~ '^[0-9]{{1,18}}$'"
            txt_num = f"CAST({txt} AS bigint)"
            expr = (
                f"CASE WHEN {t} = 'number' AND abs(CAST({txt} AS numeric)) < {_SERIAL_ID_MAX} "
                f"THEN {num} WHEN {txt_ok} THEN {txt_num} END"
            )
        else:
            t = f"json_type(payload, '$.{field}')"
            raw = f"json_extract(payload, '$.{field}')"
            expr = (
                f"CASE WHEN {t} IN ('integer', 'real') AND abs({raw}) < {_SERIAL_ID_MAX} "
                f"THEN CAST({raw} AS INTEGER) "
                f"WHEN {t} = 'text' AND length({raw}) BETWEEN 1 AND 18 "
                f"AND {raw} NOT GLOB '*[^0-9]*' "
                f"THEN CAST({raw} AS INTEGER) END"
            )
        whens.append(f"WHEN kind = '{kind}' THEN '{kind}:' || {expr}")
    return "CASE " + " ".join(whens) + " END"


class InMemoryJobQueue:
    """The DEFAULT adapter: the in-process ``queue.Queue`` the app has always used.

    Wrap, don't move -- ``enqueue`` == ``put``, ``claim`` == ``get`` (blocking, byte-identical
    to the old ``_worker_loop``), ``release`` == ``task_done``, and ``recover`` reproduces the
    old ``jobs.recover_interrupted`` body EXACTLY (RUNNING -> FAILED, QUEUED -> re-enqueued and
    workers ensured). The queue is lost with the process, so ``recover`` is the visible-not-
    silent restart contract, unchanged."""

    def __init__(self) -> None:
        # One FIFO per pool under one condition, so a worker can wait on several pools at once
        # (a long worker takes long jobs first, then short ones).
        self._cv = threading.Condition()
        self._qs: dict[str, collections.deque[int]] = {p: collections.deque() for p in POOLS}
        self._pool_of: dict[int, str] = {}  # job_id -> pool, so requeue returns it to its own pool
        # ADR 0105: the serial key of each queued or running job, and the keys running now. A
        # job whose key is running is skipped by ``claim`` (the same rule as the DB adapter).
        self._serial_of: dict[int, str] = {}
        self._busy: set[str] = set()

    def _put(self, job_id: int, pool: str, key: str | None = None) -> None:
        pool = pool if pool in self._qs else "long"
        with self._cv:
            self._pool_of[job_id] = pool
            if key:
                self._serial_of[job_id] = key
            self._qs[pool].append(job_id)
            self._cv.notify_all()

    @staticmethod
    def _lookup_key(job_id: int) -> str | None:
        """The job's serial key, read from its row; None on any failure (never blocks a submit)."""
        try:
            from sqlmodel import Session

            from app.db import engine
            from app.models import Job

            with Session(engine) as s:
                job = s.get(Job, job_id)
                return serial_key_for(job.kind, job.payload) if job else None
        except Exception:
            log.debug("serial key look-up failed for job %s", job_id, exc_info=True)
            return None

    def enqueue(self, job_id: int, pool: str = "long") -> None:
        self._put(job_id, pool, self._lookup_key(job_id))

    def claim(
        self,
        worker_id: str,
        timeout: float | None = None,
        pools: tuple[str, ...] | None = None,
        *,
        idle: bool = False,
    ) -> int | None:
        # timeout=None blocks forever. A finite timeout returns None on expiry. ``pools`` is the
        # pools this worker serves, in priority order (default: every pool, short first).
        # ``idle`` changes nothing here: a memory queue costs nothing to look at.
        order = tuple(p for p in (pools or POOLS) if p in self._qs)
        deadline = None if timeout is None else (time.monotonic() + timeout)
        with self._cv:
            while True:
                for p in order:
                    for jid in self._qs[p]:
                        key = self._serial_of.get(jid)
                        if key and key in self._busy:
                            continue  # ADR 0105: an earlier job of this key is still running
                        if key and self._older_waiting(jid, key):
                            continue  # ADR 0105: submission (job id) order, also after a requeue
                        self._qs[p].remove(jid)
                        if key:
                            self._busy.add(key)
                        return jid
                if deadline is None:
                    self._cv.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cv.wait(remaining)

    def _older_waiting(self, jid: int, key: str) -> bool:
        """True when a queued job of the same key has a lower job id (call under ``_cv``)."""
        return any(
            other < jid and self._serial_of.get(other) == key
            for q in self._qs.values()
            for other in q
        )

    def release(self, job_id: int) -> None:
        with self._cv:
            self._pool_of.pop(job_id, None)
            key = self._serial_of.pop(job_id, None)
            if key:
                self._busy.discard(key)
                self._cv.notify_all()  # the next job of this key may start

    def requeue(self, job_id: int, delay: float = 0.0, error: str | None = None) -> None:
        """Put a claimed job back at the TAIL instead of releasing it (Issue #613: a
        translate job whose engine is at its max_parallel_jobs cap). The id goes back in
        before the claim is marked done, so it is never absent from the queue. With a
        ``delay`` (transient-retry backoff) a daemon timer re-enqueues it later, so no worker
        sleeps; if the process dies first the Job is QUEUED and boot ``recover`` re-enqueues."""
        with self._cv:
            pool = self._pool_of.get(job_id, "long")
            key = self._serial_of.get(job_id)
            if key:
                self._busy.discard(key)  # not running any more (ADR 0105)
        if delay and delay > 0:
            timer = threading.Timer(delay, self._put, args=(job_id, pool, key))
            timer.daemon = True
            timer.start()
        else:
            self._put(job_id, pool, key)

    def renew(self, job_id: int, worker_id: str) -> None:
        """No lease in the in-memory queue: a no-op (interface parity)."""

    def hand_back(self, job_ids: list[int]) -> None:
        """No queue rows to reset: the caller returns the Job to QUEUED and the next boot's
        ``recover`` re-enqueues it. A no-op (interface parity)."""

    def recover(self, at_boot: bool = False) -> int:
        """Byte-identical to the pre-Train-S ``jobs.recover_interrupted``: fail the RUNNING
        rows a restart cut off and re-enqueue the QUEUED ones (lazy imports to avoid an
        import cycle with ``jobs`` / heavy ``app.db``). ``at_boot`` is accepted for interface
        parity with ``DbJobQueue.recover`` and IGNORED: the in-memory queue is per-process, so
        a CLI sweep runs against its OWN empty queue and can never touch the live server's, and
        the behavior stays exactly the pre-Train-S restart contract."""
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Job, JobStatus
        from app.services import jobs

        touched = 0
        with Session(engine) as s:
            for job in s.exec(select(Job).where(Job.status == JobStatus.RUNNING)).all():
                job.status = JobStatus.FAILED
                job.error = "Interrupted by a server restart."
                job.finished_at = datetime.utcnow()
                s.add(job)
                touched += 1
            queued = [
                (j.id, j.kind)
                for j in s.exec(select(Job).where(Job.status == JobStatus.QUEUED)).all()
            ]
            s.commit()
        for jid, kind in queued:
            jobs.ensure_workers()
            self._put(jid, jobs.pool_for(kind), self._lookup_key(jid))
            touched += 1
        return touched


def _candidate_sql(pools: tuple[str, ...] | None, long_kinds, postgres: bool) -> tuple[str, dict]:
    """The SELECT that picks the next claim candidate, plus its extra bound params.

    Joins ``job`` for the kind (no extra column on ``jobqueue``). ``pools=None`` keeps the
    pre-S2 order (by id, any kind). A pool tuple filters to those pools and orders by the
    tuple's priority, then by id: ("short", "long") takes a short row before any long one.
    Kept SQL-text only (and importable) so the Postgres ``FOR UPDATE ... SKIP LOCKED`` form
    is unit-checkable without a Postgres server. SQLite keeps the plain SELECT; its safety is
    the conditional UPDATE after it."""
    from app.models import JobStatus

    extra: dict = {}
    where = "q.state = 'pending' AND (q.not_before IS NULL OR q.not_before <= :now)"
    # ADR 0105: a keyed row (a project's TM import) is a candidate only when no other row of its
    # key is running and none is pending and older: one at a time, in submission order. The
    # older-pending half also keeps the order when two Postgres claimers skip each other's locked
    # row; the partial unique index on running rows is the hard guarantee behind it. "Older" is
    # the JOB id, not the queue row id (a requeue makes a new, higher row id for an old job), and
    # an older PENDING row blocks only while its Job exists and is queued or running: a queue row
    # whose Job was deleted (project delete) must not hold the key for ever. A RUNNING row always
    # blocks, whatever its Job says: the partial unique index refuses a second running row of the
    # key anyway, so letting the next row through would make it the head candidate whose flip fails
    # every round and stalls every other claim (gate round 2 M2); release / the lease sweep free it.
    where += (
        " AND (q.serial_key IS NULL OR NOT EXISTS (SELECT 1 FROM jobqueue r "  # noqa: S608 -- constants only
        "WHERE r.serial_key = q.serial_key AND (r.state = 'running' OR ("
        "r.state = 'pending' AND r.job_id < q.job_id "
        "AND EXISTS (SELECT 1 FROM job jr WHERE jr.id = r.job_id "
        f"AND jr.status IN ('{JobStatus.QUEUED}', '{JobStatus.RUNNING}'))))))"
    )
    order = "q.id"
    if pools:
        wanted = [p for p in pools if p in POOLS] or list(POOLS)
        extra["long_kinds"] = sorted(long_kinds) or [""]
        if set(wanted) == {"short"}:
            where += " AND j.kind NOT IN :long_kinds"
        elif set(wanted) == {"long"}:
            where += " AND j.kind IN :long_kinds"
        elif wanted[0] == "short":
            order = "CASE WHEN j.kind IN :long_kinds THEN 1 ELSE 0 END, q.id"
        else:
            order = "CASE WHEN j.kind IN :long_kinds THEN 0 ELSE 1 END, q.id"
    sql = (
        "SELECT q.id, q.job_id FROM jobqueue q JOIN job j ON j.id = q.job_id "  # noqa: S608 -- fixed SQL; values are bound params
        f"WHERE {where} ORDER BY {order} LIMIT 1"
    )
    if postgres:
        sql += " FOR UPDATE OF q SKIP LOCKED"
    return sql, extra


#: The claim's flip of a pending row to running (both dialects; the WHERE follows).
_CLAIM_FLIP = (
    "UPDATE jobqueue SET state='running', worker=:w, "
    "lease_expires_at=:exp, attempts=attempts+1, updated_at=:now "
)


def _pg_claim_sql(candidate_sql: str) -> str:
    """S7b: the Postgres claim as ONE statement: flip the row `candidate_sql` (the
    ``_candidate_sql`` pick, FOR UPDATE OF q SKIP LOCKED) picks, returning its job id. The pick
    is narrowed to the queue row id, the one column a scalar subquery may return. SQL text only,
    so it is unit-checkable without a Postgres server."""
    pick = candidate_sql.replace("SELECT q.id, q.job_id ", "SELECT q.id ", 1)
    if pick == candidate_sql:
        raise ValueError("unexpected candidate SQL shape")
    return _CLAIM_FLIP + f"WHERE id = ({pick}) AND state='pending' RETURNING job_id"


class DbJobQueue:
    """The DURABLE adapter (``job_queue_backend="db"``): the ``jobqueue`` DB table.

    Registered as the ``"jobqueue"`` OVERRIDE by ``configure_at_boot`` only when the flag is
    on and the table exists. Every method opens its own ``Session`` (worker threads each hold
    their own), lazy-imports ``app.db`` / ``app.models`` (module stays light), and speaks raw
    SQL for the atomic claim / recovery so the compare-and-swap is portable to sqlite AND
    postgres (see the module docstring)."""

    def __init__(self, lease_seconds: int = _LEASE_SECONDS, poll_seconds: float = _POLL_SECONDS):
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._held: dict[int, str] = {}  # job_id -> worker_id, leases this process renews
        self._held_lock = threading.Lock()
        self._renewer: threading.Thread | None = None
        # Wakes this process's idle claimers the moment a row is enqueued, so the poll interval
        # only bounds cross-process latency (a lost wakeup costs at most one poll).
        # S7b: the wake-up is COUNTED. Each enqueue (here, or in another process via
        # app.services.wake) adds one signal for its pool and exactly one idle claimer takes it
        # and runs the claim query. Before, every enqueue sent every idle claimer thread of every
        # worker process to the database at once (a herd of about 8 queries per process per job,
        # a quarter of a worker's CPU under load), and every idle thread polled each second.
        self._cv = threading.Condition()
        self._signals: dict[str | None, int] = {}  # pool (None: unknown) -> pending wake-ups
        self._next_poll: dict[tuple, float] = {}  # pool set -> monotonic time of its next poll

    def _notify(self, n: int = 1, pool: str | None = None) -> None:
        """Wake ``n`` idle claimers for jobs of ``pool`` (None: a pool not known to the
        caller). Signals are kept until taken, capped, so a wake-up that arrives while every
        claimer is busy is not lost."""
        key = pool if pool in POOLS else None
        with self._cv:
            self._signals[key] = min(self._signals.get(key, 0) + max(1, int(n)), _SIGNAL_CAP)
            self._cv.notify_all()

    def _take_signal(self, pools: tuple[str, ...] | None) -> bool:
        """Consume one pending wake-up this claimer can serve (caller holds ``_cv``). A claimer
        that takes long jobs also drains short ones (S2), so it takes any signal; a short-only
        claimer takes only short ones."""
        keys = ("short",) if pools and "long" not in pools else ("long", "short", None)
        for key in keys:
            if self._signals.get(key, 0) > 0:
                self._signals[key] -= 1
                return True
        return False

    def _await_turn(self, pools: tuple[str, ...] | None, wait: float) -> str | None:
        """Wait up to ``wait`` seconds for a wake-up this claimer can take: "signal" (it took
        one), "poll" (none, but this pool set's poll is due) or None (do not query). Without a
        wake-up, ONE claimer per pool set polls the table about every ``_poll_seconds`` (a
        waiter checks when its own wait ends, so two polls can be up to about two intervals
        apart): the fallback for rows no wake-up announced (a lost event, a ``not_before`` that
        just passed, a lease that expired back to pending)."""
        end = time.monotonic() + max(0.0, wait)
        with self._cv:
            while True:
                if self._take_signal(pools):
                    return "signal"
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            now = time.monotonic()
            key = tuple(pools) if pools else ()
            if now >= self._next_poll.get(key, 0.0):
                self._next_poll[key] = now + self._poll_seconds
                return "poll"
            return None

    # --- lease heartbeat -------------------------------------------------------------
    def _renew_interval(self) -> float:
        return max(0.05, min(LEASE_RENEW_SECONDS, self._lease_seconds / 3.0))

    def _hold(self, job_id: int, worker_id: str) -> None:
        with self._held_lock:
            self._held[job_id] = worker_id
            if self._renewer is None or not self._renewer.is_alive():
                self._renewer = threading.Thread(
                    target=self._renew_loop, name="loc-job-lease-renewer", daemon=True
                )
                self._renewer.start()

    def _unhold(self, job_id: int, worker_id: str | None = None) -> None:
        """Stop renewing `job_id`; with `worker_id`, only while that worker still holds it, so
        a stale renewal never drops the lease a later claim of the same job now holds."""
        with self._held_lock:
            if worker_id is None or self._held.get(job_id) == worker_id:
                self._held.pop(job_id, None)

    def _renew_loop(self) -> None:
        while True:
            time.sleep(self._renew_interval())
            with self._held_lock:
                held = list(self._held.items())
            for jid, worker in held:
                try:
                    self.renew(jid, worker)
                except Exception:
                    log.warning("lease renewal failed for job %s", jid, exc_info=True)

    def renew(self, job_id: int, worker_id: str) -> None:
        """Extend the lease of a job this worker holds. A zero-row update means the row was
        reclaimed or finished: stop renewing it."""
        from sqlalchemy import text
        from sqlmodel import Session

        from app.db import engine

        now = datetime.utcnow()
        with Session(engine) as s:
            res = s.execute(
                text(
                    "UPDATE jobqueue SET lease_expires_at=:exp, updated_at=:now "
                    "WHERE job_id=:jid AND state='running' AND worker=:w"
                ),
                {
                    "exp": now + timedelta(seconds=self._lease_seconds),
                    "now": now,
                    "jid": job_id,
                    "w": worker_id,
                },
            )
            s.commit()
        if res.rowcount == 0:
            self._unhold(job_id, worker_id)

    def hand_back(self, job_ids: list[int]) -> None:
        """Graceful drain: return this process's still-running rows to ``pending`` (worker and
        lease cleared, ``attempts`` kept). The caller has already put the Jobs back to QUEUED."""
        if not job_ids:
            return
        from sqlalchemy import bindparam, text
        from sqlmodel import Session

        from app.db import engine

        for jid in job_ids:
            self._unhold(jid)
        with Session(engine) as s:
            s.execute(
                text(
                    "UPDATE jobqueue SET state='pending', worker=NULL, lease_expires_at=NULL, "
                    "updated_at=:now WHERE job_id IN :jids AND state='running'"
                ).bindparams(bindparam("jids", expanding=True)),
                {"now": datetime.utcnow(), "jids": list(job_ids)},
            )
            s.commit()
        self._notify(n=len(job_ids))

    def enqueue(self, job_id: int, pool: str = "long") -> None:
        """INSERT a pending queue row, mirroring the just-written ``Job``'s kind/payload.
        ``pool`` is accepted for interface parity and ignored: this adapter decides a row's
        pool from its job's kind at claim time (``jobs.LONG_KINDS``, read live)."""

        now = datetime.utcnow()
        kind = self._enqueue_copy(job_id, now)
        if kind is None:
            kind = self._enqueue_orm(job_id, now)
        from app.services import jobs

        # The wake-up's pool comes from the kind too (the argument may be the default).
        self._notify(pool=jobs.pool_for(kind) if kind else None)

    def _enqueue_copy(self, job_id: int, now) -> str | None:
        """S7b round 2: the queue row as ONE ``INSERT ... SELECT`` from the job row (the same
        kind and the same stored payload, no ORM object and no JSON round trip). Returns the
        kind, or None when the job row is missing or its payload is SQL NULL: the caller then
        takes the original path, which writes ``{}``. Any other error propagates, as the ORM
        insert's did (no second attempt, so a commit that reached the server cannot be
        written twice). A JSON ``null`` payload is copied as ``null`` (the ORM path wrote
        ``{}``); ``jobs.submit`` always stores a dict, and nothing reads the queue row's
        payload (gate LOW-4)."""
        from sqlalchemy import text

        from app.db import engine
        from app.models import JobQueueState

        # ADR 0105: the serial key is computed in this same statement (see _serial_key_sql).
        key_sql = _serial_key_sql(engine.dialect.name == "postgresql")
        with engine.begin() as cx:
            row = cx.execute(
                text(
                    "INSERT INTO jobqueue (job_id, kind, payload, state, attempts, "  # noqa: S608 -- fixed SQL; values are bound params
                    "enqueued_at, updated_at, serial_key) SELECT id, kind, payload, :st, 0, "
                    f":now, :now, {key_sql} "
                    "FROM job WHERE id = :jid AND payload IS NOT NULL RETURNING kind"
                ),
                {"st": JobQueueState.PENDING, "now": now, "jid": job_id},
            ).first()
        return None if row is None else (row[0] or "")

    def _enqueue_orm(self, job_id: int, now) -> str:
        """The original enqueue: read the Job, insert a mirrored JobQueue row."""
        from sqlmodel import Session

        from app.db import engine
        from app.models import Job, JobQueue, JobQueueState

        with Session(engine) as s:
            job = s.get(Job, job_id)
            kind = job.kind if job else ""
            entry = JobQueue(
                job_id=job_id,
                kind=kind,
                payload=(dict(job.payload or {}) if job else {}),
                state=JobQueueState.PENDING,
                attempts=0,
                enqueued_at=now,
                updated_at=now,
                serial_key=(serial_key_for(job.kind, job.payload) if job else None),
            )
            s.add(entry)
            s.commit()
        return kind

    def claim(
        self,
        worker_id: str,
        timeout: float | None = None,
        pools: tuple[str, ...] | None = None,
        *,
        idle: bool = False,
    ) -> int | None:
        """Atomically take the next pending job id, or return ``None`` after ``timeout``.

        ``pools`` is the pools this worker serves in priority order: ``("short",)`` takes
        only short kinds, ``("long", "short")`` takes any kind, long first, ``None`` takes
        any kind in id order (the pre-S2 shape). A row's pool comes from its job's kind
        against ``jobs.LONG_KINDS`` read live at claim time.

        Blocks (polling every ``_poll_seconds``, woken early by a local enqueue) when
        ``timeout is None`` -- the worker-loop shape. The claim is a compare-and-swap on
        ``state='pending'`` (see the module docstring): a lost race retries immediately,
        not after a sleep. On Postgres the candidate pick also takes ``FOR UPDATE SKIP
        LOCKED`` so concurrent claimers on different nodes never queue on one row.

        S7b: the first look is immediate unless ``idle`` (the worker loop passes it when its
        previous claim came back empty); after that the claimer runs the query only when it
        takes a counted wake-up or its pool set's poll is due (``_await_turn``), so idle
        claimers do not query the table on every wake-up or every timeout."""
        from app.db import engine

        deadline = None if timeout is None else (time.monotonic() + timeout)
        postgres = engine.dialect.name == "postgresql"
        look = "entry" if not idle else self._await_turn(pools, 0.0)
        while True:
            if look:
                claimed, seen = self._try_claim(worker_id, pools, postgres)
                if claimed is not None:
                    if look != "signal":
                        with self._cv:  # this job's own wake-up, if still pending, is spent
                            self._take_signal(pools)
                    return claimed
                if seen:
                    # Lost the race for an available candidate -- retry at once, do NOT sleep.
                    continue
            # Nothing pending: honor the timeout, else wait for a wake-up or the next poll.
            wait = self._poll_seconds
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                wait = min(wait, remaining)
            look = self._await_turn(pools, wait)

    def _try_claim(
        self, worker_id: str, pools: tuple[str, ...] | None, postgres: bool
    ) -> tuple[int | None, bool]:
        """One claim attempt: (the claimed job id or None, whether a candidate was seen and
        lost to another claimer). Core connection, no ORM session: nothing here is an object.
        On Postgres the SKIP LOCKED pick and the flip are ONE statement (``UPDATE ... WHERE id
        = (pick) RETURNING``): the picked row is locked by this transaction, so it cannot be
        lost; SQLite keeps the select-then-CAS shape."""
        from sqlalchemy import bindparam, text

        from app.db import engine
        from app.services import jobs

        now = datetime.utcnow()
        sql, extra = _candidate_sql(pools, jobs.LONG_KINDS, postgres)
        params = {
            "now": now,
            "w": worker_id,
            "exp": now + timedelta(seconds=self._lease_seconds),
            **extra,
        }
        from sqlalchemy.exc import IntegrityError

        with engine.connect() as cx:
            try:
                if postgres:
                    stmt = text(_pg_claim_sql(sql))
                    if "long_kinds" in extra:
                        stmt = stmt.bindparams(bindparam("long_kinds", expanding=True))
                    row = cx.execute(stmt, params).first()
                    if row is None:
                        return None, False
                    cx.commit()
                    self._hold(row[0], worker_id)
                    return row[0], False
                stmt = text(sql)
                if "long_kinds" in extra:
                    stmt = stmt.bindparams(bindparam("long_kinds", expanding=True))
                row = cx.execute(stmt, {"now": now, **extra}).first()
                if row is None:
                    return None, False
                qid, jid = row[0], row[1]
                res = cx.execute(
                    text(_CLAIM_FLIP + "WHERE id=:qid AND state='pending'"),
                    {"w": worker_id, "exp": params["exp"], "now": now, "qid": qid},
                )
                cx.commit()
            except IntegrityError:
                # ADR 0105: another claimer flipped a row of the SAME serial_key to running
                # between our pick and our flip; ux_jobqueue_serial_running refused ours. Nothing
                # was changed (the failed statement is rolled back: the row stays pending, no
                # lease), so this round claims nothing; the next look sees the key running.
                cx.rollback()
                return None, False
            if res.rowcount == 1:
                self._hold(jid, worker_id)  # this worker won the CAS
                return jid, False
            return None, True  # another worker won this candidate

    def release(self, job_id: int) -> None:
        """Mark the queue entry terminal, DERIVING done/failed from ``Job.status`` (the one
        authority ``_execute`` writes). A no-op if the entry is not ``running`` (e.g. a job
        whose ``_execute`` early-returned because it was already terminal)."""
        from sqlalchemy import text

        from app.db import engine
        from app.models import JobQueueState, JobStatus

        now = datetime.utcnow()
        # S7b: the status column only, on a core connection (the whole Job row
        # through the ORM, payload and result JSON decoded, was read just for its status).
        with engine.begin() as cx:
            status = cx.execute(
                text("SELECT status FROM job WHERE id=:jid"), {"jid": job_id}
            ).scalar()
            final = JobQueueState.DONE if status == JobStatus.COMPLETED else JobQueueState.FAILED
            freed = cx.execute(
                text(
                    "UPDATE jobqueue SET state=:st, worker=NULL, lease_expires_at=NULL, "
                    "updated_at=:now WHERE job_id=:jid AND state='running' RETURNING serial_key"
                ),
                {"st": final, "now": now, "jid": job_id},
            ).first()
        self._unhold(job_id)
        if freed is not None and freed[0]:
            # ADR 0105: a serial key just became free: wake the claimer of the next job of this
            # key now (here and in the other processes) instead of leaving it to the poll.
            self._notify(pool="long")
            try:
                from app.services import wake

                wake.poke("jobs", pool="long")
            except Exception:
                log.debug("wake after a serial key was freed failed", exc_info=True)

    def requeue(self, job_id: int, delay: float = 0.0, error: str | None = None) -> None:
        """Return a claimed job to the TAIL of the queue in ONE transaction (Issue #613: a
        translate job whose engine is at its max_parallel_jobs cap). The claimed ``running``
        row is removed and a fresh ``pending`` row (higher id, so behind the current backlog)
        is inserted in the same commit. Never ``enqueue`` + ``release``: release keys on
        ``job_id``, so it would also mark a second worker's fresh claim of the new row
        ``failed`` mid-run, and that row would then be skipped by crash recovery (the Job
        stuck RUNNING forever). A crash before the commit leaves the ``running`` row, which
        recovery returns to pending as usual. ``attempts`` carries over.

        ``delay`` (transient-retry backoff) sets the new row's ``not_before`` to now + delay
        so ``claim`` skips it until then; ``error`` is stored in ``last_error``."""
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Job, JobQueue, JobQueueState

        now = datetime.utcnow()
        with Session(engine) as s:
            claimed = s.exec(
                select(JobQueue).where(
                    JobQueue.job_id == job_id, JobQueue.state == JobQueueState.RUNNING
                )
            ).all()
            attempts = max((r.attempts or 0 for r in claimed), default=0)
            for row in claimed:
                s.delete(row)
            job = s.get(Job, job_id)
            kind = job.kind if job else ""
            s.add(
                JobQueue(
                    job_id=job_id,
                    kind=(job.kind if job else ""),
                    payload=(dict(job.payload or {}) if job else {}),
                    state=JobQueueState.PENDING,
                    attempts=attempts,
                    enqueued_at=now,
                    updated_at=now,
                    not_before=(now + timedelta(seconds=delay)) if delay and delay > 0 else None,
                    last_error=(error[:500] if error else None),
                    serial_key=(serial_key_for(job.kind, job.payload) if job else None),
                )
            )
            s.commit()
        self._unhold(job_id)
        from app.services import jobs

        self._notify(pool=jobs.pool_for(kind) if kind else None)  # S7b: its own pool's claimers

    def recover(self, at_boot: bool = False) -> int:
        """Return interrupted ``running`` rows to ``pending`` and reset each backing ``Job``
        (RUNNING -> QUEUED, ``started_at`` cleared) so ``_execute`` re-runs it -- the
        durable-queue restart recovery. Ensures workers if it re-queued anything. Returns the
        count.

        At boot host/PID liveness permits dead predecessors, this PID and legacy
        workers regardless of lease, plus NULL/expired leases from any instance.
        Foreign-live fresh leases are preserved and reported. PID reuse reads as
        live until lease expiry and a subsequent boot or manual recovery.
        The default live sweep remains lease-only. A live worker's lease is renewed by the
        heartbeat (see the module docstring), so the sweep never reclaims a live long job.

        The ``Job`` reset is guarded on ``status='running'`` either way, so a job that actually
        COMPLETED before the crash is left terminal (and ``_execute`` would refuse it anyway)."""
        from sqlalchemy import text
        from sqlmodel import Session

        from app.db import engine

        global FOREIGN_INSTANCE_ROWS

        now = datetime.utcnow()
        lease_pred = " AND (lease_expires_at IS NULL OR lease_expires_at < :now)"
        params = {"now": now}
        foreign_rows = []
        with Session(engine) as s:
            if at_boot:
                workers = s.execute(
                    text(
                        "SELECT worker, COUNT(*) FROM jobqueue WHERE state='running' "
                        "AND lease_expires_at >= :now GROUP BY worker"
                    ),
                    params,
                ).fetchall()
                own_terms = ["worker IS NULL"]
                for index, (worker, count) in enumerate(workers):
                    rule = _boot_worker_rule(worker)
                    if rule.startswith("foreign-live:"):
                        foreign_rows.append((worker, count, rule))
                    elif worker is not None:
                        bind = f"own_worker_{index}"
                        own_terms.append(f"worker = :{bind}")
                        params[bind] = worker
                own_pred = " OR ".join(own_terms)
                lease_pred = (
                    f" AND ({own_pred} OR lease_expires_at IS NULL OR lease_expires_at < :now)"
                )
            # RETURNING ties the Job reset to rows actually won by this guarded UPDATE,
            # including when another transaction changes a candidate before we update it.
            jids = [
                row[0]
                for row in s.execute(
                    text(
                        "UPDATE jobqueue SET state='pending', worker=NULL, "  # noqa: S608 -- fixed SQL; values are bound params
                        "lease_expires_at=NULL, updated_at=:now WHERE state='running'"
                        + lease_pred
                        + " RETURNING job_id"
                    ),
                    params,
                ).fetchall()
            ]
            if jids:
                from sqlalchemy import bindparam

                s.execute(
                    text(
                        "UPDATE job SET status='queued', started_at=NULL "
                        "WHERE id IN :jids AND status='running'"
                    ).bindparams(bindparam("jids", expanding=True)),
                    {"jids": jids},
                )
            s.commit()
        if at_boot:
            from app import config

            foreign_count = sum(row[1] for row in foreign_rows)
            if config.ROLE != "all":
                # S3 (ADR 0095): with split roles, live peer worker processes are the design,
                # not a second instance. Their rows were left alone above all the same.
                if foreign_count:
                    log.info("left %s job(s) held by live peer workers", foreign_count)
                foreign_count = 0
            FOREIGN_INSTANCE_ROWS = foreign_count  # Single atomic publication for health readers.
            if foreign_count:
                log.error(
                    "another instance appears live against this database; its in-flight jobs "
                    "were not reclaimed: count=%s, workers/counts/classification_rules=%s",
                    foreign_count,
                    foreign_rows,
                )
        if jids:
            from app.services import jobs

            jobs.ensure_workers()
        return len(jids)


def get() -> "JobQueue":
    """The active JobQueue adapter: override-first (the DB adapter installed by
    ``configure_at_boot``, or a test hot-swap), else the default ``InMemoryJobQueue``,
    resolved through the frozen ``app.ports`` registry. ``jobs`` calls ``jobqueue.get()`` for
    every enqueue / claim / release / recover -- a call-time lookup, so a boot-time swap to
    the DB adapter is honored without a captured reference."""
    return ports.get("jobqueue")


def configure_at_boot() -> None:
    """Select the job-queue backend at boot: ``job_queue_backend="db"`` (the default since S2)
    registers ``DbJobQueue``; the opt-out ``"memory"`` is a STRICT NO-OP (the
    ``InMemoryJobQueue`` self-registered below stays active).

    The backend read is the FIRST decision (the s3_blobstore shape): ``"memory"`` returns
    before any ``app.db`` / inspector import, so an opted-out install touches nothing.
    RAISES ``JobQueueError`` loudly (fails the boot) when the value is neither
    ``memory`` nor ``db``, or when ``db`` is selected but the ``jobqueue`` table is absent --
    never a silent degrade that would drop durable work at dispatch time."""
    from app.services import settings

    backend = (settings.get("job_queue_backend") or "db").strip() or "db"
    if backend == "memory":
        from app import config

        if config.ROLE != "all":
            # S3 (ADR 0095): a web process and its workers share work only through the DB.
            raise JobQueueError(
                f"LOC_TMS_ROLE={config.ROLE} needs the durable job queue, but "
                "job_queue_backend=memory. Set `config set job_queue_backend db` on the host, "
                "or run the single-process role (LOC_TMS_ROLE=all)."
            )
        return  # FIRST decision -- before any app.db import / inspector / adapter build
    if backend != "db":
        raise JobQueueError(
            "job_queue_backend must be 'memory' (the default in-process queue) or 'db' "
            f"(the durable DB-backed queue), got {backend!r}. Set it with "
            "`config set job_queue_backend memory|db` on the host, or turn it back to memory."
        )
    from sqlalchemy import inspect as sqla_inspect

    from app.db import engine

    if not sqla_inspect(engine).has_table("jobqueue"):
        raise JobQueueError(
            "job_queue_backend=db but the 'jobqueue' table does not exist. The durable job "
            "queue needs its table -- run `alembic upgrade head` on the host (or rebuild the "
            "schema), or set `config set job_queue_backend memory`. A misconfigured install "
            "must not boot into a state where it silently drops durable background work."
        )
    global DB_BACKEND_ARMED

    ports.register("jobqueue", DbJobQueue())
    DB_BACKEND_ARMED = True
    # DEBUG, not INFO: the one-shot CLI arms the queue too, and an INFO line on every command
    # lands on stderr (the server boot reports the queue on /health instead).
    log.debug("Durable DB-backed job queue armed (job_queue_backend=db, jobqueue table).")


# Self-register the default adapter as the module's LAST statement (frozen-registry docstring
# rule): the choice defaults to the in-memory queue; configure_at_boot installs the DB
# override at boot when the flag selects it. A default registered here survives even if an
# import failure were to occur lower in the module.
ports.register_default("jobqueue", InMemoryJobQueue())
