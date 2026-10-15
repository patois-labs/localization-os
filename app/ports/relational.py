"""RelationalStore port -- names the relational-datastore seam (composable-core phase 1,
Train 5).

This module NAMES the seam Localization OS uses to reach its relational store: the SQLAlchemy
engine + session helpers in ``app.db``. It is a thin FACADE over ``app.db`` --
WRAP, DON'T MOVE: ``app.db`` is untouched; its module-scope side effects (engine
construction at ``db.py:30``, the SQLite WAL/busy-timeout PRAGMA listener, the key-change
feed registration, ``init_db``'s three schema sub-paths) and its 123 legitimate
``sqlmodel``/``sqlalchemy`` importers all stay EXACTLY as they are. This module re-exposes
``engine`` / ``get_session`` / ``commit_with_retry`` / ``init_db`` behind a single
``RelationalStore`` interface for NEW code and for the documented boundary.

A LIGHT NAMING PASS, NOT A DATA-ACCESS REWRITE (section-3 discrepancy 5)
-----------------------------------------------------------------------
RelationalStore is ALREADY half-ported: ``config.DB_URL`` (``config.py:85``, the opt-in
``DATABASE_URL``) + SQLAlchemy already abstract SQLite vs Postgres, and CI smokes Postgres.
So phase 1 names the CONSTRUCTION boundary and does NOT route the pervasive
``with Session(engine) as s:`` sites through the port -- that pervasive-session cut is
phase 4's ``loc-core`` work. New code references ``ports.relational.get()`` (or the
module-level re-exports); the existing sites keep importing ``sqlmodel``/``app.db``
unchanged.

NO IMPORT-CONTRACT ROW -- THE create_engine BOUNDARY IS CALL-LEVEL, NOT AN IMPORT EDGE
--------------------------------------------------------------------------------------
The aspirational rule has the same SHAPE as the other ports ("only ``app.ports.relational``
may construct the engine"), but it is NOT enforceable in phase 1 by an import edge:

  * 123 modules under ``app/`` import ``sqlmodel``/``sqlalchemy`` LEGITIMATELY (``Session``,
    ``select``, ``event``, ``text``); an import-edge contract on ``app.db`` (or on
    ``sqlmodel``) would allowlist nearly every module and be VACUOUS -- the same reason the
    spec gives no row to MTEngine (name-level factory boundary) and Train 2 gives none to the
    ``scheduler`` MODULE (6 broad-API importers). A row over ``app.db`` would allowlist the
    123 importers and prove nothing.
  * ``create_engine`` is imported from ``sqlmodel`` (NOT ``sqlalchemy``) at ``db.py:12``, and
    the real guarantee -- "the engine is CONSTRUCTED only in ``db.py``" -- is a CALL-LEVEL
    property (``create_engine(config.DB_URL, ...)`` appears only at ``db.py:30``), which needs
    a call-level check that is deliberately NOT built in phase 1.

So Train 5 states the boundary as CONVENTION and records the deferred call-level check;
it adds NO ``Contract`` row to ``tests/_import_contracts.py`` (a rationale comment is
appended there for the record). Scope note for a FUTURE call-level check: it must EXCLUDE
by path both ``alembic/env.py`` (uses ``engine_from_config``, not ``create_engine``) and
``tools/vendor_studio/vstudio/db.py`` (a SEPARATE app with its OWN ``create_engine``) -- both
are outside this port's contract scope.

THE REGISTRY BYPASS (un-closeable this train; mirrors Train 1/2 errata (b))
--------------------------------------------------------------------------
Production/new code must reach the store only through ``ports.relational.get()``. The RAW
registry -- ``from app import ports; ports.get("relational")`` -- returns the SAME default
adapter object, so it reaches ``db.engine`` / ``db.get_session`` / ``db.commit_with_retry``
without importing ``app.db`` at the call site. That is un-closeable this train
(``app/ports/__init__.py`` is frozen), so it is DOCUMENTED here, not fixed. It is also mostly
moot for this port: there is no import-contract row to bypass (see above), and the port does
NOT gate anything (unlike telemetry's redaction law) -- ``get()`` returns the default adapter
directly, no wrapper.

DIALECT / RESILIENCE PROPERTIES THAT STAY IN ``app.db`` (documentation, not refactors)
--------------------------------------------------------------------------------------
Naming the port must not leak SQLite-specific behavior into the port's general contract.
These stay inside ``app.db`` behind its dialect guard and are recorded here so a future
NON-SQL / alternate adapter reviewer sees the sharp edges:

  * JSON COLUMNS (``Document.skeleton``, ``Document.context``, ``Connection.config``,
    ``WorkflowRun.context``, ...) are stored as native JSON/TEXT via the SQLModel ``JSON``
    type. A non-relational or non-JSON-capable adapter would break here -- RECORD, don't fix.
  * ALEMBIC-BEYOND-ADD-COLUMN (the 2026-07-10 lesson): ``init_db`` runs ``alembic upgrade
    head`` in-process for a fresh/stamped real install so NON-additive migrations actually
    reach the DB; the additive-only ``_ADDED_COLUMNS`` shim is the TEST + legacy-DB path.
    Leave ``init_db``'s three sub-paths intact -- the port must not "simplify" schema bring-up.
  * WRITER-LOCK LOGIC is SQLite-specific and STAYS behind ``engine.dialect.name == "sqlite"``:
    the WAL/``busy_timeout``/``foreign_keys`` PRAGMA listener (``db.py:44``) and
    ``commit_with_retry``'s jittered lock-retry (``db.py:1267``; ``is_locked_error`` matches
    SQLite lock phrases only, so Postgres MVCC never engages it). It must never leak into the
    port's general contract -- ``commit_with_retry`` is a passthrough, byte-identical to a
    plain ``session.commit()`` on the happy path.

LAZY-IMPORT INVARIANT (spec HIGH-1 family): this module module-scope-imports ``app.db``,
which CONSTRUCTS THE ENGINE at import. That is FINE here because this module is only ever
imported LAZILY (via ``ports.get("relational")``'s ``importlib`` fallback, or an explicit
``from app.ports import relational`` in a function body) -- it is NEVER imported at a
config-only module scope (e.g. ``providers.py``'s module scope, which must stay DB-free per
the TelemetrySink lazy-import invariant). Do NOT add a module-scope import of this module to
any config-only import path.

NO ``ports.get()`` AT MODULE SCOPE (frozen-file docstring rule / spec LOW-2). This module
self-registers its default as its LAST statement and resolves nothing at import.

Documented in: docs/architecture/kernel-and-ports.md
"""

from typing import Protocol, runtime_checkable

from app import db, ports

__all__ = [
    "DefaultRelationalStore",
    "RelationalStore",
    "commit_with_retry",
    "engine",
    "get",
    "get_session",
    "init_db",
]

# --------------------------------------------------------------------------------------
# Module-level re-exports of the db.py surface for NEW code + the documented boundary.
# --------------------------------------------------------------------------------------

# The singleton SQLAlchemy engine, constructed ONCE at db import (db.py:30) and never
# reassigned. An IDENTITY ALIAS (not a call-time wrapper): the engine is a constant object,
# and aliasing it -- rather than exposing a swap seam -- is the SAFER choice for this port
# (pointing the app at a fake engine mid-process is dangerous and deliberately not offered;
# tests prove the resolution SEAM with a recording adapter, never a live engine swap).
# IMPERATIVE RULE (L4): this correctness rests on db.engine NEVER being rebuilt. If any future
# code reassigns db.engine at runtime, this module-level alias goes stale (it still points at
# the OLD engine); convert it to a module `__getattr__` returning db.engine at that point.
engine = db.engine


def get_session():
    """Re-export of ``db.get_session`` (call-time): a generator that yields ONE session per
    call, used as the FastAPI request dependency. Call-time (not a frozen import alias) so a
    test/hot-swap monkeypatch of ``db.get_session`` is honored through the port. Same
    semantics as ``db.get_session`` -- each call opens ``with Session(db.engine) as s:`` and
    yields ``s``."""
    return db.get_session()


def commit_with_retry(session, **kw):
    """Passthrough to ``db.commit_with_retry`` (call-time). Byte-identical to a plain
    ``session.commit()`` on the happy path; the SQLite jittered writer-lock retry engages
    only when a commit raises a lock error (a NON-lock error / a non-SQLite backend
    propagates unchanged). ``**kw`` forwards ``stage`` / ``attempts`` / ``keep_loaded`` so the
    port never duplicates -- or drifts from -- ``db.commit_with_retry``'s own defaults."""
    return db.commit_with_retry(session, **kw)


def init_db():
    """Passthrough to ``db.init_db`` (call-time): the boot-time schema bring-up (the three
    SQLite sub-paths + the Postgres alembic-authority refuse-if-unmigrated). Re-exported for
    NEW code / an alternate adapter's own bring-up; ``app.main`` still calls ``db.init_db``
    directly this phase (no call-site migration -- section-3 discrepancy 5)."""
    return db.init_db()


# --------------------------------------------------------------------------------------
# The interface + default adapter.
# --------------------------------------------------------------------------------------


@runtime_checkable
class RelationalStore(Protocol):
    """The relational-datastore seam over today's ``app.db``. Thin by design (section-3
    discrepancy 5): the dialect is already abstracted by ``config.DB_URL`` + SQLAlchemy.

    ``session`` and ``commit_with_retry`` are NAMED so an alternate adapter cannot silently
    drop the writer-lock-safe commit path; ``engine`` is the SQLAlchemy engine (already
    dialect-abstract). ``runtime_checkable`` so ``verify_relationalstore_port`` can assert the
    surface structurally (with a negative control), not merely in prose."""

    @property
    def engine(self): ...  # the SQLAlchemy engine (already dialect-abstract)

    def session(self): ...  # == db.get_session (per-call session generator)

    def commit_with_retry(
        self, session, *, stage=None, attempts: int = db.WRITE_RETRY_ATTEMPTS, keep_loaded=False
    ) -> None: ...

    # attempts REFERENCES db.WRITE_RETRY_ATTEMPTS (=3), never restates 3: db.py:1267's real
    # default is `attempts: int = WRITE_RETRY_ATTEMPTS` and db.py:1303 does `range(attempts)`,
    # so an adapter that copied a `attempts=None` stub verbatim and forwarded attempts=attempts
    # would TypeError on `range(None)` every call. The stub carries the real default (M1).


class DefaultRelationalStore:
    """The DEFAULT adapter: ``app.db`` itself.

    ``engine`` returns ``db.engine`` (identity -- the one constant engine); ``session()``
    delegates to ``db.get_session()``; ``commit_with_retry`` passes through to
    ``db.commit_with_retry``. All CALL-TIME (through the imported ``db`` module object), so a
    test that monkeypatches ``db.get_session`` / ``db.commit_with_retry`` sees its patch
    honored through the port (the Train-2 call-time-lookup lesson). Wrap, don't move -- no
    behavior of ``app.db`` changes."""

    @property
    def engine(self):
        return db.engine

    def session(self):
        """Delegates to ``db.get_session()`` -- a GENERATOR (the FastAPI request dependency),
        NOT a context manager, despite the spec's pinned ``-> Session (context manager)``
        annotation. Drive it as a generator (do NOT write ``with store.session() as s:`` --
        that TypeErrors on a generator)::

            gen = store.session()
            s = next(gen)          # enter `with Session(db.engine) as s:`; s is the session
            try:
                ...                # use s
            finally:
                gen.close()        # exit the with-block, closing the session

        Or use it as a FastAPI dependency (``Depends(relational.get_session)``), which drives
        the generator for you."""
        return db.get_session()

    def commit_with_retry(self, session, **kw):
        return db.commit_with_retry(session, **kw)


def get() -> "RelationalStore":
    """The active RelationalStore adapter: an override first (tests / hot-swap via
    ``ports.register('relational', ...)``), else the default ``DefaultRelationalStore``
    self-registered below, resolved through the frozen ``app.ports`` registry. New code reads
    the engine with ``relational.get().engine``, opens a session generator with
    ``relational.get().session()``, and commits with
    ``relational.get().commit_with_retry(sess, ...)``. Mirrors ``blobstore.get()`` /
    ``scheduler.get()``."""
    return ports.get("relational")


# Self-register the default adapter as the module's LAST statement (frozen-file docstring
# rule): a default registered BEFORE an import failure lower in the module would survive in
# _DEFAULTS even though the failed module is dropped from sys.modules. No such import here,
# but the rule is followed uniformly.
ports.register_default("relational", DefaultRelationalStore())
