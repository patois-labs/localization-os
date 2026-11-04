"""One leader among worker processes for work that must run exactly once (S3, ADR 0095).

The scheduler tick (connector syncs, vendor polls and retries, notification sweeps, the
lifecycle reapers, audit-chain verify, LLMemory housekeeping) and the retention purge are
serialized only inside one process (`scheduler._tick_lock`, `jobs.submit_unique`'s in-process
lock). In the single-process role that is enough; with N worker processes (`LOC_TMS_ROLE=worker`)
every worker would run them and a due connector would sync twice (double MT spend, double
writes to the customer's Airtable or Jira). So exactly one worker runs them: the one holding
the leadership.

* Postgres: a session advisory lock (`pg_try_advisory_lock`) on a dedicated connection
  outside the pool, held for the life of the process. It works across hosts and is released
  by the server when the holder's connection or process dies.
* SQLite: an exclusive non-blocking lock on a file in the data directory (one box, which is
  SQLite's deployment). The operating system releases it when the holder dies.

A non-leader worker keeps trying every `RETRY_S` seconds, so another worker takes over when
the leader stops. If the leader's Postgres lock connection fails, the leadership may already
have passed to a peer, so `watch()` exits the process (the container supervisor restarts the
group) rather than risk two leaders.

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import zlib

log = logging.getLogger("loc_tms.leader")

#: How often a non-leader tries again, and how often the leader checks its lock connection.
RETRY_S = 15.0

_lock = threading.Lock()
#: name -> the held handle (a psycopg connection, or an open locked file).
_held: dict[str, object] = {}


def _key(name: str) -> int:
    """A stable signed advisory lock key for `name` in THIS database (advisory locks are
    cluster-wide, so two installs sharing one Postgres server must not share a leader)."""
    try:
        from app.db import engine

        db = engine.url.database or ""
    except Exception:  # pragma: no cover
        db = ""
    return zlib.crc32(f"loc_tms:leader:{db}:{name}".encode()) - (1 << 31)


def is_leader(name: str) -> bool:
    with _lock:
        return name in _held


def try_acquire(name: str) -> bool:
    """Take the leadership `name` for this process if nobody holds it. True when this
    process holds it (now or already)."""
    with _lock:
        if name in _held:
            return True
        from app import config

        handle = _acquire_sqlite(name) if config.IS_SQLITE else _acquire_pg(name)
        if handle is None:
            return False
        _held[name] = handle
        log.info("process %s is the %s leader", os.getpid(), name)
        return True


def release(name: str) -> None:
    with _lock:
        handle = _held.pop(name, None)
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        log.debug("closing the %s leadership failed", name, exc_info=True)


def _acquire_pg(name: str):
    from app.services import events

    try:
        conn = events._pg_listen_connection()  # dedicated autocommit connection, not pooled
    except Exception:
        log.warning("could not open the %s leader connection", name, exc_info=True)
        return None
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_key(name),)).fetchone()[0]
    except Exception:
        log.warning("the %s leader lock query failed", name, exc_info=True)
        got = False
    if not got:
        conn.close()
        return None
    return conn


class _FileLock:
    def __init__(self, fh):
        self._fh = fh

    def close(self) -> None:
        self._fh.close()  # closing the handle releases the lock


def _acquire_sqlite(name: str):
    from pathlib import Path

    from app import config

    path = Path(config.DATA_DIR) / f".{name}.leader.lock"
    fh = open(path, "a+b")  # noqa: SIM115 - held open for the life of the leadership
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return _FileLock(fh)


def check(name: str) -> bool:
    """True while this process still holds `name` (Postgres: its lock connection answers)."""
    with _lock:
        handle = _held.get(name)
    if handle is None:
        return False
    if isinstance(handle, _FileLock):
        return True
    try:
        handle.execute("SELECT 1").fetchone()
        return True
    except Exception:
        log.error("the %s leader lock connection failed", name, exc_info=True)
        return False


# --- S3c (ADR 0097): the boot lock and web-process presence --------------------------------
#: How long a booting process waits for the boot lock before giving up (a peer's boot that
#: takes longer than this is wedged; failing the boot lets the supervisor restart the group).
BOOT_LOCK_WAIT_S = 600.0


@contextlib.contextmanager
def boot_lock():
    """Serialize the one-time boot work of the processes of one install (S3c, ADR 0097).

    With N web processes (uvicorn --workers N) and M worker processes starting together, each
    runs init_db, the seeding steps and the boot cleanups. Run one at a time they are the same
    steps a single process runs; run at once they race (two first-admin seeds, two schema
    shims). Postgres: a blocking session advisory lock on a dedicated connection (released
    when the connection closes, also if the process dies). SQLite: an exclusive lock on a file
    in the data directory, polled. Used by the split roles only; the single `all` process takes
    no lock (byte-identical boot)."""
    from app import config

    handle = _boot_lock_sqlite() if config.IS_SQLITE else _boot_lock_pg()
    try:
        yield
    finally:
        try:
            handle.close()
        except Exception:
            log.debug("releasing the boot lock failed", exc_info=True)


def _boot_lock_pg():
    from app.services import events

    conn = events._pg_listen_connection()
    try:
        conn.execute(f"SET statement_timeout = {int(BOOT_LOCK_WAIT_S * 1000)}")
        conn.execute("SELECT pg_advisory_lock(%s)", (_key("boot"),))
    except Exception:
        conn.close()
        raise
    return conn


def _boot_lock_sqlite():
    import time

    deadline = time.monotonic() + BOOT_LOCK_WAIT_S
    while True:
        handle = _acquire_sqlite("boot")
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for another process's boot (boot lock)")
        time.sleep(0.05)


#: How long a cross-process critical section waits for a peer before failing loudly.
MUTEX_WAIT_S = 30.0


@contextlib.contextmanager
def mutex(name: str):
    """A cross-process critical section for a check-then-insert (S3c gate MED-1, ADR 0097).

    In the split roles several processes can run the same dedupe or idempotency read and then
    insert (two web processes, or a web process and a worker), so an in-process lock alone no
    longer makes the pair atomic. Postgres: a session advisory lock taken and released on one
    pooled connection (the server drops it if the process dies). SQLite: an exclusive lock on
    a file in the data directory, polled. In the single `all` process this is a no-op, so the
    in-process lock the caller already holds stays the whole story there (byte-identical)."""
    from app import config

    if config.ROLE == "all":
        yield
        return
    if config.IS_SQLITE:
        import time

        deadline = time.monotonic() + MUTEX_WAIT_S
        safe = "".join(c if c.isalnum() else "_" for c in name)[:80]
        while (handle := _acquire_sqlite(f"mutex-{safe}")) is None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for the {name} lock held by a peer")
            time.sleep(0.02)
        try:
            yield
        finally:
            handle.close()
        return
    from sqlalchemy import text

    from app.db import engine

    key = _key(f"mutex:{name}")
    with engine.connect() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{int(MUTEX_WAIT_S)}s'"))
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        conn.commit()
        try:
            yield
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
            except Exception:
                # A connection that could not unlock must not go back to the pool holding it.
                log.warning("releasing the %s lock failed; dropping its connection", name)
                conn.invalidate()


def _presence_name(instance: str) -> str:
    # The instance id is "host:pid:hex"; a file name must not carry ":" on Windows.
    return "web-" + "".join(c if c.isalnum() else "_" for c in instance)


#: How many times a booting web process tries to take its presence lock before failing.
PRESENCE_TRIES = 5


def hold_presence(instance: str, *, watch: bool = True) -> None:
    """Mark the web process `instance` (jobs.instance_id()) as alive for as long as it runs:
    a lock only this process holds. Peers ask `present()` before they fail a row it owns.

    Gate MED-3: the boot fails when the lock cannot be taken (a process that is not visibly
    present would have its live rows failed by a peer's boot), and a watcher re-checks it
    every RETRY_S: if the Postgres lock connection dropped (a restart, a failover, an idle
    timeout) it takes the lock again, retrying until it can (it never exits the process)."""
    import time

    name = _presence_name(instance)
    for attempt in range(PRESENCE_TRIES):
        if try_acquire(name):
            break
        time.sleep(min(2.0, 0.2 * (attempt + 1)))
    else:
        raise RuntimeError(f"could not take the presence lock of web process {instance}")
    if watch:
        threading.Thread(
            target=_watch_presence, args=(name,), name="loc-presence", daemon=True
        ).start()


def _watch_presence(name: str) -> None:
    """Keep the presence lock held (gate round 2 MED-A). If its Postgres connection drops (a
    restart, a failover, a half-open socket) the lock is released and taken again; until that
    works the process keeps serving and retries every RETRY_S. It never exits: the presence
    name is unique to this process, so no peer can take it over, and while the database is
    unreachable no peer can boot and fail this process's rows anyway."""
    lost_since = None
    while True:
        threading.Event().wait(RETRY_S)
        if check(name):
            continue
        release(name)
        if try_acquire(name):
            log.warning("the %s presence lock was lost and taken again", name)
            lost_since = None
            continue
        if lost_since is None:
            lost_since = time.monotonic()
        log.error(
            "the %s presence lock is not held (%.0f s); a peer's boot may treat this process's "
            "live rows as orphaned until it is taken again; retrying",
            name,
            time.monotonic() - lost_since,
        )


def sweep_presence_files() -> int:
    """SQLite: delete presence lock files of web processes that are gone (gate LOW-6); each
    boot leaves one. Runs under the boot lock. A file whose lock is held is left alone."""
    from pathlib import Path

    from app import config

    if not config.IS_SQLITE:
        return 0
    n = 0
    for path in Path(config.DATA_DIR).glob(".web-*.leader.lock"):
        name = path.name[1 : -len(".leader.lock")]
        if is_leader(name):
            continue
        handle = _acquire_sqlite(name)
        if handle is None:
            continue
        handle.close()
        with contextlib.suppress(OSError):
            path.unlink()
            n += 1
    return n


def present(instance: str) -> bool:
    """True when the web process `instance` is alive (it holds its presence lock). A process
    that never took one (an old build, a crash before boot finished) reads as gone."""
    from app import config

    name = _presence_name(instance)
    if is_leader(name):
        return True
    if config.IS_SQLITE:
        from pathlib import Path

        path = Path(config.DATA_DIR) / f".{name}.leader.lock"
        if not path.exists():
            return False
        handle = _acquire_sqlite(name)
        if handle is None:
            return True
        handle.close()
        with contextlib.suppress(OSError):
            path.unlink()
        return False
    from app.services import events

    conn = events._pg_listen_connection()
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_key(name),)).fetchone()[0]
        if got:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_key(name),))
        return not got
    finally:
        conn.close()


def run_when_leader(name: str, start, *, stop: threading.Event | None = None) -> threading.Thread:
    """Start a daemon thread that waits for the leadership `name`, calls `start()` once it
    holds it, then watches the lock: if the lock is lost the process exits (code 3), since a
    peer may already lead. Returns the thread."""

    def _loop():
        while not (stop and stop.is_set()):
            if try_acquire(name):
                break
            (stop or threading.Event()).wait(RETRY_S)
        else:
            return
        try:
            start()
        except Exception:
            log.exception("starting the %s leader work failed", name)
        while not (stop and stop.is_set()):
            (stop or threading.Event()).wait(RETRY_S)
            if not check(name):
                log.critical("lost the %s leadership; exiting so no two leaders run", name)
                os._exit(3)

    th = threading.Thread(target=_loop, name=f"loc-leader-{name}", daemon=True)
    th.start()
    return th
