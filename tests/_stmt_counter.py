"""SQL statement counter for the query-budget suite and diagnostics (S4 query diet).

Test and diagnostic use only: nothing in app/ imports this. It hooks SQLAlchemy's
``before_cursor_execute`` on the shared engine and counts every statement any thread runs,
except statements run by a thread inside ``counter.paused()`` (the test's own polling).

    counter = StatementCounter(engine)
    with counter.measure() as m:
        ...                      # drive the app
    m.total, m.by_statement      # count, Counter of normalized SQL text
"""

import contextlib
import re
import threading
import traceback
from collections import Counter

from sqlalchemy import event

_WS = re.compile(r"\s+")
_IN_LIST = re.compile(r"\(\s*(?:\?|%\([^)]*\)s|:\w+)(?:\s*,\s*(?:\?|%\([^)]*\)s|:\w+))*\s*\)")
_POSTCOMPILE = re.compile(r"\(__\[POSTCOMPILE_\w+\]\)")


def normalize(sql: str) -> str:
    """One line, IN-lists folded, so the same query shape groups together."""
    s = _WS.sub(" ", sql).strip()
    s = _POSTCOMPILE.sub("(?...)", s)
    return _IN_LIST.sub("(?...)", s)


class _Measure:
    def __init__(self):
        self.total = 0
        self.by_statement = Counter()
        self.by_thread = Counter()
        self.by_site = Counter()

    def top(self, n=25):
        return self.by_statement.most_common(n)


class StatementCounter:
    def __init__(self, engine, stacks=False):
        self._engine = engine
        self._stacks = stacks
        self._local = threading.local()
        self._lock = threading.Lock()
        self._current = None
        event.listen(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        m = self._current
        if m is None or getattr(self._local, "paused", False):
            return
        key = normalize(statement)
        with self._lock:
            m.total += 1
            m.by_statement[key] += 1
            m.by_thread[threading.current_thread().name] += 1
        if self._stacks:
            frames = [
                f"{f.filename.replace(chr(92), '/').split('/app/', 1)[-1]}:{f.lineno}:{f.name}"
                for f in traceback.extract_stack()[:-1]
                if "/app/" in f.filename.replace(chr(92), "/")
            ]
            site = (
                (
                    key.split(" FROM ")[0][:60] + " FROM " + key.split(" FROM ")[1][:40]
                    if " FROM " in key
                    else key[:80]
                ),
                " < ".join(reversed(frames[-6:])),
            )
            with self._lock:
                m.by_site[site] += 1

    @contextlib.contextmanager
    def paused(self):
        """Statements run by THIS thread inside the block are not counted (test polling)."""
        prev = getattr(self._local, "paused", False)
        self._local.paused = True
        try:
            yield
        finally:
            self._local.paused = prev

    @contextlib.contextmanager
    def measure(self):
        m = _Measure()
        self._current = m
        try:
            yield m
        finally:
            self._current = None

    def close(self):
        event.remove(self._engine, "before_cursor_execute", self._on_execute)
