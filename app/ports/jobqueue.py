"""JobQueue port -- the named background-job DISPATCH seam (sprint Train S, STRETCH).

This module NAMES the seam Localization OS uses to hand a submitted ``Job`` to a worker thread
and to recover work across a restart. Before this train that seam was un-named: it was the
bare ``queue.Queue`` + ``recover_interrupted`` body inside ``app.services.jobs``. This port
introduces the MINIMAL interface those operations already had -- ``enqueue`` / ``claim`` /
``release`` / ``recover`` -- adapts the existing in-memory implementation to it as the
DEFAULT (byte-identical), and adds a DURABLE, DB-backed adapter behind a flag.

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

THE ATOMIC CLAIM (works on BOTH sqlite and postgres)
----------------------------------------------------
``DbJobQueue.claim`` selects the lowest pending row, then flips it with
``UPDATE jobqueue SET state='running', ... WHERE id=:qid AND state='pending'`` and checks
``rowcount``. That conditional UPDATE is a compare-and-swap: if two workers pick the SAME
candidate, exactly one UPDATE matches ``state='pending'`` (rowcount 1 -> it won) and the
other matches zero rows (rowcount 0 -> it lost and retries). SQLite serializes writers (WAL:
one writer at a time) so the loser sees ``state='running'`` by the time its UPDATE runs;
Postgres row-locks it identically. No ``SKIP LOCKED`` is needed -- the guard is the
``state='pending'`` predicate, portable to both. The ``attempts`` counter is bumped on each
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
load-bearing here, not redundant. Lease HEARTBEAT RENEWAL is still absent (named F04
follow-up, out of scope here). A job legitimately running longer than ``_LEASE_SECONDS``
is still the one narrow pre-existing window where either recovery arm can re-dispatch it.
ONE app process per database is supported; the fence detects evidence of another process
at boot and surfaces it on /health, but does not prevent a second process from starting.

FLAG-OFF TOUCHES NOTHING (the s3_blobstore fail-loud shape)
-----------------------------------------------------------
The DEFAULT adapter (self-registered at import) is ``InMemoryJobQueue``. ``configure_at_boot``
reads ``job_queue_backend``: ``"memory"`` (default) is the FIRST-statement no-op -- the DB
adapter is never built, the ``jobqueue`` table is never touched, dispatch is byte-identical
to pre-Train-S. ``"db"`` registers ``DbJobQueue`` as the override, FAILING THE BOOT
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

import logging
import os
import queue
import re
import socket
import time
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app import ports

__all__ = [
    "DB_BACKEND_ARMED",
    "FOREIGN_INSTANCE_ROWS",
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

#: How long a claimed job's lease is valid before ``recover`` may re-dispatch it. Generous
#: (an hour) because v1 has no heartbeat renewal: it must comfortably exceed any real
#: handler's runtime so a live single-node worker's job is never re-queued out from under it
#: during boot recovery or a CLI sweep. See the module docstring's F04 note.
_LEASE_SECONDS = 3600

#: The DB adapter's idle poll interval -- how long ``claim`` sleeps between empty polls. A
#: durable single-node queue does not need sub-second dispatch latency; 1s keeps two idle
#: worker threads at ~2 lightweight SELECTs/sec.
_POLL_SECONDS = 1.0


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

    def enqueue(self, job_id: int) -> None: ...
    def claim(self, worker_id: str, timeout: float | None = None) -> int | None: ...
    def release(self, job_id: int) -> None: ...
    def requeue(self, job_id: int) -> None: ...  # #613: a claimed job back to the tail
    def recover(self, at_boot: bool = False) -> int: ...


class InMemoryJobQueue:
    """The DEFAULT adapter: the in-process ``queue.Queue`` the app has always used.

    Wrap, don't move -- ``enqueue`` == ``put``, ``claim`` == ``get`` (blocking, byte-identical
    to the old ``_worker_loop``), ``release`` == ``task_done``, and ``recover`` reproduces the
    old ``jobs.recover_interrupted`` body EXACTLY (RUNNING -> FAILED, QUEUED -> re-enqueued and
    workers ensured). The queue is lost with the process, so ``recover`` is the visible-not-
    silent restart contract, unchanged."""

    def __init__(self) -> None:
        self._q: queue.Queue[int] = queue.Queue()

    def enqueue(self, job_id: int) -> None:
        self._q.put(job_id)

    def claim(self, worker_id: str, timeout: float | None = None) -> int | None:
        # timeout=None blocks forever -- byte-identical to the old `_queue.get()`. A finite
        # timeout (tests) returns None on expiry rather than blocking.
        if timeout is None:
            return self._q.get()
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def release(self, job_id: int) -> None:
        self._q.task_done()

    def requeue(self, job_id: int) -> None:
        """Put a claimed job back at the TAIL instead of releasing it (Issue #613: a
        translate job whose engine is at its max_parallel_jobs cap). The id goes back in
        before the claim is marked done, so it is never absent from the queue."""
        self._q.put(job_id)
        self._q.task_done()

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
            queued = [j.id for j in s.exec(select(Job).where(Job.status == JobStatus.QUEUED)).all()]
            s.commit()
        for jid in queued:
            jobs.ensure_workers()
            self._q.put(jid)
            touched += 1
        return touched


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

    def enqueue(self, job_id: int) -> None:
        """INSERT a pending queue row, mirroring the just-written ``Job``'s kind/payload."""
        from sqlmodel import Session

        from app.db import engine
        from app.models import Job, JobQueue, JobQueueState

        now = datetime.utcnow()
        with Session(engine) as s:
            job = s.get(Job, job_id)
            entry = JobQueue(
                job_id=job_id,
                kind=(job.kind if job else ""),
                payload=(dict(job.payload or {}) if job else {}),
                state=JobQueueState.PENDING,
                attempts=0,
                enqueued_at=now,
                updated_at=now,
            )
            s.add(entry)
            s.commit()

    def claim(self, worker_id: str, timeout: float | None = None) -> int | None:
        """Atomically take the next pending job id, or return ``None`` after ``timeout``.

        Blocks (polling every ``_poll_seconds``) when ``timeout is None`` -- the worker-loop
        shape -- so a DB worker behaves like the in-memory ``queue.get()`` from above the
        seam. The claim is a compare-and-swap on ``state='pending'`` (see the module
        docstring): a lost race retries immediately, not after a sleep."""
        from sqlalchemy import text
        from sqlmodel import Session

        from app.db import engine

        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            now = datetime.utcnow()
            claimed = None
            with Session(engine) as s:
                row = s.execute(
                    text(
                        "SELECT id, job_id FROM jobqueue WHERE state = 'pending' "
                        "ORDER BY id LIMIT 1"
                    )
                ).first()
                if row is not None:
                    qid, jid = row[0], row[1]
                    res = s.execute(
                        text(
                            "UPDATE jobqueue SET state='running', worker=:w, "
                            "lease_expires_at=:exp, attempts=attempts+1, updated_at=:now "
                            "WHERE id=:qid AND state='pending'"
                        ),
                        {
                            "w": worker_id,
                            "exp": now + timedelta(seconds=self._lease_seconds),
                            "now": now,
                            "qid": qid,
                        },
                    )
                    s.commit()
                    if res.rowcount == 1:
                        claimed = jid  # this worker won the CAS
                    # rowcount 0 -> another worker won this candidate; fall through and retry
            if claimed is not None:
                return claimed
            if row is not None:
                # Lost the race for an available candidate -- retry at once, do NOT sleep.
                continue
            # Nothing pending: honor the timeout, else poll again after a short sleep.
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(self._poll_seconds)

    def release(self, job_id: int) -> None:
        """Mark the queue entry terminal, DERIVING done/failed from ``Job.status`` (the one
        authority ``_execute`` writes). A no-op if the entry is not ``running`` (e.g. a job
        whose ``_execute`` early-returned because it was already terminal)."""
        from sqlalchemy import text
        from sqlmodel import Session

        from app.db import engine
        from app.models import Job, JobQueueState, JobStatus

        now = datetime.utcnow()
        with Session(engine) as s:
            job = s.get(Job, job_id)
            final = (
                JobQueueState.DONE
                if (job is not None and job.status == JobStatus.COMPLETED)
                else JobQueueState.FAILED
            )
            s.execute(
                text(
                    "UPDATE jobqueue SET state=:st, worker=NULL, lease_expires_at=NULL, "
                    "updated_at=:now WHERE job_id=:jid AND state='running'"
                ),
                {"st": final, "now": now, "jid": job_id},
            )
            s.commit()

    def requeue(self, job_id: int) -> None:
        """Return a claimed job to the TAIL of the queue in ONE transaction (Issue #613: a
        translate job whose engine is at its max_parallel_jobs cap). The claimed ``running``
        row is removed and a fresh ``pending`` row (higher id, so behind the current backlog)
        is inserted in the same commit. Never ``enqueue`` + ``release``: release keys on
        ``job_id``, so it would also mark a second worker's fresh claim of the new row
        ``failed`` mid-run, and that row would then be skipped by crash recovery (the Job
        stuck RUNNING forever). A crash before the commit leaves the ``running`` row, which
        recovery returns to pending as usual. ``attempts`` carries over."""
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
            s.add(
                JobQueue(
                    job_id=job_id,
                    kind=(job.kind if job else ""),
                    payload=(dict(job.payload or {}) if job else {}),
                    state=JobQueueState.PENDING,
                    attempts=attempts,
                    enqueued_at=now,
                    updated_at=now,
                )
            )
            s.commit()

    def recover(self, at_boot: bool = False) -> int:
        """Return interrupted ``running`` rows to ``pending`` and reset each backing ``Job``
        (RUNNING -> QUEUED, ``started_at`` cleared) so ``_execute`` re-runs it -- the
        durable-queue restart recovery. Ensures workers if it re-queued anything. Returns the
        count.

        At boot host/PID liveness permits dead predecessors, this PID and legacy
        workers regardless of lease, plus NULL/expired leases from any instance.
        Foreign-live fresh leases are preserved and reported. PID reuse reads as
        live until lease expiry and a subsequent boot or manual recovery.
        The default live sweep remains lease-only.
        F04 heartbeat renewal remains out of scope; see the module docstring.

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
            foreign_count = sum(row[1] for row in foreign_rows)
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
    """Select the job-queue backend at boot IFF ``job_queue_backend="db"``; a STRICT NO-OP on
    the default ``"memory"`` (the ``InMemoryJobQueue`` self-registered below stays active).

    The backend read is the FIRST decision (the s3_blobstore shape): ``"memory"`` returns
    before any ``app.db`` / inspector import, so ``app.main``'s unconditional boot call is a
    true no-op on a default install (the in-process verify suites depend on flag-off touching
    nothing). RAISES ``JobQueueError`` loudly (fails the boot) when the value is neither
    ``memory`` nor ``db``, or when ``db`` is selected but the ``jobqueue`` table is absent --
    never a silent degrade that would drop durable work at dispatch time."""
    from app.services import settings

    backend = (settings.get("job_queue_backend") or "memory").strip() or "memory"
    if backend == "memory":
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
    log.info("Durable DB-backed job queue armed (job_queue_backend=db, jobqueue table).")


# Self-register the default adapter as the module's LAST statement (frozen-registry docstring
# rule): the choice defaults to the in-memory queue; configure_at_boot installs the DB
# override at boot when the flag selects it. A default registered here survives even if an
# import failure were to occur lower in the module.
ports.register_default("jobqueue", InMemoryJobQueue())
