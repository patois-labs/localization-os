"""DB engine + session helpers. SQLite is the zero-config default; setting
DATABASE_URL opts into another backend (Postgres). SQLite-specific behaviour
(the WAL PRAGMA, file encryption at rest, PRAGMA-based column backfill) is guarded
behind the dialect so the Postgres path is clean and Alembic-owned.

Documented in: docs/architecture/data-and-migrations.md
"""

import contextvars
import logging
import random
import threading as _threading
import time

from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as _SATimeoutError
from sqlalchemy.orm import Session as _SAOrmSession
from sqlalchemy.pool import NullPool, QueuePool
from sqlmodel import Session, SQLModel, create_engine

from app import config, models
from app.services import dbcrypt, vault

log = logging.getLogger("loc_tms.db")

config.private_directory(config.DATA_DIR)
if config.IS_SQLITE and not dbcrypt.enabled():
    # Legacy shutdown-vault mode: if the DB was encrypted at rest on a previous
    # shutdown, restore it before use. In always-on SQLCipher mode the file is
    # never plaintext + there is no `.vault`, so this is skipped.
    vault.unlock_if_needed(config.DB_PATH)
config.prepare_sqlite_file()

# Known Postgres DBAPI driver tokens -> their import module name. Deliberately a fixed
# ALLOWLIST (LOW-1): we NEVER import an arbitrary module named by DATABASE_URL — a typo'd
# scheme could otherwise run some importable module's import side effects at boot.
_PG_DRIVER_MODULES = {
    "psycopg": "psycopg",  # psycopg3 — the driver this build ships (opt-in extra)
    "psycopg2": "psycopg2",
    "asyncpg": "asyncpg",
    "pg8000": "pg8000",
}

# Postgres driver preflight (opt-in extra): psycopg (psycopg3) is LGPL and lives in
# requirements-postgres.txt, NOT the default install. If DATABASE_URL points at Postgres
# but the DSN's driver is unknown or its module is not importable, raise a CLEAR hint HERE —
# before create_engine() -> SQLAlchemy would otherwise surface an opaque ImportError deep in
# dialect import. We only import a KNOWN, allowlisted module, and only ever raise when it is
# genuinely missing, so an install that legitimately has (say) psycopg2 for a bare
# `postgresql://` DSN is NOT broken (LOW-3). SQLite (the default) and `postgres://`
# (SQLAlchemy's own NoSuchModuleError, unchanged from main) never enter this branch.
if config.DB_URL.startswith("postgresql"):
    import importlib

    _scheme = config.DB_URL.split("://", 1)[0].strip().lower()  # e.g. "postgresql+psycopg"
    _driver = _scheme.split("+", 1)[1] if "+" in _scheme else "psycopg2"  # bare -> psycopg2
    _module = _PG_DRIVER_MODULES.get(_driver)
    if _module is None:
        raise RuntimeError(
            f"DATABASE_URL uses the '{_scheme}://' scheme with an unsupported DBAPI driver "
            f"'{_driver}'. This build ships the psycopg3 driver; use the "
            "'postgresql+psycopg://' scheme and install it: "
            "pip install -r requirements-postgres.txt"
        )
    try:
        importlib.import_module(_module)
    except ImportError as e:
        if _driver == "psycopg":
            raise RuntimeError(
                "DATABASE_URL points at Postgres but the psycopg driver is not installed: "
                "pip install -r requirements-postgres.txt"
            ) from e
        raise RuntimeError(
            f"DATABASE_URL uses the '{_scheme}://' scheme (DBAPI driver '{_driver}'), which is "
            "not installed. This build ships the psycopg3 driver; use the "
            "'postgresql+psycopg://' scheme and install it: "
            "pip install -r requirements-postgres.txt"
        ) from e


# --- the PER-REQUEST pool-wait budget (gate A round 1, HIGH-1) ----------------
# One request may spend AT MOST ONE `pool_timeout` waiting for a connection, ever.
#
# The train's first round bounded a single CHECKOUT (5s instead of SQLAlchemy's 30s) and
# stopped there. The gate measured what that leaves: one `GET /` under an exhausted pool
# performed EIGHTEEN sequential checkout timeouts -- eighteen queries, each waiting the
# full budget -- so the request pinned an anyio worker thread for ~90s and then returned a
# bodyless 500. Shortening the per-checkout wait divided the disaster by six; it did not
# remove it, and a pinned worker thread per request is precisely the accumulator that
# turned the 2026-09-06 stall into an OOM kill (GitHub #109).
#
# So the budget is scoped to the REQUEST, not to the checkout: the first timeout inside a
# request marks the budget spent, and every later checkout in that same request raises
# immediately instead of waiting again. The scope is a mutable object in a ContextVar
# because Starlette runs a sync endpoint in a COPY of the request's context: a flag would
# be invisible to the middleware that set it, an object's attribute is shared.
#
# NO SCOPE = NO BUDGET, and that is deliberate. Background run threads, the batch
# dispatcher and the job daemons start with an empty context, so they see `None` and get
# exactly the pre-existing behaviour (one honest wait per checkout). A long-lived daemon
# thread must never be able to latch itself permanently out of the database.
#
# IT IS A STALL BOUND, NOT A ONE-WAY LATCH (gate A round 2, MED-2). `spent` is cleared by
# the next SUCCESSFUL checkout, so the rule is "one wait per stall", not "one wait per
# request": once the pool recovers, the scope recovers with it. The first version never
# cleared it, and the gate measured a scope refused instantly on a completely free pool
# (60/60 available) because of one earlier blip. And the two LONG-LIVED request scopes —
# an SSE `observe_run` stream (a Session per 0.4s poll for up to 900s) and a `wait=true`
# fan-out drain — opt OUT entirely via `exempt_pool_budget()`: they are minutes long by
# design, so a request-scoped budget would kill the operator's only live progress view
# after one transient blip. They get the per-CHECKOUT `pool_timeout` instead.
class _PoolBudget:
    """One request's allowance of pool WAITING. `spent` is set by the pool when a checkout
    in this scope times out, and cleared again when one succeeds."""

    __slots__ = ("spent",)

    def __init__(self) -> None:
        self.spent = False


_pool_budget: contextvars.ContextVar = contextvars.ContextVar("loc_tms_pool_budget", default=None)


def begin_pool_budget() -> _PoolBudget:
    """Open a FRESH pool-wait budget for the current context (one HTTP request).

    Called by the doorman at the very start of every request, so the budget is
    per-request by construction: nothing carries over from the previous one."""
    budget = _PoolBudget()
    _pool_budget.set(budget)
    _relief_scope.set(False)
    return budget


def exempt_pool_budget() -> None:
    """Drop the pool-wait budget for the rest of THIS scope (MED-2).

    For handlers that legitimately live for minutes and issue hundreds of checkouts — the
    SSE run observer and the `wait=true` fan-out drain. They fall back to the plain
    per-checkout `pool_timeout`, which is the right bound for them: a stream that loses one
    poll to a transient saturation retries on the next tick instead of dying, and the
    request it belongs to is not one an operator is waiting on a page for.

    Sets the ContextVar to None rather than mutating the budget object, so an outer scope
    (the doorman's) is untouched and nothing leaks back out."""
    _pool_budget.set(None)


#: THE RELIEF SCOPE (gate A round 3, HIGH-4). Set by the capacity doorman for the small,
#: explicit set of paths that are how an operator makes a saturation STOP — cancelling a run
#: and the settings page — and ONLY when the pool is already at its ceiling when the request
#: arrives. While it is set, every seam that asks `relief_engine_or()` for its engine gets the
#: POOL-LESS one, so the request cannot wait on the pool at all.
#:
#: Round 2 exempted these paths from being SHED at the door and stopped there, which the gate
#: measured as strictly worse than shedding them: `/settings` and both cancel routes still
#: answered 503, now after 5.02s of a PINNED anyio worker thread each instead of 0.01s — an
#: operator retrying cancel (which is what an operator does) accumulating exactly the parked
#: threads the doorman exists to prevent, and that GitHub #109 says ended in the OOM kill. A
#: path exempt from the door is only useful if it can actually ANSWER, and the only state it
#: is ever exercised in is the state where no pooled connection exists.
_bookkeeping_scope = contextvars.ContextVar("loc_tms_bookkeeping", default=False)

_relief_scope: contextvars.ContextVar = contextvars.ContextVar("loc_tms_relief", default=False)


def begin_relief_scope() -> None:
    """Serve the rest of THIS request off the pool-less engine (see `_relief_scope`)."""
    _relief_scope.set(True)


def in_relief_scope() -> bool:
    return bool(_relief_scope.get())


@event.listens_for(_SAOrmSession, "after_transaction_create")
def _route_relief_transaction(session, transaction):
    """Route all ORM helpers on a relief request before their first checkout.

    Settings templates and audit helpers also create Sessions directly. Routing only
    FastAPI dependencies misses those reads. Restrict this to the application's engine;
    a Session bound to a caller's connection or another database retains its binding.
    """
    if (
        transaction.parent is None
        and (in_relief_scope() or _bookkeeping_scope.get())
        and session.bind is engine
        and not session.info.get("relief_pin")
    ):
        session.info["relief_original_bind"] = session.bind
        session.bind = relief_engine_or(engine)


@event.listens_for(_SAOrmSession, "do_orm_execute")
def _route_relief_read(state):
    # A bookkeeping Session pins its own engine once, at creation (bookkeeping.session sets
    # `relief_pin`), so its reads never switch connections mid-transaction when capacity
    # flips underneath it (gate A round 9 LOW-1 -- a widened bookkeeping scope must not turn
    # a healthy pooled session into a two-connection transaction that loses its own writes).
    # Helper Sessions opened inside a relief/bookkeeping scope are unpinned and still route.
    if (
        (in_relief_scope() or _bookkeeping_scope.get())
        and state.session.bind is engine
        and not state.session.info.get("relief_pin")
    ):
        state.bind_arguments["bind"] = relief_engine_or(engine)


@event.listens_for(_SAOrmSession, "after_transaction_end")
def _restore_relief_transaction(session, transaction):
    if transaction.parent is None and "relief_original_bind" in session.info:
        session.bind = session.info.pop("relief_original_bind")


def relief_engine_or(pooled):
    """The engine this scope must use: the bounded relief engine inside a relief scope,
    otherwise the `pooled` engine the CALLER passed.

    Takes the pooled engine as an ARGUMENT rather than reading the module global, so a caller
    whose module-level `engine` has been rebound (every suite that points the app at a
    throwaway data dir) still gets its own engine, and this function can never quietly
    redirect a test's writes to a second engine on the real DB path."""
    if _bookkeeping_scope.get():
        return _ensure_emergency_engine() if pool_has_no_capacity() else pooled
    if not _relief_scope.get():
        return pooled
    try:
        return _emergency_engine_for_relief()
    except Exception:  # pragma: no cover - relief must never be worse than the pool
        return pooled


def pool_budget_spent() -> bool:
    """True once this scope has already paid its one checkout timeout — the signal an
    error renderer uses to skip a database-backed page (app/web/deps.py::_error_page)."""
    budget = _pool_budget.get()
    return bool(budget is not None and budget.spent)


class _BudgetedQueuePool(QueuePool):
    """QueuePool that honours the per-request budget above.

    `_do_get` is the one seam where a caller WAITS, so it is the one place a total-wait
    bound can live; SQLAlchemy has no public hook before the wait. Pinned by
    `verify_stress_liveness_pool` [I] so a SQLAlchemy upgrade that renames it fails a
    suite instead of silently restoring the 18x behaviour."""

    def _has_free_connection(self) -> bool:
        """Is there capacity RIGHT NOW, read from the pool's own counters — no checkout, no
        lock, no wait. The same non-blocking predicate the doorman uses
        (`pool_has_no_capacity`), asked of this pool instead of the module's engine so a
        second engine cannot be answered about the first."""
        try:
            overflow_cap = self._max_overflow
            if overflow_cap < 0:  # unbounded overflow: there is always another connection
                return True
            return self.checkedout() < (self.size() + overflow_cap)
        except Exception:
            return True  # a pool that cannot report must never be a reason to refuse

    def _do_get(self):
        budget = _pool_budget.get()
        # ONE WAIT PER STALL, NOT ONE PER REQUEST. Refusing instantly is right only while
        # the stall LASTS: the counters say whether it does. Reading them (rather than
        # trusting `spent`) is what makes the recovery branch below reachable at all — the
        # first version checked `spent` alone, so no later checkout in the scope ever ran
        # `super()._do_get()`, the clear was dead code, and the gate's finding (a scope
        # refused in 0.000s on a completely free pool) survived its own fix. Asking the
        # counters costs nothing and cannot block; if they are wrong in the racy direction
        # the caller spends at most one more honest `pool_timeout`, and only while the pool
        # looks free.
        if budget is not None and budget.spent and not self._has_free_connection():
            raise _SATimeoutError(
                f"connection pool wait budget already spent for this request "
                f"(one {config.DB_POOL_TIMEOUT_S}s checkout timeout per stall)"
            )
        try:
            conn = super()._do_get()
        except _SATimeoutError:
            if budget is not None:
                budget.spent = True
            raise
        if budget is not None and budget.spent:
            # The stall is over: this scope may wait once more if the pool fills again.
            budget.spent = False
        return conn


# A ":memory:" SQLite URL gets SingletonThreadPool/StaticPool, which take neither
# max_overflow nor pool_timeout -- so the QueuePool sizing below is only passed for a
# pooled (file / server) URL. Nothing in the product runs on :memory:, but a test or an
# embedding host can, and boot must not explode on an unexpected kwarg.
_IS_MEMORY_DB = config.IS_SQLITE and ":memory:" in config.DB_URL
# GitHub #100: SIZE THE POOL EXPLICITLY. SQLAlchemy's defaults (5 + 10 overflow, 30s
# timeout) are sized for short request-scoped checkouts; this app pins one connection per
# in-flight workflow run for the run's whole duration, so 15 connections is fewer than the
# runs a single flood launches. The 30s default timeout is the worse half: it converts
# "the pool is full" into thirty seconds of a pinned anyio worker thread (or a pinned
# event loop, for an async handler), which is how a DB-FREE /health ends up stalling for
# minutes (#99). See config.DB_POOL_SIZE for the whole capacity triangle.
_POOL_KWARGS: dict = (
    {}
    if _IS_MEMORY_DB
    else {
        "pool_size": config.DB_POOL_SIZE,
        "max_overflow": config.DB_MAX_OVERFLOW,
        "pool_timeout": config.DB_POOL_TIMEOUT_S,
        # pre_ping stays OFF (SQLAlchemy's default, stated rather than inherited): it
        # would add a round trip to EVERY checkout, and checkout is the hot path this
        # train just finished making cheap. Staleness is handled by recycling instead —
        # a connection idle for an hour (across a Fly machine suspend, say) is re-opened
        # on its next use rather than trusted. Turn pre_ping on if this install is ever
        # pointed at a Postgres behind a connection-killing proxy.
        "pool_pre_ping": False,
        "pool_recycle": 3600,
        # The per-request wait budget above. Same QueuePool behaviour in every other
        # respect — it only refuses to wait a SECOND time inside one request.
        "poolclass": _BudgetedQueuePool,
    }
)

# check_same_thread=False lets FastAPI's threadpool share the connection safely
# for our simple single-user local use — a SQLite-only kwarg.
# dbcrypt.engine_kwargs() is {} on the default path (byte-unchanged) and only
# swaps in the SQLCipher DBAPI + keying creator when LOC_TMS_DB_ENCRYPTION=sqlcipher.
engine = create_engine(
    config.DB_URL,
    echo=False,
    connect_args={"check_same_thread": False} if config.IS_SQLITE else {},
    **_POOL_KWARGS,
    **dbcrypt.engine_kwargs(config.DB_PATH),
)


# SQLite writer-lock wait (QA-1 defense-in-depth). A few seconds: long enough to
# ride out any brief legitimate contention (one connection committing while another
# waits to write), short enough that a genuinely stuck writer still surfaces as an
# error rather than hanging a request/worker indefinitely.
BUSY_TIMEOUT_MS = 5000


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL lets readers proceed during a write (the reloader + SSE streams +
    request threads all share this file); NORMAL sync is safe with WAL.

    busy_timeout (QA-1, defense-in-depth): SQLite is single-writer, so a second
    connection that tries to write while another holds the writer lock gets
    SQLITE_BUSY. We set the wait EXPLICITLY here so the writer-contention floor is
    owned by this listener (greppable; batch_runs._dispatch's zombie-guard comment
    references db.BUSY_TIMEOUT_MS) and is guaranteed regardless of the DBAPI default
    or any future connect_args change. NB: this is belt-and-suspenders, not the cure
    for QA-1 — pysqlite's connect() already applies a ~5s busy_timeout by default, so
    the value was effectively 5000ms before this line too, and the graph-workflow
    deadlock happened ANYWAY: the orchestrator held an uncommitted write across the
    provider call and spend.record_usage's own session (same thread, sequential) then
    blocked on it — a true circular wait a busy_timeout can only DELAY (it errored
    after the ~5s wait, the report's "locked after 5.16s"), never resolve. The actual
    cure is removing the hold (orchestrator commits the RunGuard scratchpad before
    dispatching the step). What this pragma DOES buy: brief, legitimate contention
    between INDEPENDENT writers (one of which will commit) is waited out to a bounded
    ceiling rather than relying on an implicit DBAPI default. Connection-scoped, so
    set here on every connect alongside the other pragmas.

    foreign_keys=ON makes SQLite ACTUALLY enforce FK constraints (SQLite
    ignores them by default — a decades-old back-compat quirk). Postgres has
    always enforced these same FKs, so turning this on merely aligns the
    zero-config/test engine with production: any write it now rejects was
    already broken on real Postgres. This closes the blind spot where an
    FK-invalid write (e.g. the glossary project_id=0 sentinel) could pass a
    fully-green SQLite suite. Set PER CONNECTION because the pragma is
    connection-scoped, not database-scoped.

    cache_size (GitHub #109, gate A round 1's memory paragraph): SQLite's page cache is
    PER CONNECTION, and its default of -2000 (2 MiB) is invisible at five connections and
    ~120 MiB of resident memory at the sixty this pool may open — on the 1 GB machine that
    OOM-killed production. Set explicitly from config.SQLITE_CACHE_KB so that pool sizing
    and the memory budget are one decision with published arithmetic
    (docs/architecture/configuration.md), not a library default nobody multiplied out.
    Negative = KiB (SQLite's own convention), which is what we want: a fixed byte budget
    per connection regardless of page size."""
    # Match the former Python key-search semantics, including non-ASCII letters.
    dbapi_conn.create_function(
        "loc_lower", 1, lambda text: text.lower() if text is not None else None, deterministic=True
    )
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute(f"PRAGMA cache_size=-{int(config.SQLITE_CACHE_KB)}")
    cur.execute("PRAGMA mmap_size=0")
    cur.close()


# Registered only for SQLite (the pragmas are SQLite-only), on the pooled engine AND on
# the pool-less bookkeeping engine below, so a connection opened by either one enters
# WAL with the same busy_timeout and FK enforcement. Defined UNCONDITIONALLY above and
# registered here, rather than defined inside the `if`, so `emergency_session` can name
# the function directly instead of fishing it out of globals().
if engine.dialect.name == "sqlite":
    event.listen(engine, "connect", _sqlite_pragmas)


# Additive migrations: new *columns* on existing tables. create_all() only
# creates missing tables, never alters existing ones, so columns added in a later
# build must be backfilled here for databases created by an earlier build.
_ADDED_COLUMNS = {
    "invite": [
        ("revoked_at", "DATETIME"),  # #129: NULL means never revoked.
        ("org_id", "INTEGER"),  # #129: legacy NULL ownership is operator-only under multi-org.
    ],
    "workflowbatch": [("reopened_members", "JSON"), ("dispatcher_owner", "JSON")],
    # #364: exact booked USD per Job, nullable, no default (NULL = pre-tracking / unknown).
    "job": [("cost_usd", "FLOAT")],
    # Project automation round 2: claim hand-over bookkeeping.
    "projectbindingclaim": [("wanted_by", "INTEGER")],
    "projectautomation": [("blocked_sig", "VARCHAR DEFAULT ''")],
    # Connected projects: a stopped (soft-removed) target language of a bound project.
    "targetlocale": [("stopped_at", "DATETIME")],
    "project": [
        ("created_by", "VARCHAR DEFAULT ''"),
        ("org_id", "INTEGER"),
        # Lifecycle (P1): status defaults to the pre-P1 implicit state so
        # existing rows backfill to 'in_translation'; the two timestamps are
        # nullable (NULL = never transitioned / never completed).
        ("status", "VARCHAR DEFAULT 'in_translation'"),
        ("status_changed_at", "DATETIME"),
        ("completed_at", "DATETIME"),
        # Segmentation profiles (P6, item 8): NULL = inherit the resolution
        # chain (customer default → system default → built-in). Existing rows
        # backfill to NULL = byte-identical to today (built-in default rung).
        ("segmentation_profile_id", "INTEGER"),
        # Optional due date (Feature A): NULL = no due date (the default).
        # Stored as a naive-UTC datetime at the day boundary; existing rows
        # backfill NULL = byte-identical to today.
        ("due_date", "DATETIME"),
        # Per-pair provider mode (Phase 2): 'uniform' (the pre-Phase-2
        # default) or 'per_pair'. Existing rows backfill to 'uniform' =
        # byte-identical (default_provider drives every pair). The
        # projectpairprovider TABLE is made by create_all() on SQLite (a
        # new table, not an altered one), mirroring workflowgroup.
        ("provider_mode", "VARCHAR NOT NULL DEFAULT 'uniform'"),
        # Content types (SPEC-content-types): NULL = the shared "General"
        # bucket. Existing rows backfill NULL = byte-identical to today (no
        # content type = today's un-scoped behaviour). The contenttype TABLE
        # is made by create_all() on SQLite (a new table, not an altered one).
        ("content_type_id", "INTEGER"),
        # Inbox re-key (SPEC-rename-delete): the stable key inbox projects are
        # looked up by (routing._inbox_key), so renaming an inbox no longer
        # forks a duplicate. NULL for every ordinary project. Pre-existing inbox
        # projects are backfilled by the one-time UPDATE in _ensure_columns
        # below (runs ONLY when the column is first added). The functional
        # unique index (coalesce(org_id,-1), inbox_key) is in _UNIQUE_INDEXES.
        ("inbox_key", "VARCHAR"),
        # Key-based projects (SPEC-key-based-projects 2.1): every existing row
        # backfills to 'document' = today's behaviour, byte-identically. NOT
        # NULL + DEFAULT because "which kind of project" is a value, never an
        # absence. The stringkey/keychangelog TABLES are made by create_all()
        # on SQLite (new tables, not altered ones), mirroring contenttype.
        ("project_type", "VARCHAR NOT NULL DEFAULT 'document'"),
        # Connected projects (SPEC-connected-projects 4; alembic f2c8a6d4b1e7): where the
        # content comes from, 'upload' (every existing row) or 'connected'.
        ("intake", "VARCHAR NOT NULL DEFAULT 'upload'"),
        # Archive/retire (residual sweep: archive verbs; Customer.archived_at
        # precedent). NULL = ACTIVE. A non-NULL timestamp retires the project
        # from default listings/pickers + blocks new runs; existing rows
        # backfill NULL = byte-identical to today.
        ("archived_at", "DATETIME"),
        # AI opt-out (HECVAT DPAI-08; alembic e4b7c1a9d2f6). 0 = AI allowed.
        ("ai_opt_out", "BOOLEAN NOT NULL DEFAULT 0"),
        # MT formality per target locale (#617 item 7; alembic b8e2f4a6c1d3).
        ("formality", "JSON"),
    ],
    # Context package (SPEC-context-metadata phase 1): the request/doc-level
    # TranslationContext that arrived with the doc (API/connector/upload). NULL on
    # every existing row = no context = byte-identical to today. Stored as JSON (TEXT
    # on SQLite). The contextprofile TABLE is made by create_all() (a new table).
    "document": [
        ("created_by", "VARCHAR DEFAULT ''"),
        ("context", "JSON"),
        ("upload_blob_key", "VARCHAR"),
        # MT formality override per target locale (#617 item 7, ADR 0062; alembic
        # b8e2f4a6c1d3). NULL = inherit the project setting.
        ("formality", "JSON"),
    ],
    "user": [
        ("org_id", "INTEGER"),
        ("ui_locale", "VARCHAR DEFAULT ''"),
        ("department", "VARCHAR DEFAULT ''"),
        ("is_superadmin", "BOOLEAN NOT NULL DEFAULT 0"),
        # Customer binding (R2-3): NULL = internal staff (byte-identical).
        ("customer_id", "INTEGER"),
        # Onboarding checklist (SPEC-onboarding-tour Phase 0): NULL = ELIGIBLE
        # (first-run "Get started" checklist may auto-show once). The one-time
        # backfill in _ensure_columns' just-added branch stamps EVERY pre-existing
        # user to dismissed so no live account is shown the tour uninvited — only
        # accounts created AFTER the column is added land NULL = eligible.
        ("onboarding_dismissed_at", "DATETIME"),
        # SCIM 2.0 external id (Train I): the IdP's stable opaque id. NULL on every
        # non-SCIM account = byte-identical to today. Indexed (ix_user_external_id in
        # _INDEXES) for the `externalId eq` filter lookup. The usergroup/usergroupmember
        # TABLES are made by create_all() on SQLite (new tables, not altered ones).
        ("external_id", "VARCHAR"),
        # LEGACY, ignored since the Console look (2026-10-08): the old per-user UI skin
        # (kiln | instrument | ledger). Nothing reads or writes it; the column stays so
        # old databases and the alembic revision c1d5e8f3a9b4 keep matching the model.
        ("ui_skin", "VARCHAR"),
        # Appearance (ADR 0063): light | dark; "" = Match my computer. Mirrors the model and
        # the alembic revision e4a7c2b9d1f6.
        ("ui_theme", "VARCHAR NOT NULL DEFAULT ''"),
        # Session generation (#138): the epoch every session cookie is minted under.
        # NOT NULL DEFAULT 0, so every pre-existing row reads 0 and an upgraded
        # install keeps working until the user logs out (which bumps it, killing
        # every cookie minted before). Mirrors the alembic revision b4e1c7f29a06.
        ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
        # Password policy (HECVAT AAAI-03): when the local password was last set (the
        # max-age clock) and the admin-reset "change at next sign-in" flag. The one-time
        # backfill in _ensure_columns' just-added branch starts every existing local
        # password's clock at the upgrade. Mirrors the alembic revision e5a1c8d3f7b2; the
        # passwordhistory TABLE is made by create_all() (a new table).
        ("password_changed_at", "DATETIME"),
        ("password_must_change", "BOOLEAN NOT NULL DEFAULT 0"),
        # OIDC subject binding (identity-01): NULL = not bound yet; an existing SSO
        # account binds on its next verified OIDC sign-in. Indexed (ix_user_sso_subject
        # in _INDEXES). Mirrors the alembic revision c5e2a8d4f1b7.
        ("sso_issuer", "VARCHAR"),
        ("sso_subject", "VARCHAR"),
    ],
    # AuditLog.customer_id (R2-3): NULL = system event. Canonical v2 hashes include
    # this column; historical hashes remain unchanged. Existing rows
    # backfill NULL = system (accepted semantics; no retroactive backfill).
    "auditlog": [
        ("prev_hash", "VARCHAR DEFAULT ''"),
        ("row_hash", "VARCHAR DEFAULT ''"),
        ("customer_id", "INTEGER"),
        # #487: the org the actor belonged to when the row was written (outside the hash).
        ("org_id", "INTEGER"),
    ],
    # Optional key expiry (#270 r2-apiauth-10): NULL = no expiry. Purely additive
    # and neutral: every pre-existing key reads NULL and keeps working exactly as
    # before. The table is `apikey` (no underscore); mirrors the model + the
    # alembic revision c4e7f2b9a1d3.
    "apikey": [("expires_at", "DATETIME")],
    # Conditional-workflow per-run scratchpad (SPEC-conditional-workflows §3.3,
    # Phase 1): survives pause/resume. NULL until the first CONDITIONAL feature
    # writes it — a plain run never does, so existing/plain runs stay byte-identical
    # (context IS NULL). Read by nobody until Phase 2/3.
    "workflowrun": [
        ("target_locale", "VARCHAR DEFAULT ''"),
        ("context", "JSON"),
        # jobs-history: who launched the run (see WorkflowRun.created_by).
        ("created_by", "VARCHAR DEFAULT ''"),
        # Fan-out batch membership (SPEC-workflows-redesign Phase 5a): NULL
        # on every existing row = a plain single run (byte-identical — the
        # history/run paths never behaved differently). Non-NULL links a
        # member run to its WorkflowBatch. The workflowbatch TABLE is made by
        # create_all() on SQLite (a new table, not an altered one).
        ("batch_id", "INTEGER"),
        # Why the run ended (GitHub #44): NULL on every existing row and on
        # every ordinary run — only the restart/staleness recovery seam
        # writes it, so existing rows and the happy path are byte-identical.
        ("notes", "VARCHAR"),
        # Durable tm_match provenance marker (Batch 15 T1, SPEC-tm-match-step-config
        # §5.1b / N2): True once THIS run executed a tm_match step (segmented OR
        # FULL_TEXT). NOT NULL DEFAULT 0 (the qe_gate bool shape): every legacy run
        # backfills False, so a pre-Batch-15 tm_match run is NOT retroactively gated by
        # the T2 delivery empty gate (the conservative prospective posture, §11/P5).
        # Mirrors the model + the alembic revision c5e8a1f9d3b7 (nullability-parity CI).
        ("used_tm_match", "BOOLEAN NOT NULL DEFAULT 0"),
        # In-step progress (GitHub #101): "N of M units done" published from INSIDE a
        # long llm_agent step, so /api/runs/{id}, `workflow show-run` and the SSE stream
        # stop reporting silence for the 10-25 minutes such a step takes. 0/0/''/NULL on
        # every existing row = "no progress recorded" (never a false 0%). Mirrors the
        # model + alembic revision d1f4a7c3e9b2 (nullability-parity CI guard).
        ("progress_done", "INTEGER NOT NULL DEFAULT 0"),
        ("progress_total", "INTEGER NOT NULL DEFAULT 0"),
        ("progress_unit", "VARCHAR NOT NULL DEFAULT ''"),
        # Which step the current N-of-M belongs to: a fresh 0-of-M per step means the bare
        # counter legitimately decreases at a boundary, so it is attributed (gate F, L3).
        ("progress_step", "VARCHAR NOT NULL DEFAULT ''"),
        ("progress_step_index", "INTEGER NOT NULL DEFAULT 0"),
        # The MT formality register that applied to the run (#617 item 7; b8e2f4a6c1d3).
        ("formality", "JSON"),
        ("progress_at", "DATETIME"),
        # The fan-out this row was queued by (gate A round 3, HIGH-1/2/3): NULL on every
        # existing row and on every ordinary single launch, non-NULL for a row a
        # "translate all" created up front in RunStatus.QUEUED. Mirrors the model +
        # alembic revision e2b6c9f4a713 (nullability-parity CI guard).
        ("queue_group", "VARCHAR"),
    ],
    # Conditional-workflow durable outcome signal (SPEC-conditional-workflows §3.2,
    # Phase 1): score/gate NULL where a step produced no outcome (populated by the
    # LQA + QE branches); outcome_index the step's visit number (0 this phase — no
    # loops). Additive + neutral-defaulted -> every existing StepRun row is
    # byte-identical. Read by nobody until Phase 2/3.
    "steprun": [
        ("score", "FLOAT"),
        ("gate", "VARCHAR"),
        ("outcome_index", "INTEGER NOT NULL DEFAULT 0"),
        # Real per-step-visit spend (feat/workflow-spend): the ACTUAL USD cost
        # + token usage this step-visit accrued (the same figure the spend
        # ledger recorded for its completion calls). All three NULLABLE with NO
        # default -> every existing StepRun row backfills to NULL = "pre-tracking
        # / unknown" (byte-identical), and the reports workflow axis treats an
        # all-NULL run as `(untracked)` rather than zeroing it. New runs stamp
        # real values on completion-bearing steps.
        ("cost_usd", "FLOAT"),
        ("tokens_in", "INTEGER"),
        ("tokens_out", "INTEGER"),
        # Per-step FAILURE SHAPE (run 190 Item C): the finish reason, provider error
        # class and produced-char count for this step visit. All three NULLABLE with NO
        # default -> every existing StepRun row backfills to NULL = "pre-tracking /
        # unknown" (byte-identical), never a fabricated value. Mirrors the model + the
        # alembic revision b7e3f1a9c2d4 (nullability-parity CI). New runs stamp real
        # values on completion-bearing steps (llm_agent / QE / LQA).
        ("finish_reason", "VARCHAR"),
        ("error_class", "VARCHAR"),
        ("output_chars", "INTEGER"),
        # #356 phase 2a: the system-prompt head this AI step visit ran with (key, override
        # version, sha256 of the composed text). NULLABLE, no default: written only while
        # the system_prompt_overrides flag is ON, so every existing row stays NULL.
        # Mirrors the model + alembic revision b2c4d6e8f0a1.
        ("system_prompt_key", "VARCHAR"),
        ("system_prompt_version", "INTEGER"),
        ("system_prompt_sha256", "VARCHAR"),
        # #356 phase 2b: which scope's head won (workflow | org | builtin). Mirrors the
        # model + alembic revision c3d5e7f9a1b2.
        ("system_prompt_scope", "VARCHAR"),
    ],
    "workflowstep": [
        ("redact_pii", "BOOLEAN NOT NULL DEFAULT 0"),
        # P5 (item 4): LLM-adapted cross-language copies. adapted_from_id
        # is the source step a copy was derived from; needs_review flags a
        # linguist post-edit. Both NULL/0 on legacy rows (byte-identical).
        ("adapted_from_id", "INTEGER"),
        ("needs_review", "BOOLEAN NOT NULL DEFAULT 0"),
        # Shared-base + override-delta variant model (SPEC-workflows-redesign
        # 4b): overridden=1 pins a variant step so base-edit propagation never
        # touches it. Default 0 (NOT NULL) backfills every existing row to
        # "not pinned" — SAFE because propagation ALSO requires a non-NULL
        # adapted_from_id (a live provenance link), which legacy rows lack, so
        # a legacy step is never a propagation target regardless. Mirrors the
        # model + the alembic migration (nullability-parity CI guard).
        ("overridden", "BOOLEAN NOT NULL DEFAULT 0"),
        # Human-review assignee (Feature B): the reviewer emailed when a
        # human_review step pauses. "" (default) = no assignee = no email.
        # Existing rows backfill "" (byte-identical).
        ("assignee_email", "VARCHAR DEFAULT ''"),
        # Glossary module (Phase 5-B): the APPROVED module Glossary this
        # step pins. NULL = today's project-scoped match_terms (byte-
        # identical). The glossary/glossarypair TABLES are made by
        # create_all() on SQLite (new tables, not altered ones).
        ("glossary_id", "INTEGER"),
        # F2 engine-layer upgrades (ModernMT/Lara). Both default OFF so
        # legacy rows behave byte-identically (unchanged prompt).
        ("adaptive_feedback", "BOOLEAN NOT NULL DEFAULT 0"),
        ("adaptive_feedback_window", "INTEGER NOT NULL DEFAULT 5"),
        ("doc_context_prepass", "BOOLEAN NOT NULL DEFAULT 0"),
        # MQM-aligned QE score + routing gate (roadmap gap #1/#6). All
        # default OFF / neutral so legacy rows behave byte-identically
        # (no judge call, unchanged prompt/events). qe_provider NULL =
        # reuse the step's own provider.
        ("qe_gate", "BOOLEAN NOT NULL DEFAULT 0"),
        ("qe_threshold", "INTEGER NOT NULL DEFAULT 70"),
        ("qe_provider", "VARCHAR"),
        # QE auto-fix (Train B, feat/jev-autofix). All default OFF / neutral so a
        # legacy row never enters the fix loop (byte-identical). NULL
        # qe_autofix_provider = the step's own provider. Mirrors the model + the
        # alembic revision d8c2a6f1e4b9 (nullability-parity CI guard).
        ("qe_autofix", "BOOLEAN NOT NULL DEFAULT 0"),
        ("qe_autofix_provider", "VARCHAR"),
        ("qe_autofix_max_rounds", "INTEGER NOT NULL DEFAULT 3"),
        ("qe_autofix_cross_check", "BOOLEAN NOT NULL DEFAULT 0"),
        # Conditional-workflow PRE-STEP GUARD (SPEC-conditional-workflows
        # §3.1, Phase 2): NULL = always run (byte-identical to today; a
        # plain step never evaluates it). Additive JSON column, mirrors the
        # model + the alembic migration (nullability-parity CI guard).
        ("run_condition", "JSON"),
        # TM few-shot examples (SPEC-context-metadata phase 2B). Default
        # ON / window 3 — a step with NO qualifying fuzzy match renders
        # nothing, so a legacy row's prompt stays byte-identical until a
        # match clears the similarity floor.
        ("tm_examples", "BOOLEAN NOT NULL DEFAULT 1"),
        ("tm_examples_window", "INTEGER NOT NULL DEFAULT 3"),
        # Per-step image context toggle (Batch 14 T2, SPEC §25). DEFAULT-TRUE bool
        # (the tm_examples DDL exactly): backfills every legacy row to ON so the
        # ratified all-LLM_AGENT-steps image behavior is preserved on a create_all-only
        # legacy SQLite boot where alembic never ran. Mirrors the model + the alembic
        # revision (nullability-parity CI guard).
        ("use_image_context", "BOOLEAN NOT NULL DEFAULT 1"),
        # Per-step concept-glossary renderer toggle (Terminology backend T3, SPEC
        # §9-T3). DEFAULT-OFF bool + two caps, all NOT NULL server-defaulted so a
        # create_all-only legacy SQLite boot (where alembic never ran) backfills every
        # legacy row to OFF/10/0 = today's exact prompts.glossary_line behavior
        # byte-for-byte (LAW-TERM-1/14). Mirrors the model + the alembic revision
        # (nullability-parity CI guard).
        ("concept_glossary_render", "BOOLEAN NOT NULL DEFAULT 0"),
        ("glossary_concept_cap", "INTEGER NOT NULL DEFAULT 10"),
        ("glossary_token_cap", "INTEGER NOT NULL DEFAULT 0"),
        # TM provenance guard (PROPOSAL-lqa-glossary-adjudication Phase 1,
        # D1). NULLABLE TRISTATE, no default: NULL on every legacy row is
        # the correction-loop-shaped default (protect only a fix step), so
        # a workflow with no LQA step is byte-identical. True/False are the
        # explicit always/never overrides. Mirrors the model + the alembic
        # revision (nullability-parity CI guard).
        ("tm_protect", "BOOLEAN"),
        # Conditional-workflow BRANCHING + bounded loop-back (SPEC §3.1,
        # Phase 3). All NULL on legacy rows = an unnamed, non-branching,
        # default-capped step -> byte-identical (a linear run never reads
        # them). key = stable slug (unique per workflow); branch = post-step
        # routing JSON on a gate step; max_visits = per-step loop ceiling
        # (NULL inherits the run-level default). Mirrors the model + the
        # alembic migration (nullability-parity CI guard).
        ("key", "VARCHAR"),
        ("branch", "JSON"),
        ("max_visits", "INTEGER"),
        # LLMemory step-level opt-out (PROPOSAL-llmemory §6). NULLABLE, no
        # default: NULL on every legacy row means the same thing as "auto"
        # (participate normally), so a pre-LLMemory install is byte-
        # identical; "off" suppresses memory injection for that step only.
        # OPT-OUT ONLY -- it can never make an ineligible step (LQA judge,
        # correction-loop fixer) eligible. Mirrors the model + the alembic
        # revision f2a9c4d81b73 (nullability-parity CI guard).
        ("memory_mode", "VARCHAR"),
        # Parallel step routing (SPEC-parallel-routing §5.6, Phase 1).
        # NULLABLE, no default: NULL on every legacy row = "not in a
        # parallel group", so a pre-parallel install is byte-identical
        # (the orchestrator's `_has_parallel` gate is False and the whole
        # fork/join seam is never entered). Consecutive steps sharing a
        # non-NULL slug run concurrently against one frozen snapshot.
        # Mirrors the model + the alembic revision a4e7d2c8b91f
        # (nullability-parity CI guard).
        ("parallel_group", "VARCHAR"),
        # External/vendor dispatch-and-wait (PROPOSAL-external-processing-
        # step Wave A). Both NULL on every legacy row = a non-external step
        # -> byte-identical (a plain run never reads them). vendor_id is the
        # dispatch target for a StepType.EXTERNAL step (a plain INTEGER in
        # the shim like glossary_id/key_id, a real FK in the model +
        # migration); external_wait_hours is the per-step bounded-wait
        # override (NULL inherits the install TTL). The vendor/vendorcustomer/
        # vendordispatch TABLES are made by create_all() on SQLite (new
        # tables, not altered ones). Mirrors the model + the alembic
        # revision (nullability-parity CI guard).
        ("vendor_id", "INTEGER"),
        ("external_wait_hours", "INTEGER"),
        # LQA step configurable pass threshold (feat/lqa-configurable-
        # threshold). NULLABLE, no default: NULL on every legacy row = the
        # built-in LQA gate (score >= 90 AND no major/critical) -> byte-
        # identical. A set value (0-100) is the step's own numeric pass bar.
        # DEAD on non-lqa steps (refused at the authoring layer). Mirrors the
        # model + the alembic revision (nullability-parity CI guard).
        ("lqa_threshold", "INTEGER"),
        # tm_match step per-step config (Batch 15 T1, SPEC-tm-match-step-config §5.1).
        # SIX NULLABLE overrides, NO default: NULL on every legacy row = inherit the
        # live global = today's exact tm_match behavior byte-for-byte (LAW-TM-3). The
        # tm_protect nullable-tristate shape, NOT the NOT-NULL use_image_context shape.
        # A NEW tm_match step is seeded with concrete global snapshots at creation
        # (add_step); LIVE only on tm_match, dead elsewhere. Mirrors the model + the
        # alembic revision c5e8a1f9d3b7 (nullability-parity CI guard).
        ("tm_fill_threshold", "INTEGER"),
        ("tm_fuzzy_lock_mode", "VARCHAR"),
        ("tm_lock_ice", "BOOLEAN"),
        ("tm_hundreds_enter_lqa", "BOOLEAN"),
        ("tm_context_floor", "INTEGER"),
        ("tm_pool_selection", "JSON"),
        # run-190 Item D / M5 (gate round 1): per-step opt-out of the over-translation
        # (page-dump) guard on an llm_agent step. NULLABLE tri-state (the tm_* override
        # shape, NOT the NOT-NULL use_image_context shape): NULL -> follow the global
        # OVER_TRANSLATION_MAX_RATIO knob (default ON); 0 -> this step opts OUT (a
        # legitimately expanding rewrite/post-edit step); 1 -> explicitly ON. Every
        # existing row backfills NULL = ON, byte-identical. Mirrors the model + the alembic
        # revision b7e3f1a9c2d4 (nullability-parity CI guard).
        ("over_translation_guard", "BOOLEAN"),
    ],
    # The guarded RETURN (PROPOSAL-external-processing-step Wave B): the contract id
    # LEDGER, {"dispatched": [...], "returned": [...]}. NULL on every Wave-A row = no
    # recorded contract, which the return path treats as un-accountable and REFUSES
    # (fail-closed) rather than guessing an id set. Mirrors the model + the alembic
    # revision a9f3c1e7d5b2 (nullability-parity CI guard).
    # Wave C dead-letter/backoff (interop R5): the transient-failure retry columns.
    # attempt_count defaults 0 (never failed transiently); next_attempt_at NULL (not
    # parked for retry); last_error '' (no sanitized reason). Byte-identical for a
    # Wave-A/B row. Mirrors the model + the alembic revision b2c3d4e5f6a7.
    # Wave E (R3 quote gate + R4 reject/rework): the quote reconciliation columns +
    # rework lineage. All default 0/NULL/'' so a legacy leg reads byte-identical (never
    # quote-gated, never reworked). Mirrors the model + the alembic revision c3d4e5f6a7b8.
    "vendordispatch": [
        ("contract_ids", "JSON"),
        ("attempt_count", "INTEGER DEFAULT 0"),
        ("next_attempt_at", "DATETIME"),
        ("last_error", "VARCHAR DEFAULT ''"),
        ("retry_contract", "JSON"),
        ("requires_quote", "BOOLEAN DEFAULT 0"),
        ("quoted_cost_usd", "FLOAT"),
        ("quote_currency", "VARCHAR DEFAULT ''"),
        ("quoted_at", "DATETIME"),
        ("quote_approved_at", "DATETIME"),
        ("quote_note", "VARCHAR DEFAULT ''"),
        ("rework_round", "INTEGER DEFAULT 0"),
        ("rework_of", "INTEGER"),
        ("rework_feedback", "JSON"),
        # F2 protocol snapshot: NULLABLE, no default (deliberately unlike the
        # vendor.protocol column's 'native' default) so a legacy leg reads NULL
        # and leg_protocol resolves it to NATIVE -- a pre-migration leg was native
        # BY CONSTRUCTION (`manual` did not exist yet), never the vendor's current
        # live protocol. Mirrors the model + the alembic revision d4e5f6a7b8c9
        # (nullability-parity CI guard).
        ("protocol", "VARCHAR"),
    ],
    # Vendor TRANSPORT PROTOCOL seam (interop R1, Wave C — seam only). The vendor TABLE
    # is created by create_all() on a fresh SQLite install, but an install that already
    # has a Wave-A vendor table needs this ALTER to add `protocol`. NULL/'' reads as
    # 'native' (today's behavior) at the single dispatch-by-protocol seam, so a legacy
    # row is byte-identical. Mirrors the model + the alembic revision b2c3d4e5f6a7.
    "vendor": [("protocol", "VARCHAR DEFAULT 'native'")],
    # Glossary module (Phase 5-B): glossary_id links a legacy per-project entry to a
    # first-class Glossary container. NULL on every existing row = a LEGACY project
    # entry (byte-identical — matched by project_id, never in the module list).
    # SPEC-context-metadata phase 1: term metadata JSON, written in later phases.
    # SPEC-terminology-backend T1 (§4.4/§4.6, M6/M19): concept_id/term_id link the
    # derived compat entry into the concept/term store. FK-LESS on the shim — SQLite
    # cannot ADD COLUMN ... REFERENCES, and these are inert nullable links (alembic +
    # Postgres carry the real FK). Do NOT hand-write a REFERENCES clause here.
    "glossaryentry": [
        ("glossary_id", "INTEGER"),
        ("meta", "JSON"),
        ("concept_id", "INTEGER"),
        ("term_id", "INTEGER"),
        # SPEC-terminology-backend T1b (LAW-TERM-18, §A.1/§A.3): the per-pair binding's
        # enforced-target pointer + per-pair meta authority. FK-less on the shim (SQLite
        # cannot ADD COLUMN ... REFERENCES); alembic + Postgres carry the real FK. The
        # ix_glossaryconcept_hgroup index is deliberately NOT shimmed (r7-11 — the closure
        # query runs unindexed on a legacy-SQLite install; accepted, performance-only).
        ("target_term_id", "INTEGER"),
        ("meta_overrides", "JSON"),
    ],
    # SPEC-terminology-backend T1 (§4.4/H2): the SAME two FK-less link columns on
    # glossarysense (the sibling homonym concept + its target term). NULL on every
    # legacy sense = byte-identical.
    "glossarysense": [("concept_id", "INTEGER"), ("term_id", "INTEGER")],
    # Content types (SPEC-content-types phase 2): a nullable FK scoping a first-class
    # Glossary to one ContentType. NULL on every existing row = the shared "General"
    # bucket (byte-identical — a NULL-type glossary applies to every project); a
    # non-NULL id offers/auto-attaches the glossary only to projects of that type.
    "glossary": [("content_type_id", "INTEGER")],
    # TM module usage tracking: use_count + last_used_at on segmenttm. Bumped by one
    # in-place UPDATE per APPLIED TM hit at every apply site (tm_module.bump_usage);
    # SUGGESTIONS never bump them. Existing rows backfill use_count 0 + last_used_at
    # NULL (a never-used entry) — byte-identical (the translate path never read them).
    # SPEC-content-types phase 3: content_type_id stamped from the owning project at
    # TM-write time (write_back/ft_store/import_tmx; the embedding carries the parent
    # row's value). NULL on every existing row = the shared "General" bucket, so reads
    # from an untyped project (content_type_id IS NULL scope) return exactly today's set
    # — byte-identical. Indexed via _INDEXES + the phase-3 alembic revision.
    "segmenttm": [
        ("use_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_used_at", "DATETIME"),
        ("activity_at", "DATETIME"),
        ("content_type_id", "INTEGER"),
        # TM-unit metadata (SPEC-context-metadata phase 1): column ships
        # now, written in later phases. NULL today = byte-identical.
        ("meta", "JSON"),
    ],
    "fulltexttm": [("content_type_id", "INTEGER"), ("activity_at", "DATETIME")],
    "segmentembedding": [("content_type_id", "INTEGER")],
    # SPEC-context-metadata phase 1: per-unit metadata JSON, written in later phases.
    # NULL on every existing row = byte-identical (nothing reads it this phase).
    # Key-based projects (SPEC-key-based-projects 2.4): both NULL on every existing
    # (document) segment = byte-identical. key_id is a real FK in the model + the
    # migration; the shim adds a plain INTEGER (same as content_type_id) because
    # SQLite cannot add a REFERENCES clause to an existing table without a rebuild,
    # and this legacy path is compared on nullability, not on constraints.
    # TM-matching locks (PROPOSAL-tm-matching-workflow-step §8): lock_band NULL on every
    # existing row = unlocked = byte-identical; lock_hard NOT NULL DEFAULT 0 (a boolean
    # value, never an absence, mirroring is_default/active). Provenance rides meta.
    "segment": [
        ("meta", "JSON"),
        ("key_id", "INTEGER"),
        ("plural_category", "VARCHAR"),
        ("lock_band", "VARCHAR"),
        ("lock_hard", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
    # P5 multilingual workflows (item 4): group_id/source_locale/target_locale/
    # is_default on Workflow. All nullable/defaulted → existing workflows backfill to
    # group_id NULL (ungrouped/universal) + is_default 0, behaving byte-identically
    # (dispatch never touches an ungrouped workflow). The workflowgroup TABLE is made
    # by create_all() on the SQLite path (a new table, not an altered one).
    "workflow": [
        ("org_id", "INTEGER"),
        ("group_id", "INTEGER"),
        ("source_locale", "VARCHAR"),
        ("target_locale", "VARCHAR"),
        ("is_default", "BOOLEAN NOT NULL DEFAULT 0"),
        # Archive/retire (residual sweep: archive verbs; Customer.archived_at
        # precedent). NULL = ACTIVE; a non-NULL timestamp retires the workflow
        # from listings/pickers/dispatch + blocks new runs. Additive nullable.
        ("archived_at", "DATETIME"),
        # Origin stamp (SPEC-workflows-redesign Phase 3): "manual" (create_workflow)
        # or "draft" (apply_workflow_draft, including every multilingual variant it
        # fans out). Nullable with no server_default (like org_id / customer_id), so
        # every legacy row reads NULL = legacy/unknown (the UI renders no origin chip).
        # Mirrors the model + the alembic revision c3e9a1f7b2d5 (nullability-parity CI).
        ("created_via", "VARCHAR"),
    ],
    # Multi-org SaaS groundwork (P0): org_id on the 6 root-owned tables (Project +
    # User already have it). Nullable; _ensure_default_org backfills the Default org
    # onto existing rows. No enforcement yet — the multi_org_enabled gate is OFF.
    # Organization.active (multi-org P7): suspension flag, NOT NULL default 1
    # (active). Nothing reads it until multi_org_enabled is on — org suspension
    # blocks login / API only under the sacred gate.
    "organization": [("active", "BOOLEAN NOT NULL DEFAULT 1")],
    # Customer 360 (P4, item 5): settings/active_pairs are JSON blobs (stored as
    # TEXT on SQLite); the three defaults are nullable. All additive → existing
    # customers backfill to NULL (no settings, no pairs, no defaults) which is
    # byte-identical to the pre-P4 bare-name behaviour.
    "customer": [
        ("org_id", "INTEGER"),
        ("settings", "JSON"),
        ("active_pairs", "JSON"),
        ("default_provider", "VARCHAR"),
        ("default_workflow_id", "INTEGER"),
        ("default_segmentation_profile_id", "INTEGER"),
        # Content types (SPEC-content-types): the customer's default content
        # type, inherited at project creation (same inherit-at-create pattern
        # as default_provider). NULL = no default; existing rows backfill NULL
        # = byte-identical to today.
        ("default_content_type_id", "INTEGER"),
        # Archive/retire (SPEC-customer-archive): NULL = ACTIVE. A non-NULL
        # timestamp retires the customer from every picker + the default
        # list view (existing projects/connections keep working). Existing
        # rows backfill NULL = active = byte-identical to today.
        ("archived_at", "DATETIME"),
        # AI opt-out for every project of the customer (HECVAT DPAI-08;
        # alembic e4b7c1a9d2f6). 0 = AI allowed.
        ("ai_opt_out", "BOOLEAN NOT NULL DEFAULT 0"),
        # MT formality default per target language (#617 item 7; alembic b8e2f4a6c1d3).
        ("formality", "JSON"),
    ],
    # Connection.customer_id (P4, item 5): NULL = global/unassigned. Existing rows
    # backfill to NULL and stay global — the connectors list never hides them.
    # Connection.content_type_id (SPEC-content-types phase 3, §6): the default content
    # type copied onto the auto-created sync project. NULL = unset (byte-identical).
    "connection": [
        ("org_id", "INTEGER"),
        ("customer_id", "INTEGER"),
        ("content_type_id", "INTEGER"),
    ],
    "languageset": [("org_id", "INTEGER")],
    "routingrule": [("org_id", "INTEGER")],
    "departmentbudget": [("org_id", "INTEGER")],
    # Multi-org P8 pre-gate cleanup: org_id on PromptTemplate closes the flagged
    # cross-org leak (prompt templates were app-global). Nullable; backfilled onto
    # the Default org by _ensure_default_org. Inert single-tenant (the gate is off).
    "prompttemplate": [("org_id", "INTEGER")],
    # Connector webhook dedup settlement (D4 audit): processed_at marks an event
    # SETTLED (accepted-and-recorded) independent of job_id, so a button-trigger
    # event (PENDING recorded, no job) still dedups its replays. Nullable; the
    # one-time backfill below stamps received_at onto pre-existing rows (they were
    # already accepted). New crash-window rows stay NULL = retryable — so the
    # backfill runs ONLY at column-add time, never on a later boot.
    "webhookevent": [("processed_at", "DATETIME")],
    # Wave 2 LQA attribution (SPEC-wave2-qa-pe-reporting §3.1.9, M9). SIX additive
    # nullable columns on the EXISTING lqaresult table — the table had no shim key
    # before. All NULLABLE, NO server default: NULL is the honest "written before Wave 2"
    # value and every report buckets it as `(untracked ...)`. Bare SQL types match the
    # alembic revision e7a1b2c3d4e5 exactly; the FK asymmetry of §2.8 applies (a shim
    # INTEGER carries no REFERENCES clause — accepted, invisible to the nullability-parity
    # guard, pre-existing across this file e.g. project.org_id). SegmentPostEdit is a NEW
    # TABLE and needs NO entry here (create_all() makes it — the LLMemory precedent).
    #
    # Wave 3 (SPEC-wave3-collaboration §6.1) adds TWO more nullable columns to the same
    # existing table — `actor` (the reviewer's email on a human finding; NULL on every
    # machine row) and `withdrawn_at` (soft-delete timestamp). Bare SQL types match the
    # alembic revision b4d9f2c7e1a3 exactly. The two NEW tables (querythread / querypost)
    # need NO entry here — create_all() makes them (the SegmentPostEdit precedent).
    "lqaresult": [
        ("target_locale", "VARCHAR"),
        ("finding_source", "VARCHAR"),
        ("judge", "VARCHAR"),
        ("scoring_id", "VARCHAR"),
        ("vendor_id", "INTEGER"),
        ("dispatch_id", "INTEGER"),
        ("actor", "VARCHAR"),
        ("withdrawn_at", "DATETIME"),
    ],
    # SPEC-llmemory-phase2 §5.6a: three NULLABLE/blank-default columns on the EXISTING
    # memoryentry table (the janitor suppression marker + the human-adjudication marker).
    # New COLUMNS on an existing table -> both an alembic revision AND this shim, so a
    # non-alembic SQLite boot backfills them identically (byte-identical: suppressed_until/
    # adjudicated_at NULL, adjudicated_by ''). The three memory* TABLES themselves are made
    # by create_all() (the b3e7c1d94a26 precedent), so no table entry here -- only columns.
    "memoryentry": [
        ("suppressed_until", "DATETIME"),
        ("adjudicated_at", "DATETIME"),
        ("adjudicated_by", "VARCHAR DEFAULT ''"),
    ],
    # SPEC-advanced-workflows P1 polish: two columns on the EXISTING advancedstagedtarget
    # table carrying a delivered target's provenance as STRUCTURED fields (they were packed
    # into model_used, which then rendered where a model id belongs). Mirrors the model
    # (models.AdvancedStagedTarget) + the alembic revision b4e1d7c2a9f3 exactly, so the
    # legacy-shape SQLite boot (tables present, no alembic_version) backfills them
    # identically: vision NULL (= unrecorded on a pre-existing row), adjudicated 0. The four
    # advanced* TABLES themselves are made by create_all(), so only columns live here.
    "advancedstagedtarget": [
        ("vision", "VARCHAR"),
        ("adjudicated", "BOOLEAN NOT NULL DEFAULT 0"),
        # SPEC-quality-lenses-p2 §5.2 (T3): stage-then-gate. quality.judge stamps the
        # evaluation id + the blocked flag onto the staged row; deliver skips a blocked row.
        # Mirrors models.AdvancedStagedTarget + the alembic revision f2c8a4e6b1d9, so the
        # legacy-shape SQLite boot backfills them identically (quality_evaluation_id NULL =
        # never judged, quality_blocked 0 = not blocked). Byte-identical to a pre-T3 row.
        ("quality_evaluation_id", "VARCHAR"),
        ("quality_blocked", "BOOLEAN NOT NULL DEFAULT 0"),
        # Advanced Workflows P3 T2 (SPEC §8): a DOCUMENT unit's SOFT reference to its Segment
        # (a BARE integer, NO FK -- the run's staged table stays outside every content cascade)
        # plus the sha256 of the source the delegate translated (deliver re-checks the binding).
        # Mirrors models.AdvancedStagedTarget + the alembic revision b9e3d7c1f5a2, so the
        # legacy-shape SQLite boot backfills them identically (both NULL on a pre-T2 row). The
        # segment_id index is carried in _INDEXES so all three schema paths agree.
        ("segment_id", "INTEGER"),
        ("source_hash", "VARCHAR"),
        # Advanced Workflows P3 T3 (SPEC §8 FULL_TEXT): a FULL_TEXT paragraph unit's 0-based
        # position in Document.skeleton["units"] (the binding + reassembly order + the _deliver
        # dispatch signal). A BARE nullable integer, NO FK and NO index (nothing queries by it;
        # NOT in _INDEXES). Mirrors models.AdvancedStagedTarget + alembic c3d5f8a1b6e4; a
        # legacy-shape SQLite boot backfills NULL = keys / SEGMENTED / pre-T3 = byte-identical.
        ("paragraph_index", "INTEGER"),
        # Advanced fix bundle (BACKLOG 2026-09-03 "deliver ignores key_ids / namespace"):
        # the resolved StringKey id target.propose stamps so deliver resolves by id, not by a
        # default-namespace NAME lookup. NULL = none carried (fall back to namespace::name).
        # Advanced fix bundle (BACKLOG 2026-09-01 orphan sweep): abandoned marks a dead run's
        # staged rows so they can never deliver. Both mirror models.AdvancedStagedTarget + the
        # alembic revision d5a2f8c19e4b; a legacy-shape SQLite boot backfills key_id NULL /
        # abandoned 0 = byte-identical to a pre-fix row.
        ("key_id", "INTEGER"),
        ("abandoned", "BOOLEAN NOT NULL DEFAULT 0"),
        # round-3 L2 (document-erasure reap): the instant a document/key erasure severed this
        # row's content, so deliver skips it (a severed keys row would otherwise resolve by NAME
        # after a delete+restore and deliver an empty target). Mirrors models.AdvancedStagedTarget
        # + alembic revision a7d4e1c9b2f6; a legacy-shape SQLite boot backfills NULL = never
        # erased = byte-identical to a pre-fix row.
        ("erased_at", "DATETIME"),
        # Advanced fix bundle round 1 (H1/M3): the unit's recorded namespace, so deliver
        # resolves the exact NAME in that namespace (then the default) without ever splitting
        # unit_key on `::`. Mirrors models.AdvancedStagedTarget + alembic d5a2f8c19e4b; a
        # legacy-shape SQLite boot backfills '' = the default namespace = byte-identical.
        ("namespace", "VARCHAR NOT NULL DEFAULT ''"),
    ],
    # SPEC-quality-lenses-p2 §5.1 (T3): the Quality Lens that gates a mission's deliveries.
    # NULL = NO lens = today's behavior (P2-D14): a pre-T3 mission runs ungated. Mirrors
    # models.AdvancedMission.lens_id + the alembic revision f2c8a4e6b1d9. The qualitylens/
    # qualityfinding/quality* TABLES are made by create_all() (the T1 e4b1c7d92a6f precedent),
    # so only the mission COLUMN lives here.
    "advancedmission": [
        ("lens_id", "INTEGER"),
        # Advanced fix bundle round 1 (M2): the explicitly-requested fences (JSON), so
        # mission_view re-derives the clamp notices on every read (durable across all four
        # surfaces + the plan transcript). Mirrors models.AdvancedMission.requested_fences +
        # alembic d5a2f8c19e4b; a legacy-shape SQLite boot backfills '{}' = no explicit request.
        ("requested_fences", "JSON NOT NULL DEFAULT '{}'"),
    ],
    # Advanced fix bundle (BACKLOG 2026-09-01 "orphaned run recovery gap"): a liveness stamp the
    # harness loop touches each iteration + tool call, so the periodic sweep can tell a live
    # RUNNING run from a dead one. Mirrors models.AdvancedRun.heartbeat_at + the alembic revision
    # d5a2f8c19e4b; a legacy-shape SQLite boot backfills NULL = fall back to started_at.
    "advancedrun": [
        ("heartbeat_at", "DATETIME"),
    ],
    # AI opt-out (HECVAT DPAI-08; alembic e4b7c1a9d2f6): a Lab snapshot's source project and
    # customer, bare provenance integers. Backfilled once below from the source document
    # where it still exists and was not created after the dataset (no id-reuse misattribution).
    "evaldataset": [
        ("source_project_id", "INTEGER"),
        ("source_customer_id", "INTEGER"),
    ],
}


# Nullability self-heal: columns the MODEL declares Optional (nullable) that a legacy
# real-world DB still has as NOT NULL. Such a DB was created by an OLD create_all() and
# is kept in sync ONLY by this shim (never by Alembic — it has no alembic_version). The
# shim's _ensure_columns can ADD a column but SQLite CANNOT ALTER an existing column's
# nullability in place, so the Alembic revision that widened one (d2e6b8c4a1f7 made
# glossaryentry.project_id NULLABLE for module-owned entries) never reached such a DB.
# Consequence on that DB: the FIRST module-owned glossary term add
# (glossary_module.add_term -> gloss_engine.add_term(project_id=None)) dies with
#   IntegrityError: NOT NULL constraint failed: glossaryentry.project_id
# even though the model (models.GlossaryEntry.project_id: Optional[int]) is nullable.
#
# Declarative like _ADDED_COLUMNS so a future widening is a one-line addition. For each
# (table -> [columns]) each listed column is made NULLABLE via a faithful SQLite table
# rebuild that preserves EVERYTHING else: all other columns (names/types/notnull/
# defaults), the primary key, every foreign key, all rows (ids preserved), and every
# index. IDEMPOTENT: a column already nullable is skipped with NO rebuild, so a fresh
# create_all() DB (where the model's Optional already made it nullable) is a pure no-op
# — the fresh-create / CI path is byte-identical. Only ever WIDENS (NOT NULL -> NULL),
# never the reverse, so it can never reject data that already lives in the table.
#: The ONE backfill of evaldataset.source_project_id / source_customer_id, shared by the
#: SQLite shim above and mirrored verbatim in alembic e4b7c1a9d2f6. Only a document that
#: still exists AND predates the dataset is trusted (a later document with the same id is
#: an unrelated reuse); an unresolvable row stays NULL and the opt-out check fails closed.
_EVALDATASET_SOURCE_COLUMNS = ("source_project_id", "source_customer_id")

_EVALDATASET_SOURCE_BACKFILL_SQL = (
    "UPDATE evaldataset SET "
    "source_project_id = (SELECT d.project_id FROM document d "
    "WHERE d.id = evaldataset.source_document_id "
    "AND d.created_at <= evaldataset.created_at), "
    "source_customer_id = (SELECT p.customer_id FROM document d "
    "JOIN project p ON p.id = d.project_id "
    "WHERE d.id = evaldataset.source_document_id "
    "AND d.created_at <= evaldataset.created_at) "
    "WHERE source_document_id IS NOT NULL AND source_project_id IS NULL"
)


_NULLABLE_FIXES = {
    "glossaryentry": ["project_id"],
}


def _build_nullable_create_sql(tmp_name, info_rows, fk_rows, nullable_cols) -> str:
    """Reconstruct a CREATE TABLE statement (under `tmp_name`) from the reflected
    PRAGMA table_info / foreign_key_list of the CURRENT table, with every column named
    in `nullable_cols` rendered WITHOUT its NOT NULL. Faithful to the live table (it
    reflects shim-added columns like glossaryentry.glossary_id, real types, defaults,
    the PK, and all FKs incl. ON DELETE/UPDATE) rather than hardcoding DDL.

    table_info rows: (cid, name, type, notnull, dflt_value, pk)
    fk_list  rows:   (id, seq, ref_table, from_col, to_col, on_update, on_delete, match)
    """
    from collections import defaultdict

    col_defs = []
    for _cid, name, ctype, notnull, dflt, _pk in info_rows:
        piece = f'"{name}" {ctype}' if ctype else f'"{name}"'
        if notnull and name not in nullable_cols:
            piece += " NOT NULL"
        if dflt is not None:
            # dflt_value is already a valid SQL literal token as it appears in the DDL
            # ('in_translation', 0, ''), so splice it back verbatim.
            piece += f" DEFAULT {dflt}"
        col_defs.append(piece)

    pk_cols = [r[1] for r in sorted((r for r in info_rows if r[5]), key=lambda r: r[5])]
    if pk_cols:
        col_defs.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk_cols) + ")")

    groups = defaultdict(list)
    for fkid, seq, ref_table, frm, to, on_update, on_delete, _match in fk_rows:
        groups[fkid].append((seq, frm, to, ref_table, on_update, on_delete))
    for fkid in sorted(groups):
        cols = sorted(groups[fkid])  # order composite FK by seq
        from_cols = ", ".join(f'"{c[1]}"' for c in cols)
        to_cols = ", ".join(f'"{c[2]}"' for c in cols)
        ref_table, on_update, on_delete = cols[0][3], cols[0][4], cols[0][5]
        piece = f'FOREIGN KEY({from_cols}) REFERENCES "{ref_table}" ({to_cols})'
        if on_delete and on_delete != "NO ACTION":
            piece += f" ON DELETE {on_delete}"
        if on_update and on_update != "NO ACTION":
            piece += f" ON UPDATE {on_update}"
        col_defs.append(piece)

    return f'CREATE TABLE "{tmp_name}" (\n  ' + ",\n  ".join(col_defs) + "\n)"


def _rebuild_table_nullable(table, nullable_cols) -> None:
    """Widen `nullable_cols` on `table` to NULLABLE via the canonical SQLite table
    rebuild (https://sqlite.org/lang_altertable.html#otheralter), preserving all data,
    indexes, and FKs.

    Correctness on the FK-enforcing engine: the app sets PRAGMA foreign_keys=ON on every
    connection, but a drop/rename rebuild under enforcement can refuse or corrupt, so the
    rebuild MUST run with foreign_keys=OFF. That pragma is a NO-OP inside a transaction,
    so we drive one autocommit DBAPI connection with explicit BEGIN/COMMIT: set FK OFF
    (autocommit) -> BEGIN -> 12-step rebuild -> PRAGMA foreign_key_check (confirm no
    orphan) -> COMMIT -> restore FK ON (autocommit). The connection's isolation_level is
    forced to None so Python's DBAPI does not inject its own implicit transactions around
    our DDL. FK ON + isolation_level are ALWAYS restored (finally) before the connection
    returns to the pool, so a later request never inherits a weakened connection."""
    raw = engine.raw_connection()
    dbapi_conn = getattr(raw, "driver_connection", None) or raw.dbapi_connection
    prev_iso = getattr(dbapi_conn, "isolation_level", "")
    cur = raw.cursor()
    try:
        dbapi_conn.isolation_level = None  # explicit txn + pragma control
        cur.execute("PRAGMA foreign_keys=OFF")

        info_rows = cur.execute(f'PRAGMA table_info("{table}")').fetchall()
        fk_rows = cur.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        idx_sqls = [
            r[0]
            for r in cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL",
                (table,),
            ).fetchall()
        ]
        colnames = ", ".join(f'"{r[1]}"' for r in info_rows)
        tmp = f"_loctms_null_rebuild_{table}"
        create_sql = _build_nullable_create_sql(tmp, info_rows, fk_rows, nullable_cols)

        cur.execute("BEGIN")
        try:
            cur.execute(f'DROP TABLE IF EXISTS "{tmp}"')  # clear any prior crashed run
            cur.execute(create_sql)
            cur.execute(f'INSERT INTO "{tmp}" ({colnames}) SELECT {colnames} FROM "{table}"')  # noqa: S608 -- identifiers from sqlite_master/PRAGMA, quoted
            cur.execute(f'DROP TABLE "{table}"')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
            for isql in idx_sqls:  # recreate the table's own indexes
                cur.execute(isql)
            # Guard: the rebuilt table must have the SAME columns, and the targets must
            # now be nullable — else abort (roll back) rather than ship a wrong schema.
            new_info = cur.execute(f'PRAGMA table_info("{table}")').fetchall()
            new_cols = {r[1] for r in new_info}
            old_cols = {r[1] for r in info_rows}
            if new_cols != old_cols:
                raise RuntimeError(
                    f"nullability rebuild of {table} changed the column set "
                    f"{sorted(old_cols)} -> {sorted(new_cols)}"
                )
            still_notnull = {r[1] for r in new_info if r[3]} & nullable_cols
            if still_notnull:
                raise RuntimeError(
                    f"nullability rebuild of {table} left {sorted(still_notnull)} NOT NULL"
                )
            # Scoped to THIS table only: an unrelated pre-existing orphan elsewhere in
            # the DB (plausible on a real DB that was never FK-validated) must NOT abort
            # this table's heal or crash boot. We only rebuilt `table`, so only its FK
            # rows are in the blast radius; a whole-DB foreign_key_check (no argument)
            # would also flag orphans this rebuild never touched.
            violations = cur.execute(f'PRAGMA foreign_key_check("{table}")').fetchall()
            if violations:
                raise RuntimeError(
                    f"foreign_key_check failed after nullability rebuild of {table}: {violations}"
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        log.info(
            "healed nullability on %s.%s (SQLite table rebuild)",
            table,
            ", ".join(sorted(nullable_cols)),
        )
    finally:
        try:
            cur.execute("PRAGMA foreign_keys=ON")
        except Exception:
            # A pooled connection left with FK enforcement OFF is an integrity hazard.
            log.warning("could not re-enable PRAGMA foreign_keys after rebuild", exc_info=True)
        try:
            dbapi_conn.isolation_level = prev_iso
        except Exception:
            log.warning("could not restore isolation_level after rebuild", exc_info=True)
        cur.close()
        raw.close()


def _ensure_nullability() -> None:
    """SQLite self-heal for columns the model widened to nullable but that a shim-only
    (never-Alembic'd) legacy DB still holds as NOT NULL. See _NULLABLE_FIXES. Runs AFTER
    _ensure_columns (so a just-added column is present to inspect) and BEFORE
    _ensure_indexes (so any hot-path index lands on the rebuilt table, never fighting the
    drop/rename). No-op unless a target column is actually NOT NULL, so a fresh DB and an
    already-healed DB both do zero work."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table, columns in _NULLABLE_FIXES.items():
        if table not in existing:
            continue
        with engine.connect() as conn:
            info = conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
            have = {r[1] for r in info}
            notnull_now = {r[1] for r in info if r[3]}
        targets = {c for c in columns if c in have and c in notnull_now}
        if not targets:
            continue  # already nullable (or column absent) -> idempotent no-op
        try:
            _rebuild_table_nullable(table, targets)
        except Exception:
            # Non-fatal to boot (defense in depth): _rebuild_table_nullable's own
            # ROLLBACK + finally already guarantee the DB is left intact (data-safe,
            # unhealed, foreign_keys restored to ON). A crash-on-boot would be strictly
            # worse than a degraded feature, so log loudly and let init_db continue with
            # the remaining tables/steps instead of propagating.
            log.warning(
                "nullability self-heal failed for %s.%s; DB left unhealed but intact, "
                "continuing boot (feature stays degraded until fixed)",
                table,
                ", ".join(sorted(targets)),
                exc_info=True,
            )


def _backfill_auditlog_org(conn) -> None:
    """ONE-TIME backfill of auditlog.org_id for rows written before the column existed
    (#487). Best effort and honest about it: a row is stamped with its actor's CURRENT org,
    then rows written before a recorded `user.move_org` are re-stamped with the org the
    user was moved OUT of (the move row's detail is `org <old>-><new> ...`). A user with no
    recorded move and a row older than an unrecorded move cannot be recovered. System and
    unauthenticated actors (no account) stay NULL = never shown to an org. Mirrors the
    alembic revision."""
    import re

    conn.execute(
        text(
            'UPDATE auditlog SET org_id = (SELECT u.org_id FROM "user" u '
            "WHERE lower(u.email) = lower(auditlog.actor) LIMIT 1) WHERE org_id IS NULL"
        )
    )
    moves = conn.execute(
        text("SELECT id, target, detail FROM auditlog WHERE action = 'user.move_org' ORDER BY id")
    ).fetchall()
    previous = {}
    for move_id, target, detail in moves:
        who = (target or "").lower()
        hit = re.match(r"org (\d+|None)->", detail or "")
        lower = previous.get(who, 0)
        previous[who] = move_id
        if not who or not hit:
            continue
        old_org = None if hit.group(1) == "None" else int(hit.group(1))
        conn.execute(
            text(
                "UPDATE auditlog SET org_id = :org WHERE lower(actor) = :who "
                "AND id > :lo AND id <= :hi"
            ),
            {"org": old_org, "who": who, "lo": lower, "hi": move_id},
        )


def _backfill_inbox_keys(conn) -> None:
    """Stamp routing._inbox_key onto pre-existing inbox projects (SPEC-rename-delete).

    SQLite- and Postgres-compatible (|| concat, substr, lower, LIKE, coalesce, MIN /
    GROUP BY are all portable). The migration runs the SAME two statements on the
    Alembic/Postgres path. Only the EARLIEST id per (org bucket, key) is keyed, so the
    functional unique index can always be built even if legacy data holds two same-key
    inboxes (a pre-fix rename-fork) — the loser keeps inbox_key NULL (an ordinary
    project). Idempotent (re-running sets the same value)."""
    # Department inboxes: "Inbox: <dept>" -> "dept:<lower(dept)>" ("Inbox: " == 7 chars).
    conn.execute(
        text(
            "UPDATE project SET inbox_key = 'dept:' || lower(substr(name, 8)) "
            "WHERE name LIKE 'Inbox: %' AND inbox_key IS NULL AND id IN ("
            "  SELECT MIN(id) FROM project WHERE name LIKE 'Inbox: %' "
            "  GROUP BY coalesce(org_id, -1), lower(substr(name, 8)))"
        )
    )
    # Per-actor inboxes: "MCP Inbox (<actor>)" -> "actor:<created_by>".
    conn.execute(
        text(
            "UPDATE project SET inbox_key = 'actor:' || created_by "
            "WHERE name LIKE 'MCP Inbox (%)' AND inbox_key IS NULL AND id IN ("
            "  SELECT MIN(id) FROM project WHERE name LIKE 'MCP Inbox (%)' "
            "  GROUP BY coalesce(org_id, -1), created_by)"
        )
    )


def _ensure_columns() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.connect() as conn:
        evaldataset_source_added = False
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # table doesn't exist yet; create_all will make it fresh
            have = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    if table == "project" and name == "inbox_key":
                        # ONE-TIME backfill (SPEC-rename-delete inbox re-key): stamp the
                        # stable inbox_key onto pre-existing inbox projects so the
                        # re-keyed lookup finds them instead of forking a new inbox.
                        # Reproduces routing._inbox_key from the historical name/creator:
                        #   "Inbox: <dept>"       -> "dept:<lower>"
                        #   "MCP Inbox (<actor>)" -> "actor:<created_by>"
                        # Only the EARLIEST row per (org bucket, key) is keyed so the
                        # functional unique index below can always be built even if a
                        # pre-fix rename-fork left two same-key inboxes (duplicates keep
                        # inbox_key NULL and behave as ordinary projects). Runs ONLY in
                        # this just-added branch, so a project renamed/created AFTER the
                        # migration is never retro-keyed on a later boot.
                        _backfill_inbox_keys(conn)
                    elif table == "auditlog" and name == "org_id":
                        # ONE-TIME backfill (#487), only in this just-added branch.
                        _backfill_auditlog_org(conn)
                    elif table == "webhookevent" and name == "processed_at":
                        # ONE-TIME backfill (D4): pre-existing ledger rows were
                        # ALREADY accepted, so they are SETTLED (processed_at =
                        # received_at). Runs ONLY in this just-added branch, so a
                        # crash-window row created AFTER the migration (processed_at
                        # NULL) is never wrongly settled on a later boot.
                        conn.execute(
                            text(
                                "UPDATE webhookevent SET processed_at = received_at "
                                "WHERE processed_at IS NULL"
                            )
                        )
                    elif table == "user" and name == "onboarding_dismissed_at":
                        # ONE-TIME backfill (SPEC-onboarding-tour Phase 0): EVERY
                        # pre-existing user boots DISMISSED so no live account is shown
                        # the first-run tour uninvited (NULL would read as ELIGIBLE).
                        # Mirrors the alembic revision's UPDATE. Runs ONLY in this
                        # just-added branch, so a user created AFTER the column is added
                        # keeps NULL = eligible and is never retro-dismissed on a later
                        # boot. COALESCE keeps a real "when" for analytics.
                        conn.execute(
                            text(
                                'UPDATE "user" SET onboarding_dismissed_at = '
                                "COALESCE(last_login, created_at, CURRENT_TIMESTAMP) "
                                "WHERE onboarding_dismissed_at IS NULL"
                            )
                        )
                    elif table == "evaldataset" and name in _EVALDATASET_SOURCE_COLUMNS:
                        # The backfill writes BOTH columns, so it runs once AFTER this
                        # table's loop, when both exist (gate round 4: running it on the
                        # first column crashed init_db on a legacy DB).
                        evaldataset_source_added = True
                    elif table == "user" and name == "password_changed_at":
                        # ONE-TIME backfill (password policy): every existing local
                        # password's max-age clock starts NOW, never retroactively.
                        # Mirrors the alembic revision e5a1c8d3f7b2's UPDATE.
                        conn.execute(
                            text(
                                'UPDATE "user" SET password_changed_at = CURRENT_TIMESTAMP '
                                "WHERE password_changed_at IS NULL "
                                "AND password_hash IS NOT NULL AND password_hash <> ''"
                            )
                        )
        if evaldataset_source_added:
            # ONE-TIME backfill (AI opt-out): mirrors alembic e4b7c1a9d2f6. Both
            # evaldataset source columns exist by now, whichever of them was just added.
            conn.execute(text(_EVALDATASET_SOURCE_BACKFILL_SQL))
        conn.commit()


# Hot-path indexes create_all() won't add to tables that already exist.
# TM lookup filters on (project, locale pair) before hashing/fuzzy-scoring;
# every segment fetch filters on (document, locale) and sorts by order_index.
_INDEXES = [
    ("ix_workflowrun_status_id", "workflowrun", "(status, id)"),
    ("ix_workflowrun_batch_id_status", "workflowrun", "(batch_id, status)"),
    ("ix_segmenttm_lookup", "segmenttm", "(project_id, source_locale, target_locale, source_hash)"),
    (
        "ix_fulltexttm_lookup",
        "fulltexttm",
        "(project_id, source_locale, target_locale, source_hash)",
    ),
    ("ix_segment_doc_locale", "segment", "(document_id, target_locale, order_index)"),
    # Semantic-TM prefilter narrows to a project's locale pair before scoring
    # (the model column is filtered separately so mixed-model rows never cross-
    # compare). Not declared on the SQLModel metadata, so _INDEXES carries it on
    # the SQLite path just like the other hot-path compound indexes.
    (
        "ix_segmentembedding_lookup",
        "segmentembedding",
        "(project_id, source_locale, target_locale)",
    ),
    # SPEC-content-types phase 3: single-column indexes on the new content_type_id so
    # the added (content_type_id == X OR IS NULL) read predicate + the delete-fails-
    # closed reference count stay index-backed. Carried here (not Field(index=True)) so
    # the SQLite create_all/upgrade path and the alembic/Postgres path build the same
    # named indexes.
    ("ix_segmenttm_content_type", "segmenttm", "(content_type_id)"),
    ("ix_fulltexttm_content_type", "fulltexttm", "(content_type_id)"),
    ("ix_segmentembedding_content_type", "segmentembedding", "(content_type_id)"),
    ("ix_connection_content_type", "connection", "(content_type_id)"),
    # Key-based projects: the key -> its value rows lookup (every grid render, every
    # export, every auto-unverify sweep). Carried here because `segment` already
    # exists on a legacy DB, so create_all()'s Field(index=True) never reaches it.
    ("ix_segment_key_id", "segment", "(key_id)"),
    # SCIM 2.0 (Train I): the `externalId eq "..."` filter lookup + the group
    # externalId lookup. Carried here (not only Field(index=True)) so a legacy `user`
    # table that predates the column still gets the index on the create_all/upgrade
    # path, matching the alembic revision.
    ("ix_user_external_id", "user", "(external_id)"),
    # OIDC subject binding (identity-01): the (issuer, sub) lookup at every OIDC sign-in.
    ("ix_user_sso_subject", "user", "(sso_subject)"),
    # #487: audit rows are confined to an org by this stamped column.
    ("ix_auditlog_org_id", "auditlog", "(org_id)"),
    ("ix_usergroup_external_id", "usergroup", "(external_id)"),
    # Advanced Workflows P3 T2 (SPEC §8): the DOCUMENT unit's segment_id lookup. Carried here
    # (not only Field(index=True)) so the legacy-shape SQLite boot path (advancedstagedtarget
    # already present) lands the index too, matching the alembic revision b9e3d7c1f5a2 and the
    # model's index=True -- three-way agreement (the ix_segment_doc_locale precedent).
    ("ix_advancedstagedtarget_segment_id", "advancedstagedtarget", "(segment_id)"),
]

# UNIQUE indexes carried on the SQLite init_db path (the Alembic baseline builds
# the same ones on Postgres). Kept separate from _INDEXES because they use CREATE
# UNIQUE INDEX. ux_segmentembedding_segtm_model enforces one embedding vector per
# (SegmentTM row, model) so a spammed backfill can't write duplicates — the app's
# upsert_embedding is delete-then-insert + IntegrityError-tolerant on top.
_UNIQUE_INDEXES = [
    ("ux_segmentembedding_segtm_model", "segmentembedding", "(segment_tm_id, model)"),
    # Connector webhook idempotency (CONNECTOR_FRAMEWORK_DESIGN §3.4): one accepted
    # event per (connection, event id) so a provider RETRY is deduped. Unique so a
    # concurrent double-delivery can't both insert + both enqueue — the second
    # INSERT hits IntegrityError and is treated as a dup (webhooks.record_event).
    ("ux_webhookevent_conn_event", "webhookevent", "(connection_id, event_id)"),
    # T3 connector id map (SPEC-t3-notion-helpscout): exactly ONE row per
    # (connection, source doc, target locale) so a resumable multi-batch page
    # build can never create two target pages for one localized document. Unique
    # so a concurrent double-run (server + CLI, or two workers) can't both INSERT a
    # reserve row — the second hits IntegrityError and re-reads the winner's row
    # (connectors.idmap.reserve), the cross-process backstop to the in-process lock.
    (
        "ux_connectoridmap_conn_src_loc",
        "connectoridmap",
        "(connection_id, source_id, target_locale)",
    ),
    # Inbox re-key (SPEC-rename-delete): at most ONE inbox project per (org bucket,
    # inbox_key). FUNCTIONAL (coalesce collapses NULL org so gate-off inboxes share one
    # bucket) + NULL-tolerant (ordinary projects carry inbox_key NULL, which SQL keeps
    # DISTINCT so they never collide). Matches the model __table_args__ index + the
    # alembic revision so all three build paths agree on ux_project_inbox_key.
    ("ux_project_inbox_key", "project", "(coalesce(org_id, -1), inbox_key)"),
    # Key-based projects (SPEC-key-based-projects 2.2/2.4). BOTH are PARTIAL, and the
    # WHERE clause rides in the column string because these tuples are pasted straight
    # into `CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} {cols}`.
    #   * ux_stringkey_identity: the un-refactorable identity tuple; partial on
    #     deleted_at IS NULL so a soft-deleted key never blocks re-creating its name.
    #   * ux_segment_key_locale_form: one value row per (key, locale, plural form);
    #     partial on key_id IS NOT NULL so DOCUMENT segments are not indexed at all
    #     (they keep ux_segment_doc_locale_order unchanged) -- which is also why this
    #     is safe to create unconditionally on a legacy DB: zero rows qualify.
    (
        "ux_stringkey_identity",
        "stringkey",
        "(project_id, coalesce(branch_id, 0), namespace, name, context) WHERE deleted_at IS NULL",
    ),
    (
        "ux_segment_key_locale_form",
        "segment",
        "(key_id, target_locale, coalesce(plural_category, '')) WHERE key_id IS NOT NULL",
    ),
    # Vendor<->customer link (PROPOSAL-external-processing-step Wave A): at most ONE
    # link row per (vendor, customer) so a double link-customer is a no-op, never a
    # duplicate. Unique so a concurrent double-link (web + CLI) can't both INSERT --
    # the second hits IntegrityError and is treated as already-linked
    # (vendors.link_customer). Matches the alembic revision so all three build paths
    # agree on ux_vendorcustomer_vendor_customer.
    ("ux_vendorcustomer_vendor_customer", "vendorcustomer", "(vendor_id, customer_id)"),
    # TM write-back uniqueness (QA Part-2 P-C2): exactly ONE SegmentTM row per
    # (project_id, source_hash, source_locale, target_locale) -- the tuple tm.write_back
    # read-checks on and import_tmx's existing_by_hash preload is scoped by. Backs the
    # IntegrityError-tolerant upsert so a concurrent multi-process writer (server + CLI
    # approving the same source) can't create duplicate TM rows -> nondeterministic
    # tm.lookup. source_hash already encodes the locale pair + normalized source, so the
    # locale columns are redundant but mirror both read-checks. Matches alembic revision
    # c2f1a7d4e9b6 so both build paths agree on ux_segmenttm_upsert_key. Guarded by a
    # pre-existing-dup check below (skip-with-warning, like segmentembedding).
    (
        "ux_segmenttm_upsert_key",
        "segmenttm",
        "(project_id, source_hash, source_locale, target_locale)",
    ),
    # SCIM 2.0 groups (Train I): one UserGroup per (org bucket, name). FUNCTIONAL
    # (coalesce collapses NULL org so gate-off groups share one bucket) — the
    # ux_project_inbox_key precedent. Matches the alembic revision so all three build
    # paths agree on ux_usergroup_org_name.
    ("ux_usergroup_org_name", "usergroup", "(coalesce(org_id, -1), name)"),
    # One membership edge per (group, user) so a repeated SCIM `add` PATCH is idempotent
    # (a concurrent double-add hits IntegrityError and is treated as already-a-member).
    ("ux_usergroupmember_group_user", "usergroupmember", "(group_id, user_id)"),
    # Identity uniqueness (#74): exactly ONE account per NORMALIZED address. FUNCTIONAL
    # over lower(email) because the app's identity law IS the normalized address --
    # security.get_by_email lowercases its needle, so 'Bob@x' and 'bob@x' are already one
    # identity to every reader, and a plain UNIQUE(email) would accept both and make
    # every by-email `.first()` a coin flip. Matches alembic revision a1f4c8b2d6e9 so
    # both build paths agree on ux_user_email_lower. Guarded by a pre-existing-dup check
    # below (skip-with-warning, like segmenttm): the shim path cannot run the migration's
    # normalize+tombstone dedupe, and init_db must not crash on a legacy DB.
    ("ux_user_email_lower", "user", "(lower(email))"),
]


def _sql_table(name: str) -> str:
    """Spell a table name the way RAW SQL must name it on EVERY supported backend.

    `user` is a reserved word in Postgres (USER = current_user), so an unquoted
    `FROM user` / `UPDATE user` / `ON user (...)` is a syntax error there while
    SQLite silently tolerates it (#494: the org-confinement check broke every
    Postgres boot). The rule was written down in alembic revision
    a1f4c8b2d6e9 and _clear_dormant_superadmin obeys it with a literal `"user"`;
    this is the same rule for the places where the name is interpolated, so the
    index/backfill builders cannot re-introduce the unquoted spelling."""
    return '"user"' if name == "user" else name


def _ensure_indexes() -> None:
    with engine.connect() as conn:
        for name, table, cols in _INDEXES:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {_sql_table(table)} {cols}"))
        # (document_id, target_locale, order_index) is unique by construction in
        # every ingest path; enforce it so a future concurrent path can't silently
        # double-insert. Skipped (with a warning) if legacy data already violates it.
        dups = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT 1 FROM segment "
                "GROUP BY document_id, target_locale, order_index HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if dups:
            log.warning(
                "segment table has %s duplicate (doc, locale, order) groups; "
                "unique index not created",
                dups,
            )
        else:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_doc_locale_order "
                    "ON segment (document_id, target_locale, order_index)"
                )
            )

        # Semantic-TM uniqueness: one vector per (segment_tm_id, model). Guarded
        # the same way — if a legacy backfill left duplicate rows, skip (warn)
        # rather than fail init_db; a re-backfill (delete-then-insert) heals them.
        emb_dups = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT 1 FROM segmentembedding "
                "GROUP BY segment_tm_id, model HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if emb_dups:
            log.warning(
                "segmentembedding has %s duplicate (segment_tm_id, model) "
                "groups; unique index not created",
                emb_dups,
            )
        else:
            for name, table_, cols in _UNIQUE_INDEXES:
                if table_ == "segmentembedding":
                    conn.execute(
                        text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table_} {cols}")
                    )

        # TM write-back uniqueness (P-C2): ux_segmenttm_upsert_key. Guarded the same way
        # as the two above -- a legacy DB with pre-fix duplicate rows would fail the
        # CREATE UNIQUE INDEX, so skip-with-warning rather than crash init_db (the alembic
        # authority path DEdups newest-wins then adds it; a legacy shim DB is healed by a
        # rebuild-under-alembic). Scoped to project_id IS NOT NULL to match exactly the
        # groups a standard UNIQUE index rejects (SQL keeps NULLs DISTINCT).
        segtm_dups = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT 1 FROM segmenttm WHERE project_id IS NOT NULL "
                "GROUP BY project_id, source_hash, source_locale, target_locale "
                "HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if segtm_dups:
            log.warning(
                "segmenttm has %s duplicate (project, source_hash, locale-pair) "
                "groups; unique index not created (rebuild under alembic to "
                "dedup + enforce)",
                segtm_dups,
            )
        else:
            for name, table_, cols in _UNIQUE_INDEXES:
                if table_ == "segmenttm":
                    conn.execute(
                        text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table_} {cols}")
                    )
        # WebhookEvent's + ConnectorIdMap's unique indexes are independent of the
        # segmentembedding dup guard above — create them unconditionally (both
        # tables are fresh here, no legacy rows to violate them). usergroup +
        # usergroupmember (Train I) join this loop for the SAME reason — they are NEW
        # tables (create_all makes them fresh, no legacy rows to violate), so all three
        # build paths (SQLite create_all/upgrade + alembic) actually agree on
        # ux_usergroup_org_name (functional, like ux_project_inbox_key) and
        # ux_usergroupmember_group_user. Without this, the SQLite path left per-org group
        # names + membership NON-unique and create_group's IntegrityError->409 was dead code.
        for name, table_, cols in _UNIQUE_INDEXES:
            if table_ in (
                "webhookevent",
                "connectoridmap",
                "project",
                "stringkey",
                "segment",
                "usergroup",
                "usergroupmember",
            ):
                conn.execute(
                    text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {_sql_table(table_)} {cols}")
                )
        # #74 ux_user_email_lower: same skip-with-warning shape as segmenttm above. The
        # `user` table is NOT fresh on this path (a legacy shim DB can already hold two
        # rows for one normalized address), and only alembic revision a1f4c8b2d6e9 can
        # normalize + tombstone them -- so on a DB that still has duplicates we log and
        # leave the index off rather than crash init_db.
        user_dups = conn.execute(
            text(
                'SELECT COUNT(*) FROM (SELECT 1 FROM "user" '
                "GROUP BY lower(trim(email)) HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if user_dups:
            log.warning(
                "#74: the user table has %s duplicate normalized-email group(s); the "
                "ux_user_email_lower unique index was NOT created (rebuild under alembic "
                "to normalize + dedupe + enforce). By-email lookups stay ambiguous until "
                "then.",
                user_dups,
            )
        else:
            for name, table_, cols in _UNIQUE_INDEXES:
                if table_ == "user":
                    conn.execute(
                        text(
                            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                            f"ON {_sql_table(table_)} {cols}"
                        )
                    )
        conn.commit()


def _fixup_retired_org_name() -> None:
    """Idempotent one-time data fixup (SPEC-brand-as-config H3): before the brand-as-config
    train, a blank Settings save persisted the LITERAL old product name into `org_name`
    (settings_save wrote `org_name.strip() or "Loc Studio"`), so an EXISTING install would
    keep the retired name in its app-shell header forever. Clear `org_name` when it equals a
    retired product name (case-insensitively) — it then falls back to app.branding.org_name()
    (the live brand). Runs on every boot; a no-op once cleared or when the value is a real
    customer org name. Direct SQL (no settings service / cache) keeps it boot-safe, and any
    failure is swallowed so a fixup can never block startup."""
    _retired = {"loc studio", "localization studio"}
    cleared = False
    try:
        with engine.begin() as conn:
            row = conn.execute(text("SELECT value FROM setting WHERE key = 'org_name'")).fetchone()
            cleared = row is not None and str(row[0] or "").strip().lower() in _retired
            if cleared:
                conn.execute(text("DELETE FROM setting WHERE key = 'org_name'"))
                log.warning(
                    "init: cleared retired org_name %r -> now falls back to the product brand",
                    row[0],
                )
        if cleared:
            # L-a: the direct-SQL DELETE bypasses the settings cache/version, so bump the
            # cross-process version (outside the transaction) — any warm process that cached
            # the retired value re-reads on its next probe instead of serving it stale.
            from app.services import settings as _settings

            _settings.bump_version()
    except Exception:
        log.exception("init: retired org_name fixup skipped (non-fatal)")


def _clear_dormant_superadmin() -> None:
    """Clear the dormant `is_superadmin` flag on every boot, on EVERY backend.

    The back-office and the super-admin account were removed 2026-09-20. The column stays
    (a destructive migration is the worse risk, and this repo has been bitten by migrations
    beyond ADD COLUMN on pre-rebuild installs), but the deleted P8 bootstrap had already
    FLAGGED the first admin on every install that ever booted. Dormant AND SET is fail-OPEN:
    it grants nothing today and grants immediately the moment anything reads the column
    again. Fail-closed is the same column set to 0.

    GATE R3 filed the first version of this as a MEDIUM and was right. It lived inside
    `_ensure_default_org`, which `init_db` calls on the SQLite branch ONLY, so on Postgres
    the clear never ran at all while the rows it targets exist there just the same:
    `_flag_superadmin` was called from `seed_admin()`, which is backend-agnostic. The
    comment claimed the outcome unconditionally and was false on a backend `init_db`
    supports, refuses to boot un-migrated, and README documents with a compose file.

    Placed beside `_fixup_retired_org_name()`, the established every-backend boot-fixup
    precedent in this file: direct SQL (no service layer) keeps it boot-safe, it is scoped
    to rows still set so it is a no-op from the second boot onward, and any failure is
    swallowed rather than blocking startup. It logs when it actually changes data, for the
    same reason its sibling does: a boot that rewrites rows on prod should say so."""
    cleared = 0
    try:
        with engine.begin() as conn:
            # GATE R4 MED-1. The first every-backend version of this was still SQLite-only,
            # for two reasons at once, and the swallow below turned both into a log line
            # nobody would read: `user` is a RESERVED WORD in Postgres and must be quoted
            # (the repo wrote that rule down in the a1f4c8b2d6e9 migration and the other raw
            # statement against this table obeys it), and the column is sa.Boolean(), so
            # `= 0` / `= 1` are integer-against-boolean type errors there. Quoted identifier
            # plus BOUND booleans: SQLAlchemy renders 0/1 for SQLite and false/true for
            # Postgres, so one statement is correct on both and neither backend is being
            # trusted to coerce anything.
            result = conn.execute(
                text('UPDATE "user" SET is_superadmin = :cleared WHERE is_superadmin = :was_set'),
                {"cleared": False, "was_set": True},
            )
            # rowcount, not a backend-specific probe (sqlite changes() / pg ROW_COUNT):
            # this function exists because the first version was accidentally SQLite-only.
            cleared = max(result.rowcount or 0, 0)
        if cleared:
            # The flag's own NAME is deliberately not in these strings. The ratchet in
            # verify_multiorg treats a string constant naming it as a site, and it caught
            # these two when they did, which is the check behaving correctly. Rewording is
            # the right answer; adding two more entries to ALLOWED_LINES so that log prose
            # can say the word is how an exemption list grows until it stops meaning much.
            log.warning(
                "init: cleared the dormant super-admin flag on %s user row(s). The "
                "back-office and that account type were removed 2026-09-20; nothing reads "
                "the column, and leaving it SET would grant on the first future read.",
                cleared,
            )
    except Exception:
        # Still swallowed, because a boot fixup must never block startup, and its sibling
        # _fixup_retired_org_name does the same. But GATE R4 was right that the first
        # version of this arm was load-bearing in the wrong way: it turned "this statement
        # is invalid on your backend, on every boot, forever" into something that reads
        # exactly like a transient blip. The message now says the failure is PERSISTENT if
        # it repeats, and names the consequence rather than only the event.
        log.exception(
            "init: could NOT clear the dormant super-admin flag (non-fatal, startup "
            "continues). If this repeats on every boot it is not transient: the flag is "
            "still SET on this install, and it was set on every install that ever booted "
            "before 2026-09-20. Nothing reads the column today, so nothing is granted now, "
            "but it is fail-OPEN for whatever reads it next."
        )


def _ensure_default_org() -> None:
    """Every install has exactly one Organization row today ('Default'); users and
    projects are backfilled onto it. Multi-org enforcement is future SaaS-mode work —
    on-prem, the install itself is the tenant boundary."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM organization LIMIT 1")).fetchone()
        if row is None:
            conn.execute(
                text(
                    "INSERT INTO organization (name, created_at) "
                    "VALUES ('Default', CURRENT_TIMESTAMP)"
                )
            )
            row = conn.execute(text("SELECT id FROM organization LIMIT 1")).fetchone()
        org_id = row[0]
        # #73: the org-confinement DATA invariant. Runs BEFORE the blanket backfill
        # below so the org-less accounts are still visible to name in the warning --
        # the backfill has silently adopted them since P0, and under an active gate
        # that adoption is confinement-relevant and must be seen.
        _assert_org_confinement_invariant(conn, org_id)
        # Stamp the Default org onto every existing row that lacks one. Idempotent:
        # the WHERE org_id IS NULL guard means already-stamped rows are never
        # re-touched, and a fresh install (no rows) is a harmless no-op.
        for table in (
            "user",
            "project",
            "customer",
            "connection",
            "workflow",
            "languageset",
            "routingrule",
            "departmentbudget",
            "prompttemplate",
            "workflowgroup",
            "segmentationprofile",
            "projecttemplate",
            "systempromptoverride",
        ):
            conn.execute(
                # Why: the table name comes from the constant tuple above (spelled the way
                # Postgres requires via _sql_table; `user` is reserved there); the value is
                # bound.
                text(f"UPDATE {_sql_table(table)} SET org_id = :o WHERE org_id IS NULL"),  # noqa: S608
                {"o": org_id},
            )
        # CV-2 H2: a projectbudget's org is a property of its PROJECT, never the Default
        # blanket -- otherwise an org-2 project's budget stamped Default (org 1) makes the
        # org LIST surfaces disagree with enforcement. Stamp EVERY row from its project's
        # org (a correlated UPDATE, idempotent + portable across SQLite/Postgres), so
        # row.org_id == project.org_id always holds; this normalizes NULL and mismatched
        # rows alike. Cheap (few budget rows) and re-running it is a no-op.
        conn.execute(
            text(
                "UPDATE projectbudget SET org_id = "
                "(SELECT p.org_id FROM project p WHERE p.id = projectbudget.project_id)"
            )
        )
        # The P8 super-admin bootstrap backfill lived here and was REMOVED on 2026-09-20
        # with the back-office. Its entire purpose was to stop a SaaS install locking the
        # operator out of that console; with no console there is nothing to be locked out
        # of, and promoting admins to a cross-tenant role nothing reads would leave a live
        # privilege bit lying in the database for whatever reads it next.
        #
        # The `is_superadmin` COLUMN is deliberately left in place rather than dropped:
        # a destructive migration is a worse risk than a dormant column, and this repo has
        # been bitten before by migrations beyond ADD COLUMN on pre-rebuild installs.
        # Nothing in app/ may read or write it -- verify_multiorg's ratchet enforces that.
        #
        # GATE R2: but "dormant" was not enough, and the first version of this comment was
        # wrong to shrug at the existing values. The deleted bootstrap had already FLAGGED
        # the first admin on every install that ever booted, so the column was dormant AND
        # SET on prod, on the AWS demo box and locally (measured: 1 row of 11 here). A flag
        # that means nothing today but is TRUE grants immediately the moment anything reads
        # it again. That is fail-OPEN dormancy, which is the opposite of how the rest of
        # this subsystem is built.
        #
        # So it is cleared once, by _clear_dormant_superadmin() below. GATE R3 moved it OUT
        # of this function: _ensure_default_org runs on the SQLite branch of init_db only,
        # and the rows it targets exist on Postgres too, because the deleted bootstrap was
        # called from seed_admin() which is backend-agnostic. A security remediation that
        # silently skips a supported backend is not a remediation.
        conn.commit()


def _multi_org_gate_on(conn) -> bool:
    """Is the sacred multi-org gate ACTIVE, read straight off the settings table on the
    caller's own connection? Deliberately NOT access.multi_org_active(): this runs from
    init_db, before the app's service layer is guaranteed importable/usable, and a second
    engine connection during boot is exactly the kind of thing that deadlocks a SQLite
    upgrade. Mirrors the two settings that authority reads -- security.auth_required()'s
    `security_auth_required` AND `multi_org_enabled` -- defaulting to OFF if the table or
    a row is missing."""
    try:
        rows = conn.execute(
            text(
                "SELECT key, value FROM setting "
                "WHERE key IN ('security_auth_required', 'multi_org_enabled')"
            )
        ).fetchall()
    except Exception:
        return False
    vals = {r[0]: (r[1] or "") for r in rows}
    return vals.get("security_auth_required") == "1" and vals.get("multi_org_enabled") == "1"


def _assert_org_confinement_invariant(conn, org_id) -> None:
    """#73 boot invariant: NO real (non-system) user may be NULL-org while the sacred
    multi-org gate is ON, and no Organization may ever carry access.BLOCKED_ORG_ID.

    A NULL-org user under an active gate is the #73 producer: their scope used to
    resolve to None, which the whole `if org_id is not None:` read family read as
    SEE-ALL. access.current_org_id now poisons that to BLOCKED_ORG_ID so the reads fail
    closed, and this is the second half — the DATA invariant behind that predicate.

    BACKFILL, not refuse-to-boot: it is what this install already does for every other
    org-less row (the loop above stamps `user` among 12 tables from the Default org, and
    has since P0), so refusing to boot here would newly brick installs on the exact rows
    the backfill has always healed. What changes is that it is no longer SILENT — under
    an active gate the affected accounts are named in a WARNING before they are adopted,
    because adopting a user into the Default org is a confinement-relevant act an
    operator must see. Called AFTER the backfill, so the assertion is the post-condition:
    if any real NULL-org user SURVIVED it, the healing path is broken and we DO refuse to
    boot rather than serve a see-all principal.

    The BLOCKED_ORG_ID check is the by-construction pin under the poison sentinel: -1 is
    only safe as "matches no row" for as long as no org owns it (ids are positive
    autoincrement PKs, so this can only fire on a hand-edited DB)."""
    from app.services.access import BLOCKED_ORG_ID
    from app.services.security import RESERVED_ACTORS

    poisoned = conn.execute(
        text("SELECT id FROM organization WHERE id = :b"), {"b": BLOCKED_ORG_ID}
    ).fetchone()
    if poisoned is not None:
        raise RuntimeError(
            f"organization id {BLOCKED_ORG_ID} is reserved (access.BLOCKED_ORG_ID, the "
            "#73 fail-closed read sentinel) and MUST NOT exist as a real org row. "
            "Refusing to boot."
        )
    if not _multi_org_gate_on(conn):
        return

    def _real_null_org_users():
        # #494: `"user"` quoted, NOT bare -- USER is a reserved word in Postgres, and
        # this function runs on the Postgres boot branch (init_db calls it there), where
        # the unquoted spelling was a syntax error on every start. SQLite tolerates both.
        rows = conn.execute(text('SELECT id, email FROM "user" WHERE org_id IS NULL')).fetchall()
        return [r for r in rows if (r[1] or "") not in RESERVED_ACTORS]

    orphans = _real_null_org_users()
    if not orphans:
        return
    log.warning(
        "#73 CONFINEMENT: multi-org is ON and %d user row(s) carry NO org: %s. Under the "
        "gate an org-less account is a broken principal (its reads fail closed, and it "
        "cannot log in at all). Adopting them into org %s (the Default-org backfill that "
        "has always run at boot) -- review these accounts and move them to their real org.",
        len(orphans),
        ", ".join(f"#{r[0]} {r[1]}" for r in orphans),
        org_id,
    )
    conn.execute(text('UPDATE "user" SET org_id = :o WHERE org_id IS NULL'), {"o": org_id})
    conn.commit()
    still = len(_real_null_org_users())
    if still:
        raise RuntimeError(
            f"#73 CONFINEMENT: {still} user row(s) are still NULL-org after the "
            "Default-org backfill while multi-org is ON. Refusing to boot rather than "
            "serve a see-all principal."
        )


def _alembic_upgrade_head() -> None:
    """Run `alembic upgrade head` in-process against the app's OWN SQLite DB
    (config.DB_URL / config.DB_PATH). Mirrors tests/verify_migrations._alembic_upgrade_head:
    build an alembic Config from alembic.ini, point script_location + sqlalchemy.url at
    this install, and upgrade. Idempotent — a DB already at head is a no-op.

    Safe against recursion/deadlock: alembic's env.py imports only app.config + app.models
    (never app.db), so it does not re-enter init_db; it opens its OWN NullPool connection to
    the same SQLite file (WAL lets it coexist with this engine's idle pooled connections).
    The explicit sqlalchemy.url override wins in env.py, but it equals config.DB_URL anyway
    so a stamped DB and the app engine always target the same file."""
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", config.DB_URL)
    command.upgrade(cfg, "head")


def _init_sqlite_create_all() -> None:
    """The create_all + additive-shim schema bring-up. Used by (a) the hermetic TEST
    path (fast; the 141 verify suites depend on it) and (b) the legacy-DB fallback (a
    real install whose DB predates alembic). NOT the authority for a fresh/stamped real
    install — that goes through _alembic_upgrade_head so every non-additive migration
    actually reaches the DB."""
    SQLModel.metadata.create_all(engine)
    _ensure_columns()
    # After columns are backfilled, before indexes are (re)built: widen any column a
    # shim-only legacy DB still holds NOT NULL that the model declares nullable (e.g.
    # glossaryentry.project_id — the Alembic widening never reached a DB that was never
    # under Alembic). A table rebuild here means _ensure_indexes then lands its indexes on
    # the final table rather than one that's about to be dropped.
    _ensure_nullability()
    _ensure_indexes()
    _backfill_glossary_concepts()


# R5-8: the one-shot marker that records the terminology consolidation repair has completed,
# so it does not re-scan every glossary on every boot. Not a user setting (absent from
# settings.DEFAULTS), so it never appears in the branding settings-key snapshot.
_CONCEPTS_CONSOLIDATED_KEY = "glossary_concepts_consolidated"
# R6-G: the marker is VERSIONED with the repair's identity (the alembic revision that shipped
# it). A FUTURE one-time concept repair bumps this id, so a completed-earlier flag does not mask
# it — the marker is re-checked against the CURRENT repair id, not merely "present".
_CONCEPTS_CONSOLIDATED_REPAIR = "d7a3c1b9e4f2"

# T1b LAW-TERM-18 (§A.5): the versioned completion marker for the per-pair BINDINGS backfill
# (target_term_id/meta_overrides on every existing module entry). Keyed on the revision that
# shipped the backfill so a future re-backfill can bump it; separate from the T1 consolidation
# marker (the two backfills are independent). Not a user setting (absent from settings.DEFAULTS).
_PAIR_BINDINGS_KEY = "glossary_pair_bindings_backfilled"


def _assert_glossary_pair_binding_invariant(session) -> None:
    """T1b boot invariant (§A.5), beside the org-confinement invariant: no MODULE entry may be
    left un-bound. For ENTRIES a row with ``concept_id IS NULL OR term_id IS NULL OR
    (target_term_id IS NULL AND target_term != '')`` is self-healed per glossary via
    :func:`glossary_concepts.backfill_pair_bindings` (which links the concept/terms first when
    needed, then resolves target_term_id + pins meta_overrides). For SENSES a ``concept_id
    IS NULL`` row heals through ``sync_entry_concept`` on its base entry, and a ``term_id IS NULL
    AND target_term != ''`` row heals by re-projecting the base row. NEVER refuses boot -- a
    failure to heal is logged and boot continues (an un-healed row keeps enforcing its stored
    compat value; the projector's H-3 skip means it is never blanked)."""
    from sqlalchemy import and_, or_
    from sqlmodel import select as _select

    from app.models import GlossaryEntry, GlossarySense
    from app.services import glossary_concepts as _gc

    if not _gc._pair_binding_columns_present(session):
        return  # pre-migration (Postgres init_db refuses un-migrated before reaching here)
    # entries: the offending glossary ids (cheap SQL existence per glossary), then heal each.
    bad_gids = set(
        session.exec(
            _select(GlossaryEntry.glossary_id).where(
                GlossaryEntry.glossary_id.is_not(None),
                or_(
                    GlossaryEntry.concept_id.is_(None),
                    GlossaryEntry.term_id.is_(None),
                    and_(
                        GlossaryEntry.target_term_id.is_(None),
                        GlossaryEntry.target_term != "",
                    ),
                ),
            )
        ).all()
    )
    healed = 0
    for gid in bad_gids:
        try:
            _gc.backfill_pair_bindings(session, gid)
            # re-project the glossary so a freshly-linked concept_id-NULL sense (healed via
            # sync_entry_concept inside the backfill) and any stale sense go current.
            _gc.project(session, gid)
            healed += 1
        except Exception:  # never refuse boot
            log.exception("pair-binding boot invariant: heal of glossary %s failed", gid)
    # senses with a NULL concept_id whose base entry is already linked: heal via the base entry.
    orphan_sense_entries = set(
        session.exec(
            _select(GlossarySense.glossary_entry_id).where(GlossarySense.concept_id.is_(None))
        ).all()
    )
    for eid in orphan_sense_entries:
        base = session.get(GlossaryEntry, eid)
        if base is None or base.glossary_id is None:
            continue
        try:
            _gc.sync_entry_concept(session, base)
            _gc.project(session, base.glossary_id, entry_ids=[base.id], derive_senses=True)
            healed += 1
        except Exception:
            log.exception("pair-binding boot invariant: sense heal of entry %s failed", eid)
    if healed:
        session.flush()
        log.info("pair-binding boot invariant: healed %d glossary/entry unit(s)", healed)


def _backfill_glossary_concepts() -> None:
    """Seed-path invocation of the terminology concept/term backfill (LAW-TERM-6).

    The alembic `add_glossary_concepts` migration runs the same backfill inside its
    upgrade(); create_all cannot reach that data step, so it is ALSO exposed as
    `glossary_concepts.backfill_glossary(session)` and called here so a fresh install
    (create_all + backfill), a wiped rebuild (alembic upgrade over an empty DB →
    backfill a no-op), and an in-place upgrade all converge on the identical concept/
    term set. Idempotent + per-glossary guarded (skips a glossary that already has any
    concept), so it is safe to run on every boot and a no-op when there are zero module
    glossary entries (the fresh-test-DB case). Imported lazily to avoid an import cycle
    (db is imported very early)."""
    from sqlmodel import Session

    from app.models import Setting
    from app.services import glossary_concepts

    with Session(engine) as session:
        did = glossary_concepts.backfill_glossary(session)
        # R4 one-time repair: merge any concepts an EARLIER (buggy) build split one-per-entry
        # across a shared source surface. R5-8: this is a TRANSITIONAL repair, so gate it
        # behind a completion marker — once run to completion, the fixed dual-write/backfill/
        # import paths can never re-split, so it must not re-scan every glossary on every boot.
        merged = 0
        marker = session.get(Setting, _CONCEPTS_CONSOLIDATED_KEY)
        needs_repair = marker is None or marker.value != _CONCEPTS_CONSOLIDATED_REPAIR
        if needs_repair:
            merged = glossary_concepts.consolidate_split_concepts(session)
            if marker is None:
                session.add(
                    Setting(key=_CONCEPTS_CONSOLIDATED_KEY, value=_CONCEPTS_CONSOLIDATED_REPAIR)
                )
            else:
                marker.value = _CONCEPTS_CONSOLIDATED_REPAIR
                session.add(marker)
        # T1b (§A.5): the per-pair BINDINGS backfill. Install-wide, ONCE, behind its own
        # versioned marker (so it is not re-scanned every boot); it writes target_term_id/
        # meta_overrides ONLY where NULL, so it is idempotent and never re-pins a chosen value.
        from app.services import glossary_concepts as _gc

        pb_did = 0
        pb_marker = session.get(Setting, _PAIR_BINDINGS_KEY)
        pb_needs = pb_marker is None or pb_marker.value != _gc._PAIR_BINDINGS_MARKER
        if pb_needs and _gc._pair_binding_columns_present(session):
            pb_did = _gc.backfill_pair_bindings(session)
            if pb_marker is None:
                session.add(Setting(key=_PAIR_BINDINGS_KEY, value=_gc._PAIR_BINDINGS_MARKER))
            else:
                pb_marker.value = _gc._PAIR_BINDINGS_MARKER
                session.add(pb_marker)
        # The boot invariant runs on EVERY boot (cheap SQL existence check first), so a legacy
        # install that skipped the marker or a row left un-bound by any path self-heals.
        _assert_glossary_pair_binding_invariant(session)
        if did or merged or needs_repair or pb_did or pb_needs:
            session.commit()


def init_db() -> None:
    """Bring the schema up on boot.

    SQLite (the zero-config default + test engine) has THREE sub-paths:

      * TEST (config.IS_TEST — throwaway LOC_TMS_DATA_DIR, no LOC_TMS_SERVER): keep the
        create_all + additive-shim path (fast; 141 hermetic verify suites depend on it).
        The nullability-parity guard (tests/verify_migrations) keeps it from drifting from
        the alembic authority.
      * REAL install, fresh OR already alembic-stamped: alembic is the schema AUTHORITY —
        run `alembic upgrade head` in-process so EVERY migration (incl. non-additive ones
        the shim cannot apply) reaches the DB. Detected as: no tables at all (fresh) OR an
        `alembic_version` table present (already stamped; upgrade is a no-op at head).
      * REAL install, LEGACY pre-alembic DB (tables exist but NO alembic_version — created
        by an old create_all()): do NOT hard-crash (a bare `upgrade head` fails on existing
        tables). Fall back to the create_all + self-heal shim AND log a WARNING to rebuild
        under alembic. (Moot once such a DB is wiped+rebuilt under alembic.)

    `_ensure_default_org()` (idempotent app seed) runs at the end of EVERY sqlite sub-path.
    The alembic baseline already seeds the 'Default' org, so on the alembic path this is a
    harmless no-op — kept for safety across all three paths.

    On any other backend (Postgres) Alembic is the SOLE schema authority (UNCHANGED). We do
    NOT run create_all() there — it would build an unstamped, unseeded schema whose CREATE
    TABLEs then collide with `alembic upgrade head`. Instead we hard-refuse to boot on an
    un-migrated DB so the app never silently serves a half-built one."""
    if engine.dialect.name == "sqlite":
        if config.IS_TEST:
            _init_sqlite_create_all()
        else:
            insp = inspect(engine)
            has_tables = bool(insp.get_table_names())
            has_alembic = insp.has_table("alembic_version")
            if not has_tables or has_alembic:
                # Fresh DB (alembic builds+stamps it) or already stamped (no-op at head).
                _alembic_upgrade_head()
                # R4: the migration's own backfill uses the fixed one-concept-per-source-
                # surface grouping, but an install that ran an EARLIER (buggy) build of this
                # migration has a store split one-per-entry. Run the idempotent
                # backfill+consolidate on every real boot too (not only create_all), so the
                # repair reaches real installs. Both halves are guarded/idempotent no-ops on
                # an already-correct store.
                _backfill_glossary_concepts()
            else:
                # Tables but no alembic_version = a pre-alembic DB. Bare `upgrade head`
                # would fail on the existing tables, so keep the create_all+shim fallback
                # working and tell the operator to rebuild under alembic so non-additive
                # migrations stop being silently skipped.
                log.warning(
                    "legacy pre-alembic SQLite DB detected (tables present, no "
                    "alembic_version): using the create_all+self-heal fallback. "
                    "Non-additive migrations (nullability/drops/constraints) CANNOT "
                    "reach this DB. Rebuild it under alembic (back up, delete the DB "
                    "file, run 'alembic upgrade head') to make alembic the authority."
                )
                _init_sqlite_create_all()
        _ensure_default_org()
    else:
        # Alembic owns the schema here; refuse to serve an un-migrated DB.
        if not inspect(engine).has_table("alembic_version"):
            raise RuntimeError(
                "Database schema is not initialized - run 'alembic upgrade head' "
                "(Postgres) before starting."
            )
        # #73: the org-confinement invariant is a DATA law, not a SQLite one. Postgres
        # never ran _ensure_default_org (alembic seeds the org there), so assert it here
        # against the install's root org — the same lowest-id row access.
        # _install_default_org_id resolves.
        with engine.connect() as conn:
            row = conn.execute(text("SELECT id FROM organization ORDER BY id LIMIT 1")).fetchone()
            if row is not None:
                _assert_org_confinement_invariant(conn, row[0])
        # R4: idempotent backfill + one-time consolidation of any concept store an earlier
        # build split one-per-entry (guarded/no-op on an already-correct store).
        _backfill_glossary_concepts()
    # H3: clear a persisted retired org_name on every boot (all backends), AFTER the schema
    # is up. Idempotent + non-fatal; falls the header back to the live product brand.
    _fixup_retired_org_name()
    # GATE R3 MED-1: the dormant super-admin flag, cleared on every boot and on EVERY
    # BACKEND. This sits here rather than in _ensure_default_org (which is SQLite-only)
    # precisely because the rows it targets were created by the backend-agnostic
    # seed_admin() path, so Postgres carries them too.
    _clear_dormant_superadmin()


def get_session():
    """FastAPI dependency: yields a session per request.

    Inside a RELIEF scope (the doorman set it because this is a cancel/settings request that
    arrived at a fully checked-out pool) the session is opened on the POOL-LESS engine, so
    the one action that ENDS in-flight work cannot itself be blocked by the in-flight work
    (gate A round 3, HIGH-4). Ordinary requests are byte-identical: the ContextVar is False
    for every one of them."""
    with Session(relief_engine_or(engine)) as session:
        yield session


# ============================================================================== #
# KEY-BASED PROJECTS: THE CHANGE-FEED HOOK (SPEC-key-based-projects 2.3)
# ============================================================================== #
# It lives HERE, not in app/services/keys.py, and that placement is the whole point
# (review A2). Registered as an import side effect of the keys service, the hook was
# only real in a process that happened to import a key surface: a CLI run, a worker,
# an /api process that imported app.db and wrote Segment.target_text the orchestrator
# way appended NOTHING to the feed, which falsifies the "a changelog row no matter who
# wrote it" claim exactly where it matters most. `app.db` is imported by every process
# that can reach a Segment at all, so registering beside the engine makes the hook
# unconditional by construction.
#
# The GUARD is here and needs nothing but models.Segment: one isinstance scan over a
# flush's new/dirty/deleted sets. On a document-only install it finds no Segment with a
# key_id, returns, and app.services.keys is never even imported -- which is what the
# inertness invariant means in practice (tests/verify_keys_inertness.py [D] proves it
# by replacing the BODY with one that raises). The body is imported lazily, only once
# a key value row is genuinely in the flush.
#
# Registered on sqlalchemy.orm.Session (the BASE), never on sqlmodel.Session: a
# listener on the subclass would miss any plain SQLAlchemy session, and "no matter who
# wrote it" has to include a writer that never heard of SQLModel.
#
# Registration is IDEMPOTENT (re-review R7). `@event.listens_for` at import time
# registers a SECOND copy of the listener if this module is ever imported twice under
# different names or reloaded -- and then EVERY key write appends two identical
# changelog rows, which a delta consumer reads as two changes and which no assertion in
# the feed can distinguish from a genuine double write. The guard is a flag on the
# Session class itself, the same shape `keys._install_actor_inheritance` uses on
# `Thread.start`, so it survives a fresh module object.
_FEED_REGISTERED_ATTR = "_loc_key_change_feed_registered"


def _key_change_feed(session, flush_context, instances) -> None:
    """Append a KeyChangeLog row for every ORM write to a key VALUE row, whoever made
    it. FAIL-CLOSED: if the change cannot be recorded, the flush fails (a feed that
    silently drops entries is the failure mode the Delta API exists to beat)."""
    seg = models.Segment
    touched = [o for o in session.dirty if isinstance(o, seg) and getattr(o, "key_id", None)]
    gone = [o for o in session.deleted if isinstance(o, seg) and getattr(o, "key_id", None)]
    fresh = [o for o in session.new if isinstance(o, seg) and getattr(o, "key_id", None)]
    if not (touched or gone or fresh):
        return
    from app.services import keys as _keys

    _keys._append_segment_changes(session, touched, gone, fresh)


def _register_key_change_feed() -> None:
    """Attach `_key_change_feed` to the ORM's base Session exactly ONCE per process."""
    if getattr(_SAOrmSession, _FEED_REGISTERED_ATTR, False):
        return
    event.listen(_SAOrmSession, "before_flush", _key_change_feed)
    setattr(_SAOrmSession, _FEED_REGISTERED_ATTR, True)


_register_key_change_feed()


# ============================================================================== #
# WRITER-LOCK DISCIPLINE ON A SHARED SESSION (the QA-1 geometry, one definition)
# ============================================================================== #
# SQLite has ONE writer. Every subsystem a provider call reaches into
# (`spend.record_usage`, `audit.log`, notifications) opens its OWN Session, so any
# caller that holds an UNCOMMITTED write while a provider call runs — on this thread
# or on a pool thread it dispatched — creates a circular wait the busy_timeout can
# only delay (QA-1: "locked after 5.16s"). The cure is always the same: release the
# lock by committing BEFORE dispatch. These three helpers are the shared vocabulary
# for asking "am I holding it?" and "release it if so" — one definition, not one per
# caller (`eval_runner` delegates to them; the orchestrator and translate fan-out
# seams call `commit_if_staged`).
def dbapi_in_transaction(session: Session) -> bool:
    """Does this session's REAL database connection have a write transaction open?

    THE PREDICATE `session.new/dirty/deleted` COULD NOT ANSWER. SQLAlchemy AUTOFLUSHES
    before every query, so the instant a caller runs any SELECT after an `add()` the
    pending row is written to the database and REMOVED from `session.new` — it is now
    persistent-but-uncommitted, still inside the caller's open transaction, and (on
    SQLite) still holding the writer lock. Every one of those collections reads empty
    while the caller is in exactly the state we need to detect. This is not theory: a
    reviewer's probe walked straight through an ORM-level guard, and a private
    `Session(engine)` then blocked 5.27s and died `OperationalError: database is
    locked`.

    So the question is asked of the connection instead of the ORM's bookkeeping.
    `sqlite3.Connection.in_transaction` is True exactly when a write statement has
    executed and not yet been committed, and False for a session that has only read
    (pysqlite does not BEGIN for SELECT) — which is the discrimination needed: a
    read-only session holds nothing and must not be treated as if it did.

    Returns False when the flag cannot be determined (a DBAPI without it) rather than
    guessing — callers pair this with the ORM-level check."""
    if session is None:
        return False
    try:
        # Do NOT call session.connection() when no transaction is in play: it would
        # BEGIN one, which is a side effect a predicate must not have.
        if not session.in_transaction():
            return False
        raw = session.connection().connection
        raw = getattr(raw, "dbapi_connection", raw)
        return bool(getattr(raw, "in_transaction", False))
    except Exception:
        return False  # that defers to the ORM-level check above


def session_has_uncommitted_work(session) -> bool:
    """True when `session` is mid-edit: pending ORM changes OR an open write
    transaction on the connection. THE seam every guard asks, so there is one
    definition of "the caller is in the middle of something" and not several that
    drift apart."""
    if session is None:
        return False
    if session.new or session.dirty or session.deleted:
        return True
    return dbapi_in_transaction(session)


def commit_if_staged(session) -> bool:
    """Release the writer lock before a provider fan-out — but ONLY if it is held.

    Returns True when a commit was actually issued. The condition is the whole point:
    an UNCONDITIONAL commit here would put a write-transaction boundary on runs that
    stage nothing at all (no graph, no memory, nothing to persist mid-step), which is
    exactly the inertness the byte-stability suites defend — a feature that is OFF
    must leave no trace anywhere, including in the transaction log. So a no-op path
    stays a no-op, and a path that IS holding the lock hands it back before it can
    deadlock against the nested Sessions a provider call opens.

    IT MUST NOT EXPIRE THE IDENTITY MAP. `expire_on_commit` is suppressed for the
    duration of this commit, and that is not an optimization — it is a correctness
    requirement at a fan-out seam. `orchestrator._run_step_on_segment` is "session-
    free" in the sense that matters (it opens no Session and issues no query), but it
    DOES read `step.*` attributes off an ORM instance the DB thread loaded, relying on
    them being warm in the identity map. A plain commit expires every loaded
    instance, so the first `step.chunk_policy` read inside a POOL WORKER would fire a
    lazy reload — a query on the shared Session from a thread that does not own it —
    which is at best a race and in practice raises
    `ObjectDeletedError: ... or its row is otherwise not present` (observed, not
    theorized). Suppressing expiry is also SOUND here rather than merely convenient:
    this session is the only writer of those rows for the duration of the run and it
    has just written exactly what it had staged, so its in-memory state IS the
    committed truth. There is nothing to refresh.

    Never raises into a translation — but A FAILED COMMIT IS ROLLED BACK, not merely
    logged (review finding 3). SQLAlchemy dooms a transaction whose commit raised: the
    session is left needing a rollback, and the caller's next use of it dies with
    `PendingRollbackError` — while the writer lock this function exists to release is
    still held. Swallowing the error would therefore leave the caller strictly WORSE
    off than if the guard had never run, which is the one outcome a best-effort helper
    must not produce. The rollback costs nothing that was not already lost (the commit
    failed; that work is gone either way), hands the lock back, and returns a session
    the caller can keep using. False is returned because nothing was committed."""
    if not session_has_uncommitted_work(session):
        return False
    prior = getattr(session, "expire_on_commit", True)
    try:
        session.expire_on_commit = False
        session.commit()
        return True
    except Exception:
        log.warning(
            "commit_if_staged: could not release the writer lock; "
            "rolling back so the session stays usable",
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:
            log.warning(
                "commit_if_staged: rollback after a failed release also failed", exc_info=True
            )
        return False
    finally:
        try:  # noqa: SIM105
            session.expire_on_commit = prior
        except Exception:  # noqa: S110 -- restoring a plain session attribute on teardown
            pass


# ============================================================================== #
# TRANSIENT WRITER-LOCK RETRY (P-C5: raise the concurrent-writer ceiling honestly)
# ============================================================================== #
# SQLite has ONE writer. `BUSY_TIMEOUT_MS` (above) makes a single contended write
# wait out brief contention at the C level, but under ~15+ concurrent full runs a
# THUNDERING HERD forms: many writers all wake on the same busy-handler schedule,
# the same unlucky ones keep losing the lock, and after the full timeout they raise
# `database is locked` (QA Part-2 P-C5: ~22% of runs failed at 18-way). busy_timeout
# alone cannot fix this — its retry schedule is UN-jittered, so the losers stay
# synchronized. This helper adds the missing piece: a bounded application-level retry
# with JITTERED backoff that de-synchronizes the herd, so a writer that lost a full
# busy_timeout window sleeps a randomized beat and tries again on a fresh budget
# rather than failing outright. The ceiling itself stays architectural (a single
# writer); this just stops transient contention from surfacing as a hard failure.

#: Total commit attempts on a transient lock (1 initial + N-1 retries). Kept SMALL
#: on purpose: each attempt's own `session.commit()` can itself block up to
#: BUSY_TIMEOUT_MS at the SQLite level before it raises, so the worst-case wall time
#: of a fully-contended commit is ~ATTEMPTS * BUSY_TIMEOUT_MS + the backoff sleeps.
#: 3 bounds that tail to ~15s (plus <2s backoff) while still giving the herd two
#: jittered chances to clear — which is where all but a sustained flood resolve.
WRITE_RETRY_ATTEMPTS = 3
#: First backoff after a lost lock; doubles each retry, capped at WRITE_RETRY_MAX_S.
WRITE_RETRY_BASE_S = 0.1
WRITE_RETRY_MAX_S = 1.0


def is_locked_error(exc: BaseException) -> bool:
    """True for SQLite's TRANSIENT writer-contention errors (SQLITE_BUSY /
    SQLITE_LOCKED — 'database is locked' / 'database table is locked').

    Matched on the DBAPI message text because pysqlite surfaces both as a bare
    `OperationalError` with no distinguishing code on `.orig`. Deliberately NARROW:
    a 'no such table'/'disk I/O error' OperationalError is NOT transient and must
    fall straight through to fail the run, so only the two lock phrases match. Any
    non-OperationalError (a programming/integrity error) is never a lock."""
    if not isinstance(exc, OperationalError):
        return False
    msg = str(getattr(exc, "orig", None) or exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


#: Seconds a caller is told to wait after the CONNECTION POOL (not the writer lock) was
#: exhausted. Longer than the writer-lock hint: a full pool means long-lived work (runs
#: holding a connection each) is in flight, and that drains on the order of a run, not a
#: commit.
POOL_EXHAUSTED_RETRY_AFTER_S = 10

#: The single sentence every surface uses for pool exhaustion, so one phrase means one
#: thing in a run's notes, an /api body, and the log (the WRITE_CONTENTION_NOTE pattern).
POOL_EXHAUSTED_NOTE = (
    "Database connection pool exhausted: too much work is in flight for this install's "
    "configured capacity, so no connection was available. Nothing was left half-written; "
    "please retry once the running work drains, or raise LOC_TMS_DB_POOL_SIZE / lower "
    "LOC_TMS_MAX_CONCURRENT_RUNS."
)


def pool_has_no_capacity() -> bool:
    """True when EVERY pooled connection is checked out right now, so the next caller
    would have to WAIT (gate A round 1, HIGH-1).

    A non-blocking read of the pool's own counters — no checkout, no query, no lock — so a
    request can be refused at the door instead of pinning a worker thread for a
    `pool_timeout` it is going to lose anyway. That refusal is the difference between a
    503 in milliseconds and the 90-120s of pinned threads per request the gate measured,
    which is the accumulator behind the OOM (GitHub #109).

    Deliberately the ABSOLUTE ceiling (`size + overflow`), not a high-water mark: below it
    a checkout either succeeds immediately or opens a new connection, so shedding there
    would refuse work the install can carry. At it, every caller is queueing.

    False for any pool that cannot report (a :memory: SingletonThreadPool, NullPool), so
    the doorman is inert wherever it cannot be sure."""
    pool = getattr(engine, "pool", None)
    try:
        size = pool.size()
        overflow_cap = pool._max_overflow
        checked_out = pool.checkedout()
    except Exception:
        return False
    if overflow_cap < 0:  # unbounded overflow: there is always another connection
        return False
    return checked_out >= (size + overflow_cap)


def is_pool_timeout_error(exc: BaseException) -> bool:
    """True for SQLAlchemy's CONNECTION-POOL checkout timeout — the
    `QueuePool limit of size N overflow M reached, connection timed out` shape that was
    the ONLY exception type the 2026-09-06 flood produced (GitHub #100).

    Deliberately distinct from `is_locked_error`: a writer lock means "the database is
    busy with another WRITE" (retry in seconds); a pool timeout means "this PROCESS has
    no connection to give you" (a capacity problem the operator can size away).
    SQLAlchemy raises a dedicated
    `sqlalchemy.exc.TimeoutError`, and its own base is `SQLAlchemyError`, so a builtin
    TimeoutError from an HTTP client can never be mistaken for one. OperationalError
    wrappers qualify only when they explicitly identify a QueuePool limit; unrelated
    database errors do not become pool exhaustion just because capacity is low."""
    return isinstance(exc, _SATimeoutError) or (
        isinstance(exc, OperationalError) and "queuepool limit" in str(exc).lower()
    )


_emergency_engine = None
_relief_engine = None
_emergency_lock = _threading.Lock()
RELIEF_MAX_SESSIONS = 4
_relief_connections = _threading.BoundedSemaphore(RELIEF_MAX_SESSIONS)


class _BoundedNullPool(NullPool):
    """Pool-less connections with a process-wide ceiling and no checkout wait."""

    def _do_get(self):
        if not _relief_connections.acquire(blocking=False):
            raise _SATimeoutError("Emergency relief session limit reached; retry shortly")
        try:
            return super()._do_get()
        except BaseException:
            _relief_connections.release()
            raise

    def _do_return_conn(self, record):
        try:
            return super()._do_return_conn(record)
        finally:
            _relief_connections.release()


def _poolless_engine(poolclass):
    em = create_engine(
        config.DB_URL,
        echo=False,
        poolclass=poolclass,
        connect_args={"check_same_thread": False} if config.IS_SQLITE else {},
        **dbcrypt.engine_kwargs(config.DB_PATH),
    )
    if em.dialect.name == "sqlite":
        event.listen(em, "connect", _sqlite_pragmas)
    return em


def _emergency_engine_for_relief():
    """Request-only bounded engine; bookkeeping never consumes relief permits."""
    global _relief_engine
    if _relief_engine is None:
        with _emergency_lock:
            if _relief_engine is None:
                _relief_engine = _poolless_engine(_BoundedNullPool)
    return _relief_engine


def _ensure_emergency_engine():
    """Unbounded, blocking bookkeeping engine, independent of request relief."""
    global _emergency_engine
    if _emergency_engine is None:
        with _emergency_lock:
            if _emergency_engine is None:
                _emergency_engine = _poolless_engine(NullPool)
    return _emergency_engine


def emergency_session() -> Session:
    """A Session on a POOL-LESS engine, for the one job that must never be blocked by a
    full pool: recording WHY something failed.

    The flood's worst outcome was not the crash, it was the SILENCE after it. A run whose
    execution died on a pool timeout ran a crash handler that opened ANOTHER pooled
    session to mark the run FAILED -- which timed out too. The handler logged "Failed to
    mark run failed after crash" and the run stayed RUNNING at step 0, with no reason,
    forever (twelve of them, on the L1 install). A NullPool engine opens a connection on
    demand and closes it on release, so it owes nothing to the exhausted QueuePool and
    the honest failure always lands.

    Built lazily and once (nothing pays for it on a healthy install), with the SAME
    connect_args, SQLCipher kwargs and SQLite pragmas as the main engine -- a second
    connection to a WAL database is exactly what a second process would open. Use it ONLY
    for short, must-not-fail bookkeeping writes and wait reconcile reads. RELIEF paths use a
    bounded engine with the same configuration; they cannot consume or refuse these
    bookkeeping connections. Ordinary work belongs on the pooled engine."""
    return Session(_ensure_emergency_engine())


#: Seconds a launch caller is told to wait before retrying after write contention
#: sheds a launch write (the Retry-After hint on the honest 503). A couple of seconds
#: is long enough for a transient herd of concurrent launch writers to drain past the
#: single-writer ceiling, short enough not to strand an operator.
LAUNCH_CONTENTION_RETRY_AFTER_S = 3


class WriteContentionError(Exception):
    """A LAUNCH-request DB write could not commit after the bounded, jittered writer-lock
    retry — the single-writer SQLite DB stayed locked under sustained concurrent writes
    (flood R2-F1). HONEST + RETRYABLE and DISTINCT from a validation 4xx: no partial launch
    was recorded (the failing transaction rolled back atomically), so the caller should
    surface a 503 with a `retry_after` hint, never a 500 traceback. Raised ONLY at the
    launch SEAMS (start_run / start_batch / resume_run_async, via commit_launch); the
    run-EXECUTION seams keep re-raising the raw OperationalError so the run's own crash
    handler stamps WRITE_CONTENTION_NOTE (the architectural single-writer ceiling)."""

    def __init__(self, what: str = "", retry_after: int = LAUNCH_CONTENTION_RETRY_AFTER_S):
        self.what = what
        self.retry_after = retry_after
        super().__init__(
            "Write contention: the database is busy with too many concurrent writes, so "
            "the launch could not be recorded. Nothing was written; please retry"
            + (f" ({what})" if what else "")
            + "."
        )


def commit_launch(
    session,
    *,
    stage=None,
    what: str = "",
    attempts: int = WRITE_RETRY_ATTEMPTS,
    keep_loaded: bool = False,
) -> None:
    """`commit_with_retry` for a LAUNCH-request write (the run/batch/resume row a POST
    creates). Same bounded, jittered writer-lock retry — byte-identical on the happy path —
    but a lock that NEVER clears is translated into a `WriteContentionError` (an honest,
    retryable 503 signal) instead of the raw `sqlite3.OperationalError`, so the request
    surfaces a clean retryable error rather than a 500 traceback. The session is rolled back
    before raising so it is left reusable. A NON-lock error (or a non-SQLite backend) still
    propagates UNCHANGED — this only reshapes the exhausted-lock case.

    `keep_loaded` (fold-in) is forwarded to commit_with_retry so the launch row's PK stays
    readable WITHOUT a post-commit `refresh()` SELECT (which could itself lose the lock)."""
    try:
        commit_with_retry(session, stage=stage, attempts=attempts, keep_loaded=keep_loaded)
    except OperationalError as exc:
        if not is_locked_error(exc):
            raise
        try:  # leave the session clean for the caller / dependency teardown  # noqa: SIM105
            session.rollback()
        except Exception:  # noqa: S110 -- the lock error is re-raised as WriteContentionError next
            pass
        raise WriteContentionError(what) from exc


def flush_launch(
    session, *, stage=None, what: str = "", attempts: int = WRITE_RETRY_ATTEMPTS
) -> None:
    """FLUSH a LAUNCH-request INSERT so its server-assigned PK is readable, WITHOUT
    committing: the row stays inside the caller's still-open transaction (invisible to
    every other connection) until the caller commits it later.

    A launch dispatcher (start_batch, gate A round 12) needs the new batch's id BEFORE the
    row is published, so it can mark the batch in-flight and keep a concurrent finalizer
    from rolling up a batch whose queued members do not exist yet. The INSERT is the ONE
    writer-lock point (on SQLite the write lock is taken on the first write and held to
    commit), so this carries the same bounded, jittered retry and the same translation of a
    lock that never clears into a retryable WriteContentionError as `commit_launch`; the
    caller's follow-up commit runs under the lock this flush already holds and needs no
    retry of its own. `stage` re-applies the ORM staging before each attempt, exactly as
    commit_with_retry requires (a rollback expunges the pending INSERT)."""
    for attempt in range(attempts):
        try:
            if stage is not None:
                stage()
            session.flush()
            return
        except OperationalError as exc:
            if not is_locked_error(exc):
                raise
            try:  # release the writer lock and make the session reusable  # noqa: SIM105
                session.rollback()
            except Exception:  # noqa: S110 -- next attempt retries; the final one re-raises
                pass
            if attempt == attempts - 1:
                raise WriteContentionError(what) from exc
            backoff = min(WRITE_RETRY_MAX_S, WRITE_RETRY_BASE_S * (2**attempt))
            time.sleep(backoff * (0.5 + random.random()))  # noqa: S311 -- retry jitter, not a secret


def commit_with_retry(
    session, *, stage=None, attempts: int = WRITE_RETRY_ATTEMPTS, keep_loaded: bool = False
) -> None:
    """Commit `session`, retrying ONLY a transient SQLite writer-lock with bounded,
    jittered exponential backoff. Byte-identical to a plain `session.commit()` on the
    happy path (stage runs once, one commit, no sleep) — the retry machinery engages
    exclusively when a commit raises a lock error.

    `stage`, when given, RE-APPLIES this transaction's ORM mutations before each
    attempt. It is required whenever the transaction stages any INSERT or UPDATE: a
    failed commit dooms the transaction, and the `session.rollback()` this helper
    issues to make the session reusable EXPUNGES pending INSERTs and REVERTS pending
    UPDATEs (SQLAlchemy semantics — the same fact `commit_if_staged` documents). So
    the staged work is gone after the rollback and MUST be re-staged to be retried;
    a bare re-`commit()` would silently persist nothing. `stage` should therefore be
    idempotent ORM staging only (re-`add` the same instances, re-set the same
    attributes) — never side effects, provider calls, or event yields.

    `keep_loaded` (flood R2-F1 fold-in) suppresses SQLAlchemy's default post-commit
    EXPIRY for the duration of the successful commit, so the just-committed row's
    server-assigned PK and attributes stay populated and readable WITHOUT a follow-up
    `session.refresh(row)` — a refresh is a SELECT, and on the launch hot path that
    SELECT can ITSELF lose the writer lock and raise a raw OperationalError -> 500, the
    exact shape R2-F1(a) hit in apikeys. `expire_on_commit` is restored in `finally`,
    so only this commit is affected (the intervening retry rollbacks still expire — the
    work is re-staged either way). Off by default, so every existing caller is unchanged.

    Re-raises the OperationalError UNCHANGED once `attempts` is exhausted, and
    re-raises any NON-lock error immediately (see is_locked_error) — so a caller's
    outer handler still marks the run FAILED, now with an honest reason. On a
    non-SQLite backend the lock phrases never match, so the first error propagates
    at once (Postgres has its own MVCC concurrency and needs none of this)."""
    prev_expire = session.expire_on_commit
    if keep_loaded:
        session.expire_on_commit = False
    try:
        for attempt in range(attempts):
            try:
                if stage is not None:
                    stage()
                session.commit()
                return
            except OperationalError as exc:
                if not is_locked_error(exc) or attempt == attempts - 1:
                    raise
                # Roll back so the doomed transaction releases the writer lock and the
                # session is reusable; `stage` re-applies the (now-discarded) work on the
                # next pass. Best-effort — a rollback that itself fails leaves nothing to
                # salvage, so let it surface.
                session.rollback()
                backoff = min(WRITE_RETRY_MAX_S, WRITE_RETRY_BASE_S * (2**attempt))
                # Jitter (0.5x–1.5x) is the whole point: it de-synchronizes writers that
                # lost the same busy_timeout window so they stop colliding on the retry.
                time.sleep(backoff * (0.5 + random.random()))  # noqa: S311 -- retry jitter, not a secret
    finally:
        if keep_loaded:
            session.expire_on_commit = prev_expire


def commit_or_shed(
    session, *, stage=None, attempts: int = WRITE_RETRY_ATTEMPTS, keep_loaded: bool = False
) -> bool:
    """Commit a BEST-EFFORT write (audit trail / memory capture / observability), riding
    out transient SQLite writer-lock contention via `commit_with_retry`, then SHEDDING
    (rollback + return False) instead of propagating the lock. Returns True on commit,
    False if the lock never cleared within `attempts`. Byte-identical to a plain
    `commit_with_retry` on the happy path (`stage` runs once, one commit, no sleep).

    The CALLER owns the degrade: it decides whether a shed write means "drop the record
    and carry on" (fail-open) or "raise / fail the operation" (fail-closed), and it emits
    the single domain-specific WARN naming what was dropped. This differs from
    `tap_envelope.commit_tap_write` (which warns internally with tap-specific wording) so
    audit + llmemory can name their own dropped record.

    A NON-lock error (a real bug — bad column, integrity violation) is RE-RAISED unchanged
    so it still surfaces loudly; only transient SQLITE_BUSY/LOCKED is shed (is_locked_error
    is deliberately narrow). On a non-SQLite backend the lock phrases never match, so the
    first error propagates at once. `stage` MUST be idempotent ORM staging only (the retry
    rolls back and re-applies it — see commit_with_retry); `keep_loaded` forwards the
    post-commit no-expiry so the row's server-assigned PK stays readable WITHOUT a
    lock-prone refresh SELECT."""
    try:
        commit_with_retry(session, stage=stage, attempts=attempts, keep_loaded=keep_loaded)
        return True
    except OperationalError as exc:
        if not is_locked_error(exc):
            raise
        # Retry exhausted on a genuinely transient lock: roll back so the doomed
        # transaction releases the writer lock and the session is reusable, then SHED.
        try:  # noqa: SIM105
            session.rollback()
        except Exception:  # noqa: S110 -- the write is shed (False) either way; caller handles it
            pass
        return False
