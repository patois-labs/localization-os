"""Cross-process live events: publish(channel, payload) here, subscribe(channel) anywhere.

Engine redesign S3 (target T7, ADR 0110). Today's live updates (run progress over SSE) are
in-process: once work runs in a worker process or a second instance, the web process never
hears about it. This module is the bus that fixes that. It does NOT replace the durable state:
an event says "something changed, look again"; the database stays the truth, and a subscriber
that missed events (a restart, a reconnect, a full queue) re-reads state from it.

Two transports, picked by the database dialect, one contract:

* Postgres: LISTEN/NOTIFY. Every process keeps ONE dedicated listen connection (outside the
  pool) on the fixed Postgres channel `loc_tms_events` and fans notifications out to its
  in-process subscribers by app channel. A payload too big for one NOTIFY (Postgres caps it
  at 8000 bytes; NOTIFY_INLINE_MAX_BYTES leaves headroom) is stored in the `liveevent` table
  and the NOTIFY carries its id. Notifications are delivered in commit order, once.
* SQLite: every event is a `liveevent` row. Each process runs one poller thread (only while it
  has subscribers) that reads new rows with an id cursor every POLL_INTERVAL_S (at most every
  500 ms; a publish in the same process wakes it at once). SQLite has one writer, so ids
  become visible in order; `sqlite_autoincrement` keeps them increasing after a sweep.
  Rows older than RETENTION_S are swept.

Contract, both transports: a subscription sees the events published after subscribe()
returned, per channel in publish order, each once. Payloads are JSON-serializable values
(datetimes and other non-JSON values become strings). publish() after your commit, or pass
`session=` to publish inside your own transaction (the event then goes out only if and when
that transaction commits; on Postgres that is NOTIFY's own semantics).

SSE INTEGRATION POINT (S3 proper rewires the streams with the worker split; nothing is
rewired yet). A stream handler does:

    with events.subscribe(events.run_channel(run_id)) as sub:
        while not disconnected:
            for payload in sub.poll(timeout=15):   # [] on timeout: send a keep-alive
                yield sse_frame(payload)
            # and re-read the run from the DB on each wake-up for anything missed

and the worker publishes `events.publish(events.run_channel(run_id), {...})` after each
committed step transition. Channel names are short ASCII strings; run_channel() and
project_channel() fix the convention.

Documented in: docs/architecture/data-and-migrations.md
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta

log = logging.getLogger("loc_tms.events")

#: The single Postgres LISTEN/NOTIFY channel; app channels ride inside the payload.
PG_CHANNEL = "loc_tms_events"
#: Largest NOTIFY payload sent inline (Postgres refuses above 8000 bytes).
NOTIFY_INLINE_MAX_BYTES = 7000
#: SQLite poll interval while a process has subscribers (the contract says at most 500 ms).
POLL_INTERVAL_S = 0.25
#: Rows in `liveevent` older than this are deleted by the retention sweep.
RETENTION_S = 600
#: How often one process runs the retention sweep (on publish or in its poller).
SWEEP_EVERY_S = 60
#: Events buffered per subscription; past this the OLDEST are dropped and counted.
QUEUE_MAX = 10000
#: Rows the SQLite poller reads per query.
POLL_BATCH = 1000
#: How long subscribe() waits for the Postgres LISTEN to be in place.
LISTEN_READY_TIMEOUT_S = 10


def run_channel(run_id: int) -> str:
    """The channel for one workflow run's live progress."""
    return f"run:{int(run_id)}"


def project_channel(project_id: int) -> str:
    """The channel for one project's live changes."""
    return f"project:{int(project_id)}"


def _db():
    from app import db  # local: app.db opens the engine at import; keep this module light

    return db


def _is_pg(bind=None) -> bool:
    return (bind or _db().engine).dialect.name == "postgresql"


def _table():
    from app.models import LiveEvent

    return LiveEvent.__table__


def _dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------------------------
_sweep_lock = threading.Lock()
_last_sweep = {"at": 0.0}


#: Longest channel name accepted (keeps the id-only NOTIFY envelope far under 8000 bytes).
CHANNEL_MAX_LEN = 200


def _check_channel(channel, what: str) -> None:
    if not isinstance(channel, str) or not channel or len(channel) > CHANNEL_MAX_LEN:
        raise ValueError(f"events.{what} needs a channel name of 1 to {CHANNEL_MAX_LEN} characters")


def publish(channel: str, payload, *, session=None) -> None:
    """Publish `payload` on `channel` to every subscriber in every process.

    Without `session` the event is written in its own short transaction (retried on a
    Postgres conflict) and is visible when this returns. With `session` it joins that
    session's transaction and goes out only when the caller commits.

    SQLite hazard: never call it WITHOUT `session` while this thread holds an open write
    transaction on another connection (an un-committed Session that already flushed). The
    publish then waits on your own writer lock for the busy timeout and fails. Pass that
    session instead, or publish after the commit."""
    _check_channel(channel, "publish")
    data = _dumps(payload)

    def write(cx) -> None:
        tbl = _table()
        if _is_pg(cx):
            from sqlalchemy import func, select

            envelope = _dumps({"c": channel, "p": payload})
            if len(envelope.encode("utf-8")) > NOTIFY_INLINE_MAX_BYTES:
                rid = cx.execute(
                    tbl.insert()
                    .values(channel=channel, payload=data, created_at=_now())
                    .returning(tbl.c.id)
                ).scalar_one()
                envelope = _dumps({"c": channel, "r": rid})
            cx.execute(select(func.pg_notify(PG_CHANNEL, envelope)))
        else:
            cx.execute(tbl.insert().values(channel=channel, payload=data, created_at=_now()))

    if session is not None:
        # Joins the caller's transaction. No sweep here: on SQLite the caller now holds the
        # writer lock, and a sweep on another connection would wait on it (gate HIGH-1).
        write(session.connection())
        _hub_wake()
        return
    db = _db()

    def tx() -> None:
        with db.engine.begin() as cx:
            write(cx)

    # sqlite_locks=False: SQLite already waited out its busy timeout inside the attempt, so a
    # retry would only multiply the wait (gate MED-3). Postgres conflicts are retried.
    db.retry_conflicts(tx, "events.publish", sqlite_locks=False)
    _hub_wake()
    _maybe_sweep()


def _now() -> datetime:
    return datetime.utcnow()


def _maybe_sweep(force: bool = False) -> int:
    """Delete `liveevent` rows older than RETENTION_S, at most once per SWEEP_EVERY_S per
    process. Best-effort and NON-BLOCKING: on SQLite the sweep refuses to wait for the
    writer lock (busy timeout 0 for this one statement) and on Postgres it waits at most
    SWEEP_LOCK_WAIT_MS, so a busy writer never stalls a publish or the poller; a skipped
    sweep simply runs next time."""
    now = time.monotonic()
    if not force and now - _last_sweep["at"] < SWEEP_EVERY_S:
        return 0
    if not _sweep_lock.acquire(blocking=False):
        return 0
    try:
        _last_sweep["at"] = now
        tbl = _table()
        cutoff = _now() - timedelta(seconds=RETENTION_S)
        db = _db()
        with db.engine.connect() as cx:
            pg = _is_pg(cx)
            try:
                if pg:  # SET LOCAL ends with this transaction: nothing to restore
                    cx.exec_driver_sql(f"SET LOCAL lock_timeout = {int(SWEEP_LOCK_WAIT_MS)}")
                else:
                    cx.exec_driver_sql("PRAGMA busy_timeout = 0")
                n = cx.execute(tbl.delete().where(tbl.c.created_at < cutoff)).rowcount or 0
                cx.commit()
                return n
            finally:
                if not pg:
                    try:
                        cx.rollback()
                        cx.exec_driver_sql(f"PRAGMA busy_timeout = {int(db.BUSY_TIMEOUT_MS)}")
                    except Exception:
                        # Never hand a connection with no busy timeout back to the pool.
                        cx.invalidate()
    except Exception:
        log.debug("events: retention sweep skipped", exc_info=True)
        return 0
    finally:
        _sweep_lock.release()


#: Longest a Postgres retention sweep waits for a row lock before giving up this round.
SWEEP_LOCK_WAIT_MS = 100


# --------------------------------------------------------------------------------------------
# subscribe
# --------------------------------------------------------------------------------------------
class Subscription:
    """An iterator of the payloads published on one channel after it was created.

    `for payload in sub:` blocks until the next event; `sub.poll(timeout)` returns the events
    that arrived within `timeout` seconds ([] if none), for loops that also need keep-alives
    or a disconnect check. `dropped` counts events lost to a full buffer (re-read the DB if it
    moves). close() (or leaving a `with` block) ends it; iteration then stops."""

    def __init__(self, hub: _Hub, channel: str, start_id: int):
        self.channel = channel
        self.start_id = start_id
        self.dropped = 0
        self.closed = False
        self._hub = hub
        self._buf: deque = deque()
        self._cond = threading.Condition()

    def _put(self, payload) -> None:
        with self._cond:
            if len(self._buf) >= QUEUE_MAX:
                self._buf.popleft()
                self.dropped += 1
            self._buf.append(payload)
            self._cond.notify_all()

    def poll(self, timeout: float | None = None) -> list:
        """Every buffered event, waiting up to `timeout` seconds (None: forever) for one."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._cond:
            while not self._buf and not self.closed:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                self._cond.wait(left if left is not None else 1.0)
            out = list(self._buf)
            self._buf.clear()
            return out

    def __iter__(self):
        return self

    def __next__(self):
        with self._cond:
            while not self._buf:
                if self.closed:
                    raise StopIteration
                self._cond.wait(1.0)
            return self._buf.popleft()

    def close(self) -> None:
        if self.closed:
            return
        self._hub.remove(self)
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def subscribe(channel: str) -> Subscription:
    """Subscribe to `channel` in this process. Events published after this returns are
    delivered, in order, once each. Close the subscription when done."""
    _check_channel(channel, "subscribe")
    return _get_hub().add(channel)


class _Hub:
    """The per-process fan-out: one pump thread (Postgres listener or SQLite poller) feeding
    every in-process subscription by channel."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.pg = _is_pg()
        if self.pg and _db().engine.dialect.driver != "psycopg":
            # The listener speaks psycopg 3 (the driver this build ships); fail fast and say so
            # instead of retrying an ImportError forever (gate LOW-4).
            raise RuntimeError(
                "events: live events on Postgres need the psycopg 3 driver; use a "
                "'postgresql+psycopg://' DATABASE_URL (pip install -r requirements-postgres.txt)"
            )
        self.lock = threading.Lock()
        self.subs: dict[str, set[Subscription]] = {}
        self.wake = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        self.cursor: int | None = None  # SQLite: last liveevent id dispatched

    # -- registry -------------------------------------------------------------------------
    def add(self, channel: str) -> Subscription:
        self._ensure_thread()
        start_id = 0
        if self.pg:
            if not self.ready.wait(LISTEN_READY_TIMEOUT_S):
                raise RuntimeError("events: the Postgres LISTEN connection is not up")
        else:
            start_id = _max_id()
        sub = Subscription(self, channel, start_id)
        with self.lock:
            if not self.pg and not any(self.subs.values()):
                self.cursor = start_id  # first subscriber: start reading from here
            self.subs.setdefault(channel, set()).add(sub)
        self.wake.set()
        return sub

    def remove(self, sub: Subscription) -> None:
        with self.lock:
            group = self.subs.get(sub.channel)
            if group is not None:
                group.discard(sub)
                if not group:
                    del self.subs[sub.channel]

    def _targets(self, channel: str) -> list[Subscription]:
        with self.lock:
            return list(self.subs.get(channel, ()))

    def _ensure_thread(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            target = self._listen_loop if self.pg else self._poll_loop
            self.thread = threading.Thread(target=target, name="loc-events", daemon=True)
            self.thread.start()

    # -- SQLite ---------------------------------------------------------------------------
    def _poll_loop(self) -> None:
        self.ready.set()
        while True:
            with self.lock:
                active = any(self.subs.values())
            if not active:
                self.wake.wait(5.0)
                self.wake.clear()
                continue
            try:
                self._poll_once()
            except Exception:
                log.warning("events: poll failed; retrying", exc_info=True)
                time.sleep(1.0)
            _maybe_sweep()
            self.wake.wait(POLL_INTERVAL_S)
            self.wake.clear()

    def _poll_once(self) -> None:
        from sqlalchemy import select

        tbl = _table()
        while True:
            with self.lock:
                cursor = self.cursor or 0
            with _db().engine.connect() as cx:
                rows = cx.execute(
                    select(tbl.c.id, tbl.c.channel, tbl.c.payload)
                    .where(tbl.c.id > cursor)
                    .order_by(tbl.c.id)
                    .limit(POLL_BATCH)
                ).all()
            for rid, channel, payload in rows:
                targets = [s for s in self._targets(channel) if rid > s.start_id]
                if targets:
                    value = _loads(payload)
                    for sub in targets:
                        sub._put(value)
            if rows:
                with self.lock:
                    self.cursor = max(self.cursor or 0, rows[-1][0])
            if len(rows) < POLL_BATCH:
                return

    # -- Postgres -------------------------------------------------------------------------
    def _listen_loop(self) -> None:
        backoff = 0.5
        while True:
            conn = None
            try:
                conn = _pg_listen_connection()
                conn.execute(f"LISTEN {PG_CHANNEL}")
                self.ready.set()
                backoff = 0.5
                while True:
                    for note in conn.notifies(timeout=5.0):
                        self._dispatch_pg(note.payload)
                    # Liveness probe each idle round: a half-open connection (NAT or load
                    # balancer idle drop, failover) raises here and reconnects (gate LOW-5).
                    conn.execute("SELECT 1")
            except Exception:
                self.ready.clear()
                log.warning(
                    "events: Postgres listen connection lost; reconnecting in %.1fs "
                    "(events published meanwhile are not replayed)",
                    backoff,
                    exc_info=True,
                )
                time.sleep(backoff)
                backoff = min(10.0, backoff * 2)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        log.debug("events: closing the listen connection failed", exc_info=True)

    def _dispatch_pg(self, raw: str) -> None:
        try:
            env = json.loads(raw)
            channel = env["c"]
        except (ValueError, KeyError, TypeError):
            log.warning("events: ignored a malformed notification")
            return
        targets = self._targets(channel)
        if not targets:
            return
        if "r" in env:
            value = _fetch_stored(int(env["r"]))
            if value is _MISSING:
                log.warning("events: stored event %s is gone (swept?); skipped", env["r"])
                return
        else:
            value = env.get("p")
        for sub in targets:
            sub._put(value)


_MISSING = object()


def _loads(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _max_id() -> int:
    from sqlalchemy import func, select

    tbl = _table()
    with _db().engine.connect() as cx:
        return int(cx.execute(select(func.max(tbl.c.id))).scalar() or 0)


def _fetch_stored(rid: int):
    """Read an oversized payload. Uses the pool (not the listen connection, which is busy
    inside notifies()), so a starved pool delays the listener; order is still kept."""
    from sqlalchemy import select

    tbl = _table()
    with _db().engine.connect() as cx:
        row = cx.execute(select(tbl.c.payload).where(tbl.c.id == rid)).first()
    return _MISSING if row is None else _loads(row[0])


def _pg_listen_connection():
    """A dedicated autocommit psycopg connection (never a pooled one: LISTEN pins it)."""
    import psycopg

    url = _db().engine.url.set(drivername="postgresql")
    return psycopg.connect(
        url.render_as_string(hide_password=False),
        autocommit=True,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )


_hub_lock = threading.Lock()
_hub: _Hub | None = None


def _get_hub() -> _Hub:
    """This process's hub (a forked child builds its own: threads do not survive fork)."""
    global _hub
    with _hub_lock:
        if _hub is None or _hub.pid != os.getpid():
            _hub = _Hub()
        return _hub


def _hub_wake() -> None:
    hub = _hub
    if hub is not None and hub.pid == os.getpid():
        hub.wake.set()


def _reset_after_fork() -> None:
    """A forked child starts with fresh locks and no hub: a lock held by another thread at
    fork time would otherwise stay held forever in the child (gate LOW-8)."""
    global _hub, _hub_lock, _sweep_lock
    _hub = None
    _hub_lock = threading.Lock()
    _sweep_lock = threading.Lock()
    _last_sweep["at"] = 0.0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
