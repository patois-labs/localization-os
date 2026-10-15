"""Shared direct-work admission and the two modeled maintenance parents.

Documented in: docs/architecture/configuration.md
"""

import threading
from contextlib import contextmanager
from functools import wraps

_execution = threading.local()


@contextmanager
def accounted_work():
    """Synchronous children share their parent's already modeled work allowance.

    Only bounded parents (maintenance passes and JOB_WORKERS consumers) enter
    this directly. Thread-local ownership deliberately does not propagate to
    another thread or an async task: an independent execution needs admission.
    This is lifetime bookkeeping, never a lock around the work.
    """
    previous = getattr(_execution, "accounted", False)
    _execution.accounted = True
    try:
        yield
    finally:
        _execution.accounted = previous


def admitted_work(fn=None, *, operation="launch"):
    """Reserve one shared run slot before direct work, including any row creation.

    Nested synchronous helpers share the reservation until the outermost call
    finishes. A worker-thread invocation acquires and releases in that thread;
    no reservation is released merely because its async waiter is cancelled.
    """

    if fn is None:
        return lambda target: admitted_work(target, operation=operation)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        if getattr(_execution, "accounted", False):
            return fn(*args, **kwargs)
        from app import config
        from app.services.orchestrator import RUN_SLOTS, RunCapacityError

        token, observed = RUN_SLOTS.acquire(count_refusal=False)
        if token is None:
            raise RunCapacityError(observed, config.MAX_CONCURRENT_RUNS, operation=operation)
        try:
            with accounted_work():
                return fn(*args, **kwargs)
        finally:
            token.release(wake=True)

    return wrapped


class MaintenancePass:
    """One active pass per owner; overlapping callers return a visible busy result."""

    def __init__(self, busy):
        self.lock = threading.Lock()
        self._active = False
        self._busy = busy

    def __call__(self, fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not self.lock.acquire(blocking=False):
                return self._busy()
            try:
                if self._active:
                    return self._busy()
                self._active = True
            finally:
                self.lock.release()
            try:
                with accounted_work():
                    return fn(*args, **kwargs)
            finally:
                with self.lock:
                    self._active = False

        return wrapped
