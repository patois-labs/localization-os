"""Cross-process wake-ups for the background work (S3 process roles, ADR 0095).

A web-role process (`LOC_TMS_ROLE=web`) queues work as durable rows (a Job on the DB queue, a
QUEUED workflow run) but executes none of it. Worker processes find that work by polling (the
DB job claim every `_POLL_SECONDS`, the run admission scheduler every `QUEUE_POLL_S`); this
module removes the poll latency: `poke()` publishes one small event on `CHANNEL` through the S3a
bus (`app.services.events`: Postgres LISTEN/NOTIFY, SQLite polled table), and every worker's
`listen()` thread turns it into the same in-process wake-ups a local enqueue already gives.

An event is only a hint. A lost one costs at most one poll interval: the rows stay the truth.

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("loc_tms.wake")

#: The events channel worker processes listen on.
CHANNEL = "work:wake"

_lock = threading.Lock()
_listener: threading.Thread | None = None


def poke(what: str = "work", *, pool: str | None = None) -> None:
    """Tell worker processes that new work is queued ("jobs", "runs" or "work"). Only a
    process that does not execute background work itself (the web role) needs to; in the
    single-process role the in-process notifications already reach the workers. Never
    raises: a failed publish only costs the poll interval. A "jobs" poke announces ONE job
    and `pool` names its job pool (S7b): each worker process wakes one claimer for it."""
    from app import config

    if config.runs_background_work():
        return
    try:
        from app.services import events

        events.publish(CHANNEL, {"what": what, "pool": pool} if pool else {"what": what})
    except Exception:
        log.debug("wake publish failed; workers find the work on their next poll", exc_info=True)


def run_changed(run_id, what: str = "change") -> None:
    """A worker process tells the web role that run `run_id` moved (a step committed, the
    progress counter persisted, the run paused or ended), so an open run stream re-reads it
    at once instead of on its next poll. Only the worker role publishes: in the single
    process the stream polls the same database it would re-read anyway. Never raises."""
    from app import config

    if config.ROLE != "worker" or run_id is None:
        return
    try:
        from app.services import events

        events.publish(events.run_channel(int(run_id)), {"what": what})
    except Exception:
        log.debug("run event publish failed for run %s", run_id, exc_info=True)


def _wake_local(payloads=None) -> None:
    """Turn received pokes into in-process wake-ups, by kind (S7b). A "jobs" poke
    wakes one job claimer per job, for its pool, and no longer the run admission scheduler:
    that pass (batch finalize, queued-run reads) had nothing to do for a job and cost a worker
    about 6 % of its CPU under a translate load. "runs" wakes only the scheduler; anything else
    ("work", an unknown payload) wakes both, as before."""
    runs = False
    jobs_by_pool: dict = {}
    for p in payloads or ({"what": "work"},):
        what = p.get("what") if isinstance(p, dict) else None
        if what != "jobs":
            runs = True
        if what != "runs":
            pool = p.get("pool") if what == "jobs" else None
            jobs_by_pool[pool] = jobs_by_pool.get(pool, 0) + 1
    if runs:
        try:
            from app.services import orchestrator

            orchestrator.ADMISSION.notify()
        except Exception:
            log.debug("could not wake the run admission scheduler", exc_info=True)
    if jobs_by_pool:
        try:
            from app.ports import jobqueue

            notify = getattr(jobqueue.get(), "_notify", None)
            if notify is not None:
                for pool, n in jobs_by_pool.items():
                    notify(n=n, pool=pool)
        except Exception:
            log.debug("could not wake the job claimers", exc_info=True)


def _listen_loop() -> None:
    from app.services import events

    while True:
        try:
            with events.subscribe(CHANNEL) as sub:
                while True:
                    got = sub.poll(timeout=30)
                    if got:
                        _wake_local(got)
        except Exception:
            log.warning("wake listener failed; retrying in 5 s", exc_info=True)
            threading.Event().wait(5)


def listen() -> bool:
    """Start this process's wake listener (idempotent). Called by worker processes."""
    global _listener
    with _lock:
        if _listener is not None and _listener.is_alive():
            return False
        _listener = threading.Thread(target=_listen_loop, name="loc-wake-listener", daemon=True)
        _listener.start()
        return True
