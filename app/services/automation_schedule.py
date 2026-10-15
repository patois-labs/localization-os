"""Pure schedule math for project automation (no database, no clock reads).

A project automation fires either at fixed UTC times of day (optionally only on some days of
the week) or every N minutes. Schedules are stored and computed in UTC only:
UTC has no daylight saving, so there is no skipped or repeated hour to get wrong. The screens
show the local time next to each UTC time; that conversion is display only and lives in the
browser (web) or is left to the reader (CLI, API).

Everything here is a pure function of its arguments so the table of cases in
tests/verify_project_automation.py can pin it.

Documented in: docs/architecture/project-automation.md
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

# Floor on how often an interval schedule may hit a live system (same as the connection-level
# schedule in scheduler.MIN_EVERY_MINUTES; duplicated here so this module stays import-free).
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_TIMES = 24

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    """A schedule the caller supplied is not valid (the message is shown to the person)."""


def parse_time(raw) -> time:
    """'9:00' / '09:00' -> time(9, 0). Only 24-hour HH:MM (UTC); anything else is refused."""
    m = _HHMM.match(str(raw or "").strip())
    if not m:
        raise ScheduleError(f"'{raw}' is not a time of day. Use 24-hour HH:MM in UTC, e.g. 14:30.")
    return time(int(m.group(1)), int(m.group(2)))


def normalize_times(raw) -> list[str]:
    """A list (or comma/space separated string) of times -> sorted unique 'HH:MM' strings."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)):
        raise ScheduleError("times must be a list of HH:MM values in UTC.")
    out = sorted({parse_time(t).strftime("%H:%M") for t in raw})
    if len(out) > MAX_TIMES:
        raise ScheduleError(f"At most {MAX_TIMES} times of day per schedule.")
    return out


def normalize_days(raw) -> list[int]:
    """Days of week -> sorted unique ints, Monday = 0. Accepts ints, 'mon'/'Monday' names, or a
    comma separated string. An empty value means every day (stored as [])."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)):
        raise ScheduleError("days must be a list of weekdays (Mon..Sun or 0..6).")
    out: set[int] = set()
    for d in raw:
        if isinstance(d, bool):
            raise ScheduleError(f"'{d}' is not a weekday.")
        if isinstance(d, int) or (
            isinstance(d, str) and d.strip().isascii() and d.strip().isdigit()
        ):
            n = int(d)
            if not 0 <= n <= 6:
                raise ScheduleError(f"'{d}' is not a weekday. Use Mon..Sun or 0 (Mon) to 6 (Sun).")
            out.add(n)
            continue
        key = str(d).strip().lower()[:3]
        names = [n.lower() for n in DAY_NAMES]
        if key not in names:
            raise ScheduleError(f"'{d}' is not a weekday. Use Mon..Sun or 0 (Mon) to 6 (Sun).")
        out.add(names.index(key))
    return sorted(out) if len(out) < 7 else []


def validate(times, days, interval_minutes) -> tuple[list[str], list[int], int]:
    """Normalize and cross-check. Returns (times, days, interval). Times of day and an interval
    cannot be combined; days only make sense with times. (Both empty = no schedule.)"""
    t = normalize_times(times)
    d = normalize_days(days)
    try:
        interval = int(interval_minutes or 0)
    except (TypeError, ValueError):
        raise ScheduleError("interval_minutes must be a whole number of minutes.") from None
    if isinstance(interval_minutes, bool) or interval < 0:
        raise ScheduleError("interval_minutes must be zero or more.")
    if t and interval:
        raise ScheduleError("Choose times of day OR an interval, not both.")
    if d and not t:
        raise ScheduleError("Days of the week go with times of day. Add a time or clear the days.")
    if interval:
        if interval < MIN_INTERVAL_MINUTES:
            raise ScheduleError(f"The shortest interval is {MIN_INTERVAL_MINUTES} minutes.")
        if interval > MAX_INTERVAL_MINUTES:
            raise ScheduleError("The longest interval is 7 days (10080 minutes).")
    return t, d, interval


def has_schedule(times, days, interval_minutes) -> bool:
    return bool(times) or bool(int(interval_minutes or 0))


def next_fire(times, days, interval_minutes, after: datetime) -> datetime | None:
    """The first fire strictly after `after` (naive UTC), or None when nothing is scheduled.

    Times of day: the earliest listed time on an allowed day that is later than `after`
    (looking ahead at most 8 days). Interval: `after` plus the interval."""
    t = [parse_time(x) for x in (times or [])]
    if t:
        allowed = set(days or []) or set(range(7))
        for offset in range(0, 9):
            day = (after + timedelta(days=offset)).date()
            if day.weekday() not in allowed:
                continue
            for tt in sorted(t):
                cand = datetime.combine(day, tt)
                if cand > after:
                    return cand
        return None
    interval = int(interval_minutes or 0)
    if interval > 0:
        return after + timedelta(minutes=interval)
    return None


def describe(times, days, interval_minutes) -> str:
    """A short plain-English summary, 12-hour clock, always labelled UTC."""
    if times:
        parts = []
        for x in times:
            tt = parse_time(x)
            parts.append(tt.strftime("%I:%M %p").lstrip("0"))
        when = ", ".join(parts) + " UTC"
        if days:
            when += " on " + ", ".join(DAY_NAMES[i] for i in days)
        else:
            when += " every day"
        return when
    interval = int(interval_minutes or 0)
    if interval:
        if interval % 1440 == 0:
            n = interval // 1440
            return "every day" if n == 1 else f"every {n} days"
        if interval % 60 == 0:
            n = interval // 60
            return "every hour" if n == 1 else f"every {n} hours"
        return f"every {interval} minutes"
    return "no schedule"
