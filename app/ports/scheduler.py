"""TimeDriver port -- names the passage-of-time seam (composable-core phase 1, Train 2).

This module NAMES the seam Localization OS uses to DRIVE TIME: the two in-process daemons
``app.services.scheduler`` (the 30s connector/lifecycle tick) and
``app.services.retention`` (the hourly retention sweep). It is a thin FACADE over those two
services -- WRAP, DON'T MOVE: neither daemon module is touched; this module re-exposes
their ``start`` / ``tick_now`` / ``purge`` surface behind a
single ``TimeDriver`` interface, and ``app.main``'s startup routes the daemon start through
``get()`` instead of calling the two ``start()`` functions inline.

THE INTERFACE NAMES BOTH OPERATIONS (spec H6)
---------------------------------------------
``TimeDriver`` names ``tick()`` AND ``sweep()`` as SEPARATE operations, deliberately, so an
alternate driver cannot silently drop retention:

  * ``start()`` -- begin driving (start the daemons; a no-op for an external cron).
  * ``tick()``  -- the FAST connector/lifecycle pass (30s cadence): auto-close, vendor
                   expiry, orphan reaper, tap-TTL sweep, and due connector schedules.
  * ``sweep()`` -- the RETENTION pass (hourly cadence): aged uploads / Document / Segment /
                   terminal-Job purge. NAMED separately, never hidden inside ``tick()``.

``scheduler.tick()`` (30s) and ``retention`` (hourly) are two operations at two cadences;
``scheduler.tick_now()`` runs ONLY the former, and retention is reachable solely via its own
daemon + ``POST /api/admin/retention/purge``. A port that exposed only ``start()``+``tick()``
would let a faithful alternate driver silently never reap aged uploads / Documents /
Segments / Jobs -- a GDPR-relevant blob leak. So ``sweep()`` maps to the retention pass, and
retention is NOT folded into ``tick()``: ``POST /api/admin/tick`` is available in ALL modes,
and folding retention into it would change default behavior (the tick would suddenly reap on
every manual fire). The default install therefore keeps the two cadences exactly as today.

DEFAULT ADAPTER SELECTION -- byte-identical to the old startup block
--------------------------------------------------------------------
The default adapter is chosen ONCE at import, from ``config.IS_EMBEDDED`` (a boot-frozen
constant), mirroring the ``if not config.IS_EMBEDDED:`` guard the startup block used before
this train:

  * NON-embedded  -> ``InProcessTimeDriver``: ``start()`` starts BOTH daemons, exactly the
    two calls ``app.main._startup`` made inline (``scheduler.start(); retention.start()``).
  * embedded      -> ``ExternalTickDriver``:  ``start()`` is a no-op; an external cron drives
    the ``POST /api/admin/tick`` + ``POST /api/admin/retention/purge`` pair (already built in
    phase 0a). No in-process daemon threads -- the phase-0 embedded assertion stays green.

Both drivers map ``tick()`` -> ``scheduler.tick_now(now)`` (the NON-blocking,
``MaintenancePass``-serialized manual entry, sharing bare ``scheduler.tick``'s guard.
The double-fire guard on vendor NETWORK sweeps is preserved; a concurrent tick answers busy) and
``sweep()`` -> ``retention.purge(now)``. The two daemon ``_loop``s are NOT merged (that would
be churn for no invariant gain); the port names the start/tick/sweep surface over the
existing daemons.

CALL-TIME LOOKUP (test-patchability)
------------------------------------
The driver methods reference ``scheduler.start`` / ``retention.start`` / ``scheduler.tick_now``
/ ``retention.purge`` through the imported MODULE objects at CALL time -- never captured as
bound references at import -- so a test that monkeypatches ``scheduler.start`` (as the phase-0
``verify_embedded_profile`` server-child does) sees its patch honored through the port.

THE REGISTRY BYPASS (un-closeable this train; mirrors Train 1 errata (b))
------------------------------------------------------------------------
Production code must reach the driver only through ``ports.scheduler.get()``. The RAW
registry -- ``from app import ports; ports.get("scheduler")`` -- returns the SAME driver
object, so ``ports.get("scheduler").sweep()`` reaches ``retention.purge`` WITHOUT importing
``app.services.retention``, i.e. it does not trip the retention import-contract row. That is
un-closeable this train (``app/ports/__init__.py`` is frozen), so it is DOCUMENTED here, not
fixed: the contract row guards the IMPORT edge (who imports the retention MODULE), never the
runtime call through the registry.

LOAD-BEARING SURFACE (phase-2 caution). The only PRODUCTION-load-bearing method today is
``start()`` (called by ``app.main._startup``); ``tick()`` / ``sweep()`` are exercised only
by the suite (the manual-tick / retention-purge ROUTES call ``scheduler.tick_now`` /
``retention.purge`` directly, not through this port). So phase 2 must NOT over-trust the
seam -- the structural ``TimeDriver`` isinstance check in ``verify_scheduler_port`` is what
keeps ``sweep()`` a real, NAMED operation (H6) rather than prose.

NO ``ports.get()`` AT MODULE SCOPE (frozen-file docstring rule / spec LOW-2). This module
self-registers its default as its LAST statement and resolves nothing at import. The
selection is FROZEN AT IMPORT from ``config.IS_EMBEDDED`` (L1): monkeypatching
``config.IS_EMBEDDED`` AFTER import does NOT re-select the driver -- to simulate an embedded
boot, use a subprocess (see ``verify_scheduler_port``'s arm H).

Documented in: docs/architecture/kernel-and-ports.md
"""

from typing import Protocol, runtime_checkable

from app import config, ports
from app.services import retention, scheduler

__all__ = ["ExternalTickDriver", "InProcessTimeDriver", "TimeDriver", "get"]


@runtime_checkable
class TimeDriver(Protocol):
    """The passage-of-time driver. ``tick()`` and ``sweep()`` are NAMED SEPARATELY so an
    alternate driver cannot silently drop the retention pass (spec H6).

    ``sweep()`` returns a counts dict, or ``{"busy": True}`` when another pass
    is active. Busy means no sweep ran; external cron should retry later."""

    def start(self) -> None: ...  # begin driving (no-op for an external cron)
    def tick(self, now=None) -> object: ...  # the fast connector/lifecycle pass (30s)
    def sweep(self, now=None) -> object: ...  # the retention pass (hourly) -- NAMED, not hidden


class InProcessTimeDriver:
    """The DEFAULT adapter on a non-embedded install: the two in-process daemons.

    ``start()`` starts BOTH daemons -- byte-identical to the inline
    ``scheduler.start(); retention.start()`` the startup block ran before this train (same
    functions, same order, same idempotent ``_started`` guards). Wrap, don't move."""

    def start(self) -> None:
        scheduler.start()
        retention.start()

    def tick(self, now=None):
        """The 30s connector/lifecycle pass via the non-blocking manual entry. Returns
        ``scheduler.tick_now(now)``'s ``(ran, fired)`` tuple: ``(False, None)`` when a tick
        already owns ``MaintenancePass`` (the 409/busy branch), so the double-fire guard on the
        vendor network sweeps is preserved."""
        return scheduler.tick_now(now)

    def sweep(self, now=None):
        """The hourly retention pass. Returns ``retention.purge(now)``'s counts dict
        (pending blob erasures retry independently of age-retention settings), or
        ``{"busy": True}`` on overlap. Busy is a skipped pass; retry later."""
        return retention.purge(now)


class ExternalTickDriver:
    """The formal alternate, selected under ``config.IS_EMBEDDED`` (the alternate ALREADY
    built in phase 0a -- this train only names it). ``start()`` is a NO-OP: the passage of
    time is driven by an EXTERNAL CRON hitting the pair

        POST /api/admin/tick             -> scheduler.tick_now()   (this driver's tick())
        POST /api/admin/retention/purge  -> retention.purge()      (this driver's sweep())

    CRON-PAIR REQUIREMENT: an embedded operator MUST schedule BOTH -- tick alone never reaps
    aged data (retention is deliberately not folded into tick), exactly as
    ``docs/embedded.md`` already instructs. ``tick()`` / ``sweep()`` are provided so an
    in-process caller can still drive one pass, but no daemon thread runs."""

    def start(self) -> None:
        # No in-process daemons: an external cron drives the tick + purge pair.
        return None

    def tick(self, now=None):
        return scheduler.tick_now(now)

    def sweep(self, now=None):
        """Counts on completion, or ``{"busy": True}`` for overlap; retry later."""
        return retention.purge(now)


def _select_default_driver(is_embedded: bool) -> "TimeDriver":
    """The default-driver choice as a PURE function of the embedded flag, so the selection
    logic is testable without a re-import / subprocess. Embedded -> external cron (no
    daemons); otherwise the in-process daemons -- the exact ``if not config.IS_EMBEDDED:``
    split the startup block used before this train.

    L1: the ACTUAL default is selected ONCE, at module import, from ``config.IS_EMBEDDED``
    (see the self-registration at the foot of the module). Monkeypatching
    ``config.IS_EMBEDDED`` after import does NOT re-run this selection; simulate an embedded
    boot with a subprocess (``verify_scheduler_port`` arm H)."""
    return ExternalTickDriver() if is_embedded else InProcessTimeDriver()


def get() -> "TimeDriver":
    """The active TimeDriver: an override first (tests / hot-swap via
    ``ports.register('scheduler', ...)``), else the default adapter self-registered below
    (resolved through the frozen ``app.ports`` registry). ``app.main._startup`` calls
    ``get().start()`` in place of the old inline ``scheduler.start(); retention.start()``."""
    return ports.get("scheduler")


# Self-register the default adapter as the module's LAST statement (frozen-file docstring
# rule): the choice is frozen from config.IS_EMBEDDED at import, mirroring the boot-frozen
# `if not config.IS_EMBEDDED:` guard the startup block used before this train.
ports.register_default("scheduler", _select_default_driver(config.IS_EMBEDDED))
