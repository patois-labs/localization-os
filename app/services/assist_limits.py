"""Shared, durable Ask/Draft call allowance.

Documented in: docs/architecture/assistant.md
"""

import hashlib
import hmac
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from app.db import engine
from app.models import AssistAllowance
from app.services import settings

PER_MINUTE = 6
PER_DAY = 60
_lock = threading.Lock()


def actor_hash(actor: str, *, host: bool = False) -> str:
    """Install-keyed, domain-separated identity; the host has its own bucket."""
    from app import config

    label = "host:" if host else "user:"
    payload = ("assist-allowance:" + label + actor).encode()
    return hmac.new(config.SESSION_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def delete_expired(session: Session, now: datetime) -> None:
    """Two-day-old UTC buckets cannot affect any active allowance window."""
    from sqlalchemy import delete

    cutoff = (now - timedelta(days=2)).date().isoformat()
    session.exec(delete(AssistAllowance).where(AssistAllowance.day < cutoff))


def reserve() -> None:
    """Consume before paid egress, including failed calls and JSON repair calls.

    Middleware supplies the authenticated actor for every request. Host-local
    CLI uses the shared system allowance. Never accept an identity from a form.
    SQLite's insert takes the write lock; Postgres also locks the selected row.
    Counts therefore survive restarts and cannot race across worker processes.
    """
    from app.services.assist_settings_keys_registered import AssistError
    from app.services.egress import CALLER_ACTOR

    actor = CALLER_ACTOR.get()
    actor = actor() if callable(actor) else actor
    actor = str(actor or "system")
    key = actor_hash(actor, host=actor == "system" and not settings.REQUEST_DOOR.get())
    per_minute = int(settings.get("assist_calls_per_minute", str(PER_MINUTE)))
    per_day = int(settings.get("assist_calls_per_day", str(PER_DAY)))
    now = time.time()
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    with _lock, Session(engine) as session:
        insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
        session.exec(
            insert(AssistAllowance)
            .values(
                actor_hash=key,
                day="",
                daily_calls=0,
                minute_started=0.0,
                minute_calls=0,
            )
            .on_conflict_do_nothing(index_elements=["actor_hash"])
        )
        row = session.exec(
            select(AssistAllowance).where(AssistAllowance.actor_hash == key).with_for_update()
        ).one()
        if row.day != day:
            row.day, row.daily_calls = day, 0
        if now - row.minute_started >= 60:
            row.minute_started, row.minute_calls = now, 0
        if per_day and row.daily_calls >= per_day:
            raise AssistError(
                "Ask and Draft daily limit reached. Try again tomorrow (UTC).", status=429
            )
        if per_minute and row.minute_calls >= per_minute:
            raise AssistError(
                "Ask and Draft rate limit reached. Wait one minute and retry.", status=429
            )
        row.daily_calls += 1
        row.minute_calls += 1
        session.add(row)
        session.commit()
