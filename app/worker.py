"""The worker process: `python -m app.worker` (S3 process roles, ADR 0095).

Runs the background work of Localization OS without serving HTTP: the job pools (short and
long, S2), the workflow-run admission scheduler with step hand-off and continuation (S2), the
scheduler tick and its sweeps, the S1 stale-run / expired-lease sweep, and the boot recovery of
interrupted jobs, batches and runs. It shares the database, data directory and configuration
with the web process (`LOC_TMS_ROLE=web`), which queues work as durable rows and runs none of
it. Start as many workers as the box has cores to spare; they claim work from the database
(conditional UPDATE on SQLite, `FOR UPDATE SKIP LOCKED` on Postgres), so no two take the same
job or run, and the S1 owner heartbeat lets any worker resume a run whose worker died.

A worker is woken through `app.services.wake` (the S3a events bus) when the web process queues
work; without that hint it still finds the work by polling.

Stop it with SIGTERM or Ctrl-C: it stops claiming, waits up to LOC_TMS_DRAIN_SECONDS (default 8)
for running jobs and hands the rest back to the queue, exactly like the web shutdown drain.
Runs still executing when the process exits are resumed from their last checkpoint by a peer
worker's sweep or by the next boot (S1).

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading


def main() -> int:
    os.environ.setdefault("LOC_TMS_ROLE", "worker")
    from app import config

    if config.ROLE != "worker":
        print(
            f"app.worker runs with LOC_TMS_ROLE=worker (got {config.ROLE}); "
            "unset it or set it to worker.",
            file=sys.stderr,
        )
        return 2

    import logging

    from app.logging_config import configure_logging

    configure_logging()
    log = logging.getLogger("loc_tms.worker")

    from app import main as app_main
    from app.services import jobs, leader, orchestrator, wake

    # S3c (ADR 0097): web and worker processes boot one at a time (init_db, seeding, recovery);
    # the protected-data-dir guard (#13) runs before the boot lock touches the data directory.
    config.assert_data_dir_allowed()
    with leader.boot_lock():
        app_main.boot_common()
        app_main.recover_background_work()
    jobs.ensure_workers()
    orchestrator.ADMISSION.ensure_started()
    wake.listen()
    stop = threading.Event()
    if not config.IS_TEST:
        # Same rule as the HTTP process: an embedded install's driver is a no-op (an external
        # cron drives the tick); the hermetic suites drive the tick themselves. With N workers
        # exactly ONE runs the tick and the retention sweep: the leader (gate S3b H1).
        from app.ports import scheduler as scheduler_port
        from app.services import leader

        leader.run_when_leader("scheduler", scheduler_port.get().start, stop=stop)

    def _stop(signum, _frame):
        log.info("worker %s: signal %s, draining", os.getpid(), signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):  # not the main thread / platform
            signal.signal(sig, _stop)
    log.info(
        "worker %s up: %s long + %s short job workers, %s concurrent runs",
        os.getpid(),
        jobs.WORKER_COUNT,
        jobs.SHORT_WORKER_COUNT,
        config.MAX_CONCURRENT_RUNS,
    )
    while not stop.wait(1.0):
        orchestrator.ADMISSION.supervise()
    try:
        drain_s = float(os.environ.get("LOC_TMS_DRAIN_SECONDS", "8"))
    except ValueError:
        drain_s = 8.0
    handed = jobs.drain(drain_s)
    if handed:
        log.warning("worker %s: drain handed %s running job(s) back", os.getpid(), handed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
