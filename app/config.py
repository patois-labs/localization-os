"""Central config. Reads .env so the app and (later) the MT providers agree on
keys and defaults. Chunk 0 works with zero keys set.

Set LOC_TMS_DATA_DIR to point the database + uploads somewhere else (the
verification scripts use this to run against a throwaway directory).

Documented in: docs/architecture/configuration.md
"""

import contextlib
import ipaddress
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains the `app/` package.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# Child of the app-wide "loc_tms" logger (same tree main.py uses). Used for the
# boot-time secret-resolution warnings in _secret(); with no handler configured
# yet at import, logging's lastResort still surfaces WARNING+ on stderr (loud).
_logger = logging.getLogger("loc_tms.config")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_num(key: str, default, *, cast=int, minimum=None, refuse_below=None):
    """A numeric env setting that can NEVER break boot. An unset/blank/garbage value
    falls back to `default` with a WARNING rather than raising at import time — these
    knobs are capacity dials an operator types into a Fly secret or a .env by hand, and
    a typo must not take the whole process down before logging is even configured.

    `minimum` CLAMPS (the value is meaningful, just too small). `refuse_below` REJECTS:
    the value is nonsense for this dial, so it falls back to `default` rather than being
    silently coerced into a neighbouring meaning. Gate A round 1 LOW-3 is why the two are
    different: `LOC_TMS_MAX_CONCURRENT_RUNS=-1` clamped to 0, and 0 means "no admission
    gate at all" — a typed minus sign turned a safety gate OFF."""
    raw = (_get(key, "") or "").strip()
    if not raw:
        return default
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        _logger.warning("%s=%r is not a number; using the default %r", key, raw, default)
        return default
    if refuse_below is not None and val < refuse_below:
        _logger.warning(
            "%s=%r is not a valid value (below %r); using the default %r",
            key,
            val,
            refuse_below,
            default,
        )
        return default
    if minimum is not None and val < minimum:
        _logger.warning("%s=%r is below the floor %r; using %r", key, val, minimum, minimum)
        return minimum
    return val


# --- App ---
QUEUE_DRAIN_WAIT_S = _get_num("LOC_TMS_QUEUE_DRAIN_WAIT_S", 600.0, cast=float, minimum=0.1)

HOST = _get("HOST", "127.0.0.1")
PORT = int(_get("PORT", "8000"))


def loopback_bind() -> bool:
    """Only literal loopback addresses and localhost are local binds."""
    if HOST.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(HOST).is_loopback
    except ValueError:
        return False


# --- Logging (opt-in structured logs) ---
# LOG_FORMAT=text (default) keeps uvicorn/stdlib defaults untouched — the verify
# suites never set it, so their output is unchanged. LOG_FORMAT=json switches the
# whole process (app + uvicorn access/error via run.py) to one-JSON-object-per-line
# for log shippers. LOG_LEVEL applies only when JSON logging is active.
LOG_FORMAT = _get("LOG_FORMAT", "text").lower()
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()

# --- Install mode (server vs. dev vs. test) ---
# The startup gate (see app/main._startup) needs to tell three worlds apart:
#   * local dev  — neither var set: seed the default admin/linguist/user1 accounts,
#                  turn the login wall on, start the scheduler (current behavior).
#   * container  — LOC_TMS_SERVER set (the Dockerfile/compose set it): a REAL install,
#                  so the login wall is on and the scheduler runs, but NO dev accounts
#                  are seeded — first visit lands on /auth/setup to create the admin.
#   * tests      -- LOC_TMS_DATA_DIR + LOC_TMS_TEST_MODE and LOC_TMS_SERVER unset:
#                  no wall, no seeding, no scheduler — deterministic + unauthenticated.
# LOC_TMS_SERVER is the explicit server-mode signal; it deliberately does NOT overload
# LOC_TMS_DATA_DIR (which the 141 verify suites set), so those suites are untouched and
# a container — which legitimately sets LOC_TMS_DATA_DIR=/data for its volume — is no
# longer mistaken for a test run.
_SERVER_ENV = _get("LOC_TMS_SERVER", "").strip().lower() in ("1", "true", "yes", "on")
# --- Embedded / headless profile (composable-core phase 0, Train 0a) ---
# LOC_TMS_EMBEDDED=1 recomposes the app as a headless API service (no Jinja/web
# surface, no /static, no /back-office, no scheduler/retention daemons, no login
# wall) — driven entirely over token-authed /api + MCP, with an external cron hitting
# POST /api/admin/tick. Truthy-parsed exactly like LOC_TMS_SERVER.
#
# CRITICAL INTERACTION with LOC_TMS_DATA_DIR / IS_TEST: an embedded install is a REAL
# install, never a test boot, and it legitimately sets LOC_TMS_DATA_DIR (its volume).
# So embedded IMPLIES server posture — it is folded into IS_SERVER below BEFORE the
# IS_TEST derivation runs. Without that fold, `IS_TEST = DATA_DIR and not IS_SERVER`
# would misclassify an embedded boot (which sets DATA_DIR but might not set
# LOC_TMS_SERVER) as a test run — the exact trap Option A closed for the container.
# LOC_TMS_SERVER stays the explicit server signal; embedded is a strict superset of it.
IS_EMBEDDED = _get("LOC_TMS_EMBEDDED", "").strip().lower() in ("1", "true", "yes", "on")
IS_SERVER = _SERVER_ENV or IS_EMBEDDED
# Test mode requires explicit intent and a throwaway directory. A storage override
# alone is a real install. Declared server and embedded modes always take precedence.
IS_TEST = (
    bool(_get("LOC_TMS_DATA_DIR"))
    and _get("LOC_TMS_TEST_MODE").lower() in ("1", "true", "yes", "on")
    and not IS_SERVER
)
if IS_TEST and not loopback_bind():
    raise RuntimeError(
        "Test mode requires a loopback HOST. Use LOC_TMS_SERVER=1 to serve remotely."
    )


def local_host_pin() -> bool:
    """Real local loopback installs always pin Host, including on their probes."""
    return not IS_SERVER and not IS_TEST and loopback_bind()


# --- Storage (overridable for tests) ---
# DATA_DIR always holds uploads + keyfiles (.session_secret/.enc_key) regardless
# of the DB backend; only the *database* location moves when DATABASE_URL is set.
DEFAULT_DATA_DIR = BASE_DIR / "data"
DATA_DIR = Path(_get("LOC_TMS_DATA_DIR", str(DEFAULT_DATA_DIR)))
# True when nothing overrode the location — i.e. we resolved to the real repo
# `data/` dir. Used by the data-dir protection guard below.
DATA_DIR_IS_DEFAULT = not bool(_get("LOC_TMS_DATA_DIR"))
UPLOADS_DIR = DATA_DIR / "uploads"
# Staging root for the full-dump data-export ZIP artifacts (Batch 13, Train A).
# A DEDICATED dir (never UPLOADS_DIR): export artifacts are staged PLAINTEXT (the
# ExportStore/DiskExportStore port, app/ports/exportstore.py), Range-seekable, and
# reaped on their own TTL — the opposite posture from the Fernet-at-rest uploads
# store. Created 0o700 with each artifact 0o600 by DiskExportStore (mirrors
# backup_archive's on-disk secret posture). Sits OUTSIDE the DR backup member set
# (backup_archive.create_archive archives db/ + uploads/ + keyfiles/ only).
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "loc_tms.db"
# SQLite is the zero-config default. Set DATABASE_URL to opt into Postgres, e.g.
#   postgresql+psycopg://user:pass@host:5432/loc_tms
# On Postgres, Alembic owns the schema (`alembic upgrade head`); see docs/DEPLOY.md.
DB_URL = _get("DATABASE_URL") or f"sqlite:///{DB_PATH}"
IS_SQLITE = DB_URL.startswith("sqlite")
if IS_SQLITE and _get("DATABASE_URL"):
    # #525: with DATABASE_URL=sqlite:///elsewhere.db the live file is NOT DATA_DIR/loc_tms.db,
    # so backup, restore, encryption and the vault (all keyed on DB_PATH) pointed at a file
    # the app never opens. Follow the URL when it names a real file.
    from sqlalchemy.engine import make_url as _make_url

    _url = _make_url(DB_URL)
    _name = _url.database
    if (
        _name
        and _name not in (":memory:", "file::memory:")
        and _url.query.get("mode") != "memory"
        and _url.query.get("uri") != "true"
    ):
        DB_PATH = Path(_name).absolute()
    del _make_url, _url, _name


def private_directory(path: Path) -> None:
    """Create each missing parent privately; existing directories keep their ACLs."""
    if path.is_dir():
        return
    if path.parent == path:
        raise FileNotFoundError(f"directory root does not exist: {path}")
    private_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)


def prepare_sqlite_file(database_url: str | None = None) -> None:
    """Precreate file-backed SQLite databases before any driver writes to them."""
    database_url = database_url or DB_URL
    if not database_url.startswith("sqlite"):
        return
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    name = url.database
    if not name or name in (":memory:", "file::memory:") or url.query.get("mode") == "memory":
        return
    if url.query.get("uri") == "true":
        from urllib.parse import unquote

        name = unquote(name.removeprefix("file:"))
        if url.query.get("mode") in ("ro", "rw"):
            return  # SQLite must refuse missing files for these modes.
    prepare_private_file(Path(name))


def prepare_private_file(path: Path) -> None:
    """Reserve a new private file without changing existing content or permissions."""
    private_directory(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)


# --- Connection pool + run admission (GitHub #99 / #100) ----------------------
# THE CAPACITY TRIANGLE. These three settings are one design point, not three
# independent knobs, and the 2026-09-06 night-stress flood is what makes that concrete:
# the install ran on SQLAlchemy's DEFAULTS (pool_size 5 + max_overflow 10 = 15
# connections, pool_timeout 30s) while EVERY in-flight workflow run pins ONE pooled
# connection for its whole duration (orchestrator._run_in_background opens a Session and
# holds it across every provider call, which is minutes on a local LLM). At 6-12
# concurrent runs the pool was therefore already spoken for before a single request
# arrived: runs crashed with `QueuePool limit of size 5 overflow 10 reached`, /api
# returned 500s at exactly 30.2s (= pool_timeout), and the DB-FREE /health stalled for
# 20-145s because every waiting caller pinned an anyio worker thread (and every async
# handler that checks out a connection pins the event LOOP) for the full 30s. On prod
# that became a 35-minute 503 outage that ended in an OOM kill.
#
# The rules the defaults below encode:
#   * total capacity (POOL_SIZE + MAX_OVERFLOW) must comfortably exceed
#     MAX_CONCURRENT_RUNS, because each run holds one connection for its lifetime,
#     and each run's mt_concurrency worker threads borrow more on top of that;
#   * POOL_TIMEOUT_S is a FAILURE budget, not a queue: it is how long a caller may pin
#     a worker thread (or the event loop) waiting for capacity that is already gone.
#     SQLAlchemy's 30s default turns a saturated pool into a liveness outage; a few
#     seconds turns it into an honest, retryable 503 (app.main._pool_exhausted).
#   * MAX_CONCURRENT_RUNS is the ADMISSION gate that keeps the first rule true. A
#     launch beyond it is REFUSED at the door with a named 503 + Retry-After
#     (orchestrator.RunCapacityError) instead of being admitted into a pool that cannot
#     carry it — the flood's runs were admitted, crashed on checkout, and (because the
#     crash handler needed a connection of its own to record the failure) were left
#     RUNNING at step 0 with no reason, forever. 0 disables the gate (unbounded, the
#     pre-fix behaviour) for an operator who has sized the pool themselves.
#
# WHAT THE GATE COUNTS, EXACTLY (gate A round 2, MED-5 — the honest version, because the
# blanket claim "every run class counts AND is bounded" was not true):
#   * COUNTED AND BOUNDED: a single document/key launch, every batch member (including a
#     re-run and retry-failed), every key fan-out launch, and an Advanced mission. A fan-out
#     takes what is free and QUEUES the rest, so it is delayed, never refused outright, and
#     never uncounted.
#   * CHILDREN share the admitted parent's lifetime, not a second run slot. MT and
#     Advanced delegate executors are capped by RUN_WORKERS and included in the memory
#     estimate below. Their pooled sessions, spend writes and tap enrichment still need
#     connection headroom; DB_CONNECTIONS_PER_RUN is the historical planning factor,
#     not a hard per-run connection quota. Size the pool for delegates too.
#   * The synchronous stream and resume entrypoints now reserve through the same counter.
#     Other direct/async work remains a #109 exclusion; provider pumps are capped below.
#     the memory estimate is deliberately not described as an install-wide RSS limit.
# Documented in: docs/architecture/configuration.md
DB_POOL_SIZE = _get_num("LOC_TMS_DB_POOL_SIZE", 20, minimum=1)
DB_MAX_OVERFLOW = _get_num("LOC_TMS_DB_MAX_OVERFLOW", 40, minimum=0)
DB_POOL_TIMEOUT_S = _get_num("LOC_TMS_DB_POOL_TIMEOUT_S", 5.0, cast=float, minimum=0.1)
# refuse_below=0 (gate A round 1, LOW-3): a NEGATIVE cap is not a small cap, it is a typo,
# and clamping it to 0 would silently turn the admission gate OFF. 0 itself stays a real
# operator choice (see the warning below); a minus sign falls back to the default.
MAX_CONCURRENT_RUNS = _get_num("LOC_TMS_MAX_CONCURRENT_RUNS", 8, minimum=0, refuse_below=0)

#: Pooled connections reserved for ORDINARY REQUEST traffic — everything that is not an
#: in-flight run: page loads, /api polls, SSE observers, the job daemons, and each run's
#: own mt_concurrency workers borrowing a second connection for a moment. It is the
#: "comfortably" in "capacity must comfortably exceed the run ceiling", made a number so
#: the boot guard below can check it instead of an adjective.
DB_REQUEST_HEADROOM = _get_num("LOC_TMS_DB_REQUEST_HEADROOM", 10, minimum=1)


#: Pooled connections a single admitted run may hold AT ONCE: the one its execution thread
#: pins for the run's whole duration, PLUS one for the second connection its own work
#: borrows for a moment (an `mt_concurrency` worker, a spend write, a tap enrichment). Two,
#: not one, is the difference between the boot guard and the invariant the suite and the
#: docs state (gate A round 2, MED-3): at one-per-run the guard would happily clamp to a
#: cap where every run holds a connection and ten are left for the entire rest of the
#: install, which is `db.pool_has_no_capacity()` as the STEADY state — the doorman shedding
#: everything, forever. One rule, stated once, enforced here.
DB_CONNECTIONS_PER_RUN = 2


def _pool_capacity_floor(pool_size: int, overflow: int, headroom: int) -> int:
    """The largest run ceiling `pool_size + overflow` can carry: `DB_CONNECTIONS_PER_RUN`
    connections per in-flight run, plus `headroom` connections left for request traffic. At
    least 1 — an install with a tiny pool still runs one run at a time rather than none.

    This is the SAME rule `tests/verify_stress_liveness_pool.py` [A] asserts of the live
    configuration (`pool_size + overflow >= cap * DB_CONNECTIONS_PER_RUN`) and the same one
    `docs/architecture/configuration.md` describes; the guard is strictly stronger than the
    invariant because it also holds back `headroom`."""
    usable = (int(pool_size) + int(overflow)) - int(headroom)
    return max(1, usable // max(1, int(DB_CONNECTIONS_PER_RUN)))


#: SQLite page cache per CONNECTION, in KiB — the MEMORY half of the pool sizing (gate A
#: round 1, the OOM paragraph; GitHub #109). SQLite's own default is `-2000` = 2 MiB per
#: connection, which nobody notices at five connections and is 120 MiB of resident memory
#: at sixty on the 1 GB machine that OOM-killed production. The arithmetic is
#: `(DB_POOL_SIZE + DB_MAX_OVERFLOW) x SQLITE_CACHE_KB` worst case,
#: `DB_POOL_SIZE x SQLITE_CACHE_KB` steady state, and it is set EXPLICITLY here so that
#: raising the pool is visibly a memory decision too. Documented, with the numbers, in
#: docs/architecture/configuration.md. Defined BEFORE the boot guard below because the
#: clamp's warning quotes this arithmetic (gate A round 2, MED-3).
SQLITE_CACHE_KB = _get_num("LOC_TMS_SQLITE_CACHE_KB", 1000, minimum=64)

#: Seconds between the interpreter's GIL hand-offs (`sys.setswitchinterval`), applied at app
#: startup (#615). CPython's default is 5 ms. A request that touches SQLite releases the GIL
#: many times, and each release to a CPU-bound thread (an import parse, an export build, a
#: search scoring pass) costs the request up to one full interval before it runs again, so
#: with the default a 30k-unit TMX import made a 40 ms write take 1 to 2 s. At 0.5 ms the
#: same concurrent write waits ~0.1 s and reads stay under 50 ms (measured on the throwaway
#: harness, docs/architecture/configuration.md). Review measurement (4 CPU-bound threads): on
#: Linux (32 vCPU) 0.5 ms costs nothing (4-thread CPU 0.436 s -> 0.414 s, concurrent imports
#: 1.14 s -> 1.2 s) and cuts the small-write p50 from 27 ms to 7 ms; on WINDOWS the same 0.5 ms
#: made 4 concurrent CPU-bound threads 50 to 90 percent slower (pure CPU 1.43 s -> 2.14 s, 4
#: concurrent 10k imports 5.7 s -> 10.6 s), so the default there stays CPython's 5 ms.
#: An explicit LOC_TMS_GIL_SWITCH_INTERVAL_S is honoured on every platform.
GIL_SWITCH_INTERVAL_S = _get_num(
    "LOC_TMS_GIL_SWITCH_INTERVAL_S",
    0.005 if os.name == "nt" else 0.0005,
    cast=float,
    minimum=0.0001,
)

# Host ceilings, independent of mutable per-workflow parallelism. A parent owns its
# child executor until shutdown, so its children are included in the parent's budget.
ANYIO_WORKERS = _get_num("LOC_TMS_ANYIO_WORKERS", 16, minimum=1)
# Also the independent process-wide provider pump ceiling; each pump queue uses
# RUN_WORKERS chunks and PROVIDER_TIMEOUT_S as its bounded backpressure wait.
STREAM_WORKERS = _get_num("LOC_TMS_STREAM_WORKERS", 32, minimum=1)
STREAM_WAIT_S = _get_num("LOC_TMS_STREAM_WAIT_S", 5, minimum=1)
RUN_WORKERS = _get_num("LOC_TMS_RUN_WORKERS", 4, minimum=1)
JOB_WORKERS = _get_num("LOC_TMS_JOB_WORKERS", 2, minimum=1)
# Unset disables refusal. This is a sizing estimate, not an OS memory limit; see the
# named exclusions and mock measurement method in configuration.md.
MEMORY_BUDGET_MB = _get_num("LOC_TMS_MEMORY_BUDGET_MB", 0, minimum=0)


def memory_estimate_mb() -> float:
    """Conservative modeled workload: caches + stacks + working sets + baseline.

    Six fixed threads reserve main/readiness/admission/progress/scheduler/retention.
    Two direct maintenance driver threads may run while the daemon threads sleep.
    Two extra parent threads per admitted run cover execution and batch waiters.
    Anyio, jobs and two maintenance callers can also open a child executor.
    STREAM_WORKERS also caps provider pumps, including abandoned upstream reads.
    R shares runs, evaluations, direct LLMemory and direct job/report/export work.
    Synchronous children share their parent's R, J or maintenance allowance.
    Other unmodeled direct helpers and arbitrary payload bytes remain excluded.
    """
    parents = MAX_CONCURRENT_RUNS + ANYIO_WORKERS + STREAM_WORKERS + JOB_WORKERS + 2
    threads = (
        6
        + 2  # direct maintenance drivers while the two daemons sleep
        + ANYIO_WORKERS
        + STREAM_WORKERS
        + STREAM_WORKERS  # independently capped provider stream pumps
        + JOB_WORKERS
        + 2 * MAX_CONCURRENT_RUNS
        + parents * RUN_WORKERS
    )
    return (
        (DB_POOL_SIZE + DB_MAX_OVERFLOW) * SQLITE_CACHE_KB / 1024 + threads * 8 + parents * 32 + 256
    )


def assert_memory_budget() -> None:
    if MEMORY_BUDGET_MB and (MAX_CONCURRENT_RUNS == 0 or memory_estimate_mb() > MEMORY_BUDGET_MB):
        raise ValueError(
            f"LOC_TMS_MEMORY_BUDGET_MB={MEMORY_BUDGET_MB} cannot carry the configured "
            f"workload estimate ({memory_estimate_mb():.1f} MiB; unbounded runs are refused). "
            "Reduce run/worker/pool settings; see docs/architecture/configuration.md."
        )


# Check the requested settings before the existing pool guard can clamp the run cap.
assert_memory_budget()

#: THE BOOT GUARD (gate A round 1, MED-2). `LOC_TMS_MAX_CONCURRENT_RUNS=60` against the
#: default 20+40 pool used to boot silently into exactly the pre-fix failure: the gate
#: admits sixty runs, the pool has sixty connections, and the first page load waits for a
#: connection that no longer exists. The three dials are one design point, so the process
#: refuses to hold a self-contradictory one.
#:
#: CLAMP, not refuse-to-start, deliberately: an install that boots today must keep booting
#: after an upgrade, and a running-but-more-conservative install is a smaller surprise at
#: 3am than a machine that will not come back. The clamp is LOUD (a WARNING naming both
#: numbers and the dial to turn) and `MAX_CONCURRENT_RUNS_CONFIGURED` keeps what the
#: operator asked for, so the clamp is visible rather than merely effective.
MAX_CONCURRENT_RUNS_CONFIGURED = MAX_CONCURRENT_RUNS
_RUN_CAP_CEILING = _pool_capacity_floor(DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_REQUEST_HEADROOM)
if MAX_CONCURRENT_RUNS > _RUN_CAP_CEILING:
    _logger.warning(
        "LOC_TMS_MAX_CONCURRENT_RUNS=%s exceeds what this connection pool can carry "
        "(%s + %s connections, %s reserved for request traffic, %s per admitted run) — "
        "CLAMPED to %s. Every in-flight run holds one connection for its whole duration "
        "and borrows a second for its own workers, so admitting more runs than that "
        "reintroduces the exhaustion the gate exists to prevent. MEMORY IS THE OTHER HALF "
        "and this clamp does NOT bound it (GitHub #109): %s admitted runs still mean %s "
        "run threads, their provider workers, and up to %s MiB of SQLite page cache "
        "(LOC_TMS_SQLITE_CACHE_KB x the pool) — on the 1 GB machine that OOM-killed "
        "production on 2026-09-06, size the MACHINE before you raise this. Raise "
        "LOC_TMS_DB_POOL_SIZE / LOC_TMS_DB_MAX_OVERFLOW to lift the ceiling; see "
        "docs/architecture/configuration.md.",
        MAX_CONCURRENT_RUNS,
        DB_POOL_SIZE,
        DB_MAX_OVERFLOW,
        DB_REQUEST_HEADROOM,
        DB_CONNECTIONS_PER_RUN,
        _RUN_CAP_CEILING,
        _RUN_CAP_CEILING,
        _RUN_CAP_CEILING,
        ((DB_POOL_SIZE + DB_MAX_OVERFLOW) * SQLITE_CACHE_KB) // 1024,
    )
    MAX_CONCURRENT_RUNS = _RUN_CAP_CEILING
if MAX_CONCURRENT_RUNS == 0:
    # SAY IT OUT LOUD, once, at boot. 0 is a real operator choice (someone who sized the
    # pool themselves), and a safety gate that is off should never be off SILENTLY -- the
    # failure it prevents is a 35-minute outage, and the symptom an operator sees is
    # nothing at all until then.
    _logger.warning(
        "LOC_TMS_MAX_CONCURRENT_RUNS=0: the run-admission gate is OFF, so concurrent "
        "workflow runs are unbounded. Size LOC_TMS_DB_POOL_SIZE for the load you expect "
        "(every in-flight run holds one connection); see docs/architecture/configuration.md."
    )

# How long the /ready probe may wait for the database before it answers 503. A
# readiness probe exists to answer FAST and HONESTLY -- a probe that blocks for
# pool_timeout is itself the outage (Fly's health check budget is 5s), so the probe
# runs on its own dedicated thread with this wall-clock budget and never borrows the
# shared anyio worker pool. See app/web/health.py.
READY_PROBE_TIMEOUT_S = _get_num("LOC_TMS_READY_PROBE_TIMEOUT_S", 2.0, cast=float, minimum=0.1)

# Seconds an EngineProfile row stays memoized in-process (app.services.engines).
# Provider resolution reads the profile once PER SEGMENT, so on a 654-segment document
# that was 654+ pool checkouts inside ONE step, from every mt_concurrency worker of
# every concurrent run (#100's "amplifier"). The cache is invalidated exactly whenever
# an EngineProfile is written through this process (engines._profile_written), so the
# TTL only bounds staleness from ANOTHER process (a CLI edit against the same DB).
# 0 disables the cache (every call re-reads the row, the pre-fix behaviour).
ENGINE_PROFILE_CACHE_TTL_S = _get_num(
    "LOC_TMS_ENGINE_PROFILE_CACHE_TTL_S", 5.0, cast=float, minimum=0
)

# --- Data-dir protection guard (issue #13) -----------------------------------
# THREAT MODEL: dev/QA tooling that launches a bare `uvicorn app.main:app` cannot
# inject LOC_TMS_DATA_DIR, so it silently serves the REAL production data dir
# (code/local-tms/data). A marker FILE that travels with the data dir is the right
# defense precisely because it works regardless of HOW the process was launched.
#
# The guard is SERVER-STARTUP ONLY (called from app.main._startup, which fires on
# uvicorn boot / TestClient context-enter). It deliberately does NOT run in the
# CLI (`python -m app.cli ...`) — the CLI operates on real data BY DESIGN and is a
# sanctioned local-root tool that never imports app.main. Tests use throwaway
# LOC_TMS_DATA_DIR dirs that never contain the marker, so the guard is inert for
# them (marker absent => no behavior change).
#
# A dir is protected iff it contains a `.protected` marker file. If the resolved
# DATA_DIR is protected AND LOC_TMS_ALLOW_REAL_DATA is not set, server startup
# hard-refuses. run.py (the sanctioned launcher) sets the override automatically,
# so the maintainer's normal `python run.py` flow is unchanged AND creates the marker on
# first sanctioned run (see create_protect_marker) — protection becomes ambient.
PROTECT_MARKER_NAME = ".protected"
PROTECT_MARKER = DATA_DIR / PROTECT_MARKER_NAME
ALLOW_REAL_DATA = _get("LOC_TMS_ALLOW_REAL_DATA", "").strip().lower() in ("1", "true", "yes", "on")


def data_dir_is_protected() -> bool:
    """True iff the resolved DATA_DIR carries the `.protected` marker file. The
    marker protects ANY dir it sits in — that is the point: an explicit
    LOC_TMS_DATA_DIR pointing at a protected dir is STILL protected (the common
    tooling case is an explicit *temp* dir, which has no marker, so it is not)."""
    try:
        return PROTECT_MARKER.exists()
    except OSError:
        return False


class ProtectedDataDirError(RuntimeError):
    """Raised when server startup is refused against a protected data dir."""


def assert_data_dir_allowed() -> None:
    """Refuse to start the SERVER against a protected data dir unless explicitly
    allowed. Pure + testable (no I/O beyond the marker stat); called from
    app.main._startup so it gates exactly the uvicorn/app.main import path — the
    preview_start failure mode — and never the CLI or the verify suites.

    The error is loud + actionable: it names the data dir, the marker, and the
    LOC_TMS_ALLOW_REAL_DATA override so an operator (or a launcher log) knows
    exactly what happened and how to proceed intentionally."""
    if ALLOW_REAL_DATA:
        return
    if not data_dir_is_protected():
        return
    raise ProtectedDataDirError(
        "REFUSING TO START: the data dir\n"
        f"    {DATA_DIR}\n"
        f"is marked PROTECTED (contains the marker file '{PROTECT_MARKER_NAME}') "
        "but this launch did NOT set LOC_TMS_ALLOW_REAL_DATA=1.\n\n"
        "This guards against dev/QA tooling (e.g. a bare 'uvicorn app.main:app') "
        "silently serving the REAL production database. If you meant to serve the "
        "real data, either launch via 'python run.py' (which sets the override "
        "automatically) or set LOC_TMS_ALLOW_REAL_DATA=1 in the environment. To "
        "run against a throwaway dir instead, set LOC_TMS_DATA_DIR to a temp path.\n"
        f"    marker:   {PROTECT_MARKER}\n"
        "    override: LOC_TMS_ALLOW_REAL_DATA=1"
    )


def create_protect_marker() -> bool:
    """Create the `.protected` marker in the resolved DATA_DIR if absent. Returns
    True if it created the marker, False if it already existed. Idempotent + safe
    to call on every sanctioned startup so protection becomes ambient after the
    first `python run.py`. Best-effort: a write failure is swallowed (protection
    is defense-in-depth, never a reason to break a legitimate boot)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if PROTECT_MARKER.exists():
            return False
        PROTECT_MARKER.write_text(
            "Data-dir protection marker (issue #13).\n"
            "Server startup refuses to serve this dir unless "
            "LOC_TMS_ALLOW_REAL_DATA=1 is set (run.py sets it automatically).\n"
            "Delete this file to drop protection.\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


# --- Always-on SQLite encryption at rest (opt-in, install-time) ---
# Chosen at install time like DATABASE_URL (NOT a runtime Setting), because the
# engine is built once at import: LOC_TMS_DB_ENCRYPTION=sqlcipher turns the local
# SQLite file into a transparently-encrypted SQLCipher database — encrypted on
# disk at ALL times, not just on shutdown (unlike the legacy `encrypt_db` vault,
# which leaves plaintext during a run + on a crash). Requires the optional
# `sqlcipher3` binding (see requirements.txt) and a valid ENCRYPTION_KEY. Empty
# (the default) keeps the plain-SQLite path byte-unchanged; ignored on Postgres.
DB_ENCRYPTION = _get("LOC_TMS_DB_ENCRYPTION", "").strip().lower()
DB_ENCRYPTION_ON = IS_SQLITE and DB_ENCRYPTION == "sqlcipher"

# --- Refusal / meta-output guard (see app/services/refusal.py) ---
# Kill switch for the downstream refusal guard: default ON. Every is_refusal() call
# site is gated `config.REFUSAL_GUARD_ENABLED and refusal.is_refusal(...)`, so
# setting this "0" restores EXACTLY the pre-guard behavior (no output is ever
# downgraded to NEEDS_REVIEW by this net) without a redeploy — e.g. if a future
# marker turns out to false-positive in production and the maintainer needs to buy time
# before a fix ships. Mirrors PROMPT_CACHING's env-bool convention.
REFUSAL_GUARD_ENABLED = _get("REFUSAL_GUARD_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# --- TM-matching locks (see PROPOSAL-tm-matching-workflow-step §5) ---
# Kill switch for the hard-lock write-refusal at the sole write seam
# (quality.commit_mt_output) AND for creating new locks: default ON. When a segment
# carries a HARD TM lock a machine-origin write (MT / workflow / TM apply / import) is
# refused and routed to review; with it OFF the seam ignores lock state and `tm_match`
# creates none. ENV-ONLY, read once at import -- this is NOT settings-surfaced (unlike
# allow_mock_egress): flipping it takes a process RESTART (on Fly, changing the secret
# restarts the machine), not a live `config set`. It is the escape hatch if a lock ever
# wrongly withholds a write in production. Inert until a `tm_match` step actually locks a
# segment, so an install that never uses the step is byte-identical either way. Mirrors
# REFUSAL_GUARD_ENABLED's env-bool convention (which is likewise env-only).
TM_LOCKS_ENABLED = _get("TM_LOCKS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# --- Mock-egress guard (see app/services/connectors/egress_guard.py) ---
# Default OFF (guard ON): the connector write-back paths REFUSE to push non-live
# (MockProvider / offline) MT output to a REAL external system, so a keyless run can
# never egress deterministic placeholder text to a real Jira/Airtable/etc. as if it
# were a real translation (the live-QA defect this closes). This is the ENV SEED for
# the `allow_mock_egress` Setting: the runtime authority is settings.get, so `config
# set allow_mock_egress` / the Settings UI flip it live; this only decides the value
# a fresh install starts with (blank => "0" => guard on). Set "1" to permit mock
# output on real systems (e.g. a throwaway staging space you WANT filled with mock).
# Mirrors REFUSAL_GUARD_ENABLED's env-bool convention.
ALLOW_MOCK_EGRESS = _get("LOC_TMS_ALLOW_MOCK_EGRESS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# --- Provider keys (used from Chunk 4) ---
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
GEMINI_API_KEY = _get("GEMINI_API_KEY")
OPENAI_API_KEY = _get("OPENAI_API_KEY")
# Jev QE POC: independent text-only hosted judge.
TYPESAFE_API_KEY = _get("TYPESAFE_API_KEY")
TYPESAFE_MODEL = _get("TYPESAFE_MODEL", "jev-latest")
JEV_REFINE_PROMPT_CAP_CHARS = _get_num("JEV_REFINE_PROMPT_CAP_CHARS", 60000, cast=int)
TYPESAFE_BASE_URL = _get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
TYPESAFE_QE_MEANING_FLOOR = _get_num("TYPESAFE_QE_MEANING_FLOOR", 0.8, cast=float)
if not 0 <= TYPESAFE_QE_MEANING_FLOOR <= 1:
    TYPESAFE_QE_MEANING_FLOOR = 0.8
# The ORIGINAL env-sourced values, captured ONCE at import before any runtime
# override touches the mutable attributes above. In-app cloud key management
# (Setup AI page / `providers key` CLI, see app/services/cloud_keys.py) stores a
# per-provider key encrypted in the Setting table; settings.apply_to_config()
# resolves effective = (decrypted-stored || this env-original) back into the three
# ANTHROPIC/GEMINI/OPENAI attributes, so a stored key OVERRIDES .env while .env
# stays the fallback when no key is stored. Every provider call site reads the
# mutable attribute live, so nothing else changes.
_ENV_ANTHROPIC_API_KEY = ANTHROPIC_API_KEY
_ENV_GEMINI_API_KEY = GEMINI_API_KEY
_ENV_OPENAI_API_KEY = OPENAI_API_KEY

# --- Databricks analytical exporter credentials (composable-core phase 2, Train B) ---
# HOST-PROVIDED ENV ONLY, NEVER PERSISTED (SPEC-databricks-exporter §6.5/§6.6/H4): the
# OAuth access token is memory-cached (databricks_transport), the client secret / PAT
# stay here in env and are redacted from logs, DatabricksExportRun.error, and any UI
# error echo. Absent => transport disabled; `--stage-only` local export still works
# (offline-first). A phase-2.1 admin-settable Fernet-at-rest path is deferred.
#   * OAuth M2M service principal (the supported path): CLIENT_ID + CLIENT_SECRET.
#   * PAT (documented legacy fallback): TOKEN. Same Bearer header; ~free to support.
DATABRICKS_CLIENT_ID = _get("LOC_TMS_DATABRICKS_CLIENT_ID", "")
DATABRICKS_CLIENT_SECRET = _get("LOC_TMS_DATABRICKS_CLIENT_SECRET", "")
DATABRICKS_TOKEN = _get("LOC_TMS_DATABRICKS_TOKEN", "")
# Opt-in gate for the env-gated real-workspace live-QA smoke (skipped by default, like
# the MCP live-QA posture) — the mocks prove the state machine, only a real workspace
# proves the §6.7 grant list is complete.
DATABRICKS_SMOKE = _get("LOC_TMS_DATABRICKS_SMOKE", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# --- S3-compatible BlobStore adapter credentials (SPEC-s3-blobstore §5.2) ---
# Host-provided env, NEVER a Setting, NEVER persisted to the DB, NEVER logged (the
# Databricks H4 posture). Read here as module attributes exactly like the databricks
# credential reads. The SECRET + SESSION_TOKEN are the two values secrets_scrub._live_secrets()
# reads for exact-value scrubbing; the access-key ID is not itself a secret (shape-redacted).
# Static credentials ONLY this train (R2-3): IAM instance profiles / ECS task roles / EKS
# IRSA (botocore metadata / web-identity chains, no static keys) are NOT supported --
# instance-role resolution is the named phase-3.1 follow-up (SPEC §7).
S3_ACCESS_KEY_ID = _get("LOC_TMS_S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = _get("LOC_TMS_S3_SECRET_ACCESS_KEY", "")
S3_SESSION_TOKEN = _get("LOC_TMS_S3_SESSION_TOKEN", "")  # STS/temporary creds; empty => omitted
# Opt-in gate for the env-gated real-store live smoke (skipped by default; NEVER keyed on
# creds-present -- L13 in SPEC §10, so a developer with S3 creds in their shell for live QA
# does not fire a networked/mutating smoke on every default suite run).
S3_SMOKE = _get("LOC_TMS_S3_SMOKE", "").strip().lower() in ("1", "true", "yes", "on")

# --- Provider defaults ---
# Kept at "claude" so a KEYED install with no stored default_provider setting still
# goes live the moment its key is present, and the app-shell mock-fallback banner
# (web/deps.py) still fires on a keyless one. To run OFFLINE on the deterministic
# MockProvider with no keys, set DEFAULT_PROVIDER=mock (or pick Mock in Setup AI) --
# see README's Offline quickstart. A keyless CONFIGURED provider no longer silently
# degrades to mock: providers.get_provider refuses it LOUD (ConfiguredProviderUnavailable),
# so a fresh zero-config install that never set a key gets an actionable failure
# instead of two days of mock placeholder text under green checkmarks (2026-08-07
# prod incident).
DEFAULT_PROVIDER = _get("DEFAULT_PROVIDER", "claude")

# Offline / dev / hermetic-test escape hatch. When set, a keyless BUILT-IN provider
# (claude/gemini/openai/local) falls back to the offline MockProvider instead of
# failing loud (providers.get_provider), and run_completion records the substitution
# HONESTLY (provider_used="mock", plus a WARNING) so it can never reproduce the
# mislabeling that hid the incident. This exists ONLY so a process that is offline
# BY DESIGN -- the hermetic verify suites, which pin scores of fixtures at
# default_provider="claude" while every key is blanked -- keeps its historical
# behavior. TEST-ONLY: DEFAULT-OFF, set solely by the test harness; it MUST stay
# unset on any real install (prod does not set it), or it re-opens exactly the
# silent-mock-fallback footgun this module closes. Read LIVE by get_provider so a
# test can toggle config.MOCK_FALLBACK_OK per-case; a startup WARNING fires when on.
MOCK_FALLBACK_OK = _get("LOC_TMS_MOCK_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")
DEFAULT_MODELS = {
    "claude": _get("DEFAULT_CLAUDE_MODEL", "claude-haiku-4-5"),
    "gemini": _get("DEFAULT_GEMINI_MODEL", "gemini-3.6-flash"),
    "openai": _get("DEFAULT_OPENAI_MODEL", "gpt-4o-mini"),
    "local": _get("DEFAULT_LOCAL_MODEL", ""),
}
PROVIDERS = ["claude", "gemini", "openai", "local"]
# Backup engine for the per-segment MT path (translate.spec_for_project) when the
# project's primary provider errors out. Blank = no fallback configured, matching
# today's behavior. Settable via Setup AI ("default_fallback_provider" setting);
# this env var is just the zero-config/test default.
DEFAULT_FALLBACK_PROVIDER = _get("DEFAULT_FALLBACK_PROVIDER", "")

# Local / self-hosted OpenAI-compatible server (LM Studio, Ollama, vLLM, llama.cpp).
# Air-gapped: when 'local' is the provider, no request leaves the LAN.
LOCAL_BASE_URL = _get("LOCAL_BASE_URL", "")
LOCAL_API_KEY = _get("LOCAL_API_KEY", "lm-studio")


# --- Private tailnet egress for LOCAL/self-hosted ENGINE calls only (opt-in) ---
# A hosted image can carry a userspace Tailscale sidecar so the
# cloud machine can privately reach a home LM Studio / self-hosted model server on
# a private tailnet (no public exposure). docker-entrypoint.sh starts tailscaled with
# a SOCKS5 proxy on localhost:1055 ONLY when TS_AUTHKEY is set; app/services/tailnet.py
# then routes outbound ENGINE http bound for a TAILNET destination (an IP in the
# 100.64.0.0/10 CGNAT range, or a *.ts.net host) through that proxy. EVERYTHING else
# — cloud providers (Anthropic/OpenAI/Gemini) and every connector — is untouched.
#
# TAILNET_PROXY is the SOCKS5 URL used for tailnet destinations. Empty == no sidecar
# == default behavior byte-identical to before. Presence is inferred from TS_AUTHKEY
# (the SAME gate the entrypoint uses to decide whether to start tailscaled), so the
# app-side routing turns on exactly when — and only when — the sidecar is actually
# running. LOC_TMS_TAILNET_PROXY overrides the URL explicitly (a differently-wired
# sidecar, or tests) without needing a real auth key.
# NOTE (2026-09-29): since #288 the entrypoint unsets TS_AUTHKEY before exec, so in the
# deployed app the inference below never fires; the entrypoint therefore EXPORTS
# LOC_TMS_TAILNET_PROXY itself whenever it leaves the sidecar running.
def _tailnet_proxy_default() -> str:
    # Sidecar is started iff TS_AUTHKEY is set & non-empty (see docker-entrypoint.sh).
    return "socks5://localhost:1055" if _get("TS_AUTHKEY", "").strip() else ""


TAILNET_PROXY = _get("LOC_TMS_TAILNET_PROXY", "").strip() or _tailnet_proxy_default()

# Hard per-request timeout on every provider call. Since Chunk-B (2026-07-03) the
# real providers stream internally, so this is now a STALL timeout: the clock
# resets on every received chunk, and a call fails only if NO data arrives for
# PROVIDER_TIMEOUT_S. A working provider can therefore run for many minutes
# safely; a genuinely hung engine still fails fast (~2 min) so failover kicks in.
PROVIDER_TIMEOUT_S = int(_get("PROVIDER_TIMEOUT_S", "120"))

# Backstop hard ceiling on a SINGLE streaming provider call's total wall-clock
# (independent of the stall timer): even a provider that dribbles one token every
# 119s must not run forever. Generous by default so a legitimately long
# translation completes; trips only on a pathological trickle.
PROVIDER_MAX_TOTAL_S = int(_get("PROVIDER_MAX_TOTAL_S", "1800"))

# Ceiling on how long we will honor a 429's Retry-After header before an NMT retry.
# NMT engines (DeepL/Google/Microsoft/Lara/Amazon) return a Retry-After on a rate
# limit; run_completion's retry sleep becomes max(Retry-After, computed backoff) so
# we don't hammer straight back into the limit — but a hostile/buggy header (e.g.
# "Retry-After: 86400") must not park a job for a day, so the honored value is
# clamped to this cap. Absent header -> plain exponential backoff (unchanged).
PROVIDER_RETRY_AFTER_CAP_S = int(_get("PROVIDER_RETRY_AFTER_CAP_S", "60"))

# Extra retry attempts a LOCAL engine (no failover target) gets on outage-shaped errors
# (502/503/504, connection errors, a stream cut off mid-body) so a server restart that
# answers 502 for ~40 s does not fail a whole run in seconds (#606). With the default
# backoff the waits are 2, 4, 8, 16, 16, 16, 16 s (5 extra attempts); 0 turns it off.
LOCAL_OUTAGE_RETRIES = int(_get("LOCAL_OUTAGE_RETRIES", "5"))

# --- Prompt caching (Feature 0) ---
# Anthropic prompt caching is ON by default: the stable system prefix of every
# translation call is marked cache_control=ephemeral.
#
# HONEST CAVEAT — WHEN THIS ACTUALLY SAVES MONEY (read before believing the badge):
# Anthropic only caches a prefix that is AT LEAST the model's minimum cacheable
# length: 4096 tokens for haiku-4-5 (our DEFAULT model) and opus-4-*, 2048 for
# sonnet-4-6, 1024 for sonnet-4-5. Below that the marker is a silent NO-OP —
# cache_creation_input_tokens stays 0, no error, no charge. Our translation system
# prefix (prompts.translation_prompt) is only ~128 tokens, and the glossary + the
# per-segment source live in the USER message, not the system block. So on the
# dominant per-segment path the cache NEVER engages on any current model, and on
# haiku it never engages regardless. What actually eliminates repeat input spend on
# that path is the Translation-Memory short-circuit (100% seg-TM / full-text-TM
# hits skip the provider entirely), NOT prompt caching. The marker is kept because
# it is free and engages automatically if a caller ever supplies a large-enough
# stable system prefix (e.g. a big self-hosted instruction block); it is not a
# reliable saver for stock segment translation. OpenAI (>1024-tok prompts) and
# Gemini cache implicitly (no opt-in) — we only capture their reported cached-token
# counts. Set PROMPT_CACHING=0 to drop the explicit Anthropic marker (cached-token
# usage capture is unaffected).
PROMPT_CACHING = _get("PROMPT_CACHING", "1").strip().lower() not in ("0", "false", "no", "off")

# Per-model MAX output-token ceiling, used to (a) size max_tokens from the input
# so a long translation is never silently truncated at a fixed cap, and (b) clamp
# that estimate. Keyed by model-id substring (FIRST match wins — so the MORE SPECIFIC
# ids MUST be listed before the generic ones, e.g. claude-3-5 / claude-3-7 before the
# bare claude-3 legacy key); DEFAULT applies to anything unrecognized.
#
# CRITICAL: this table CLAMPS the max_tokens we actually send, so a value MUST NOT
# exceed a model's REAL maximum — the API 400s ("max_tokens greater than the maximum")
# on an over-statement, which is a hard, non-retryable failure. When unsure for a
# family, keep a safe KNOWN-GOOD (lower) value: under-stating only costs a little
# output headroom (the escalate-on-truncation retry + a clear ceiling error cover the
# rare genuinely-huge output), while over-stating breaks every large request.
#
# Audited 2026-07 against the Anthropic/OpenAI/Google model docs. Prior values
# over-stated the Claude 4 ceilings (128000 vs the real 32K/64K caps) — corrected
# down; added the 3.7 / legacy-3 / gpt-4.1 / gemini-2.5 rows the substring table
# previously missed (they fell through to DEFAULT=8192).
MODEL_OUTPUT_CEILING = {
    # Anthropic (standard, non-beta output caps)
    "claude-opus-4": 32000,  # Opus 4 / 4.1 — 32K output
    "claude-sonnet-4": 64000,  # Sonnet 4 / 4.5 — 64K output
    "claude-haiku-4-5": 64000,  # Haiku 4.5 — 64K output
    "claude-3-7": 64000,  # 3.7 Sonnet — 64K output (128K only under a beta header)
    "claude-3-5": 8192,  # 3.5 Sonnet / Haiku — 8192 output
    "claude-3": 4096,  # legacy Claude 3 Opus/Sonnet/Haiku — 4096 output
    # OpenAI
    "gpt-4.1": 32768,  # GPT-4.1 family — 32K output
    "gpt-4o": 16384,  # GPT-4o / 4o-mini — 16384 output
    "gpt-4": 4096,  # GPT-4 / 4-turbo — 4096 output (safe known-good)
    # Google
    "gemini-2.5": 65536,  # Gemini 2.5 Pro / Flash — 64K output
    "gemini": 8192,  # Gemini 1.5 / 2.0 — 8192 output
    "DEFAULT": 8192,
}


def model_output_ceiling(model: str | None) -> int:
    """The max output tokens a model will emit (first substring match wins)."""
    m = (model or "").lower()
    for key, cap in MODEL_OUTPUT_CEILING.items():
        if key != "DEFAULT" and key in m:
            return cap
    return MODEL_OUTPUT_CEILING["DEFAULT"]


# Rough throughput (output tokens/second) per provider, for the pre-flight
# duration estimate ONLY (never a live count). A settable constant, not measured.
PROVIDER_TOKENS_PER_SEC = {
    "claude": int(_get("TPS_CLAUDE", "80")),
    "gemini": int(_get("TPS_GEMINI", "120")),
    "openai": int(_get("TPS_OPENAI", "80")),
    "local": int(_get("TPS_LOCAL", "30")),
    # openai_compatible hosts (OpenRouter/DeepSeek/Groq/…) are addressed as
    # engine:<id> so the pre-flight estimator falls back to the mock tps today
    # (like every other EngineProfile kind); this keyed default is kept for the
    # documented per-provider map + any future by-kind estimate.
    "openai_compatible": int(_get("TPS_OPENAI_COMPATIBLE", "80")),
    # NMT engines (Lara/Amazon) are addressed as engine:<id>; like the other NMT kinds
    # the pre-flight estimator falls back to the mock tps today. These keyed defaults
    # document a per-kind throughput for any future by-kind estimate.
    "lara": int(_get("TPS_LARA", "80")),
    "amazon": int(_get("TPS_AMAZON", "120")),
    "mock": 1000,
}

# --- Ingestion / chunking ---
# Conservative per-request budget the orchestrator chunks large docs against.
MAX_CHUNK_TOKENS = int(_get("MAX_CHUNK_TOKENS", "4000"))
# #683: the share of an engine's output ceiling one MT request may be planned to use. A
# segment whose (generously) estimated translation is larger is translated in pieces cut at
# sentence / clause / space boundaries and rejoined (app/services/mt_chunking.py). 0 turns
# the piece planner off (every segment goes as one request, as before).
MT_CHUNK_OUTPUT_FILL = max(0.0, min(1.0, float(_get("MT_CHUNK_OUTPUT_FILL", "0.5") or 0)))


# --- Under-translation guard (honest-failure, 2026-08-13 big-doc flood) ---
# The FULL_TEXT chunk loop trips LOUD when a chunk's output is an implausibly tiny
# fraction of its input, so a run FAILS honestly instead of committing COMPLETED over
# a mostly-untranslated body. The observed failure: a sentence-tuned local model
# (qwen3.5-9b-loc-esfr-v1) emitted ~74 output chars for a 9,211-token chunk (char
# ratio ~0.008-0.016 across runs) — nonzero, so it slipped past the empty/blank
# guards, and the step + run reported green while export served ENGLISH source.
#
# Char-based ratio, NOT usage tokens: prompt_tokens carries template/system/glossary
# overhead that would depress the ratio dishonestly. UNDER_TRANSLATION_MIN_RATIO is
# the floor `len(out.strip()) / max(1, len(input))` must clear; below it the chunk
# fails. Default 0.05 is far below any plausible translation — the MOST compressive
# legitimate pair (en->CJK) still lands ~0.3 char ratio — so a false positive on a
# real translation is structurally unlikely, while the observed 0.008-0.016 failure
# is caught with a wide margin. Set "0" (or false/no/off) to DISABLE (the kill switch:
# restores the pre-guard behavior). Mirrors REFUSAL_GUARD_ENABLED / TM_LOCKS_ENABLED:
# ENV-ONLY, read ONCE at import (changing it takes a process RESTART, not a live
# `config set`); the chunk loop reads the module ATTRIBUTE live, so a test can monkey-
# patch config.UNDER_TRANSLATION_MIN_RATIO per-case.
#
# UNDER_TRANSLATION_MIN_INPUT_CHARS exempts short chunks/docs: a one-sentence doc
# legitimately produces one-sentence output (a ratio the floor would otherwise trip),
# so the guard fires ONLY when the chunk's input is at least this many chars.
#
# SCOPE LIMITATION (review F6): the guard runs on every FULL_TEXT llm_agent step, and
# every such step is task="translate" today, so the ratio floor is a valid translation
# invariant. A step INTENTIONALLY prompted to emit far-shorter-than-input output (a
# summarize-style FT step) would trip it — a loud FAILED run naming this knob, not a
# silent wrong deliverable. If that use case is ever added, lower or disable this floor
# via the env var. Parsing tolerates junk (mirrors _source_fallback_ceiling): a
# non-numeric ratio -> default 0.05; out of [0.0, 1.0] -> default; a bad int -> 500,
# floored at 0.
def _under_translation_min_ratio() -> float:
    raw = _get("UNDER_TRANSLATION_MIN_RATIO", "0.05").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.05
    return val if 0.0 <= val <= 1.0 else 0.05


def _under_translation_min_input_chars() -> int:
    try:
        val = int(_get("UNDER_TRANSLATION_MIN_INPUT_CHARS", "500"))
    except (TypeError, ValueError):
        return 500
    return val if val >= 0 else 0


UNDER_TRANSLATION_MIN_RATIO = _under_translation_min_ratio()
UNDER_TRANSLATION_MIN_INPUT_CHARS = _under_translation_min_input_chars()


# OVER-TRANSLATION ceiling (run 190 Item D — the symmetric twin of the under-translation
# floor above). A classic segmented `llm_agent` step whose model, handed one short unit
# (a Fluent key value, a UI string), instead returns the WHOLE PAGE — a markdown table
# across languages, a code fence, a multi-line explanation, or a blob many times the
# source length — is NOT a usable single-unit target. Seen live on Gemma AND Sonnet 5
# (the Advanced committee's `_looks_like_translation` guards the SAME class there; classic
# had no reject, only the SOFT `quality.length_runaway` flag at the write seam). This is
# the HARD reject knob: a tripped candidate is retried ONCE with a strengthened single-
# unit instruction, then WITHHELD (routed to review, never committed) — the dump never
# reaches `commit_mt_output`. The check is shape-based (fence / cross-language table /
# multi-line when the source is a single line) PLUS a length ceiling
# `len(candidate) > max(160, len(source) * RATIO + 60)` — the SAME 8x form the Advanced
# guard uses, generous enough that real en->de expansion never trips it.
#
# "0" (or false/no/off) DISABLES the whole guard (the kill switch — restores the pre-guard
# behaviour, leaving only the soft `length_runaway` flag). Mirrors UNDER_TRANSLATION_MIN_
# RATIO / REFUSAL_GUARD_ENABLED: ENV-ONLY, read ONCE at import (a live `config set` does
# not move it — process RESTART), and the segment loop reads the module ATTRIBUTE live so
# a test can monkey-patch config.OVER_TRANSLATION_MAX_RATIO per-case. Parsing tolerates
# junk (non-numeric -> default 8.0; negative -> default).
def _over_translation_max_ratio() -> float:
    raw = _get("OVER_TRANSLATION_MAX_RATIO", "8.0").strip().lower()
    if raw in ("0", "0.0", "false", "no", "off"):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 8.0
    return val if val > 0.0 else 8.0


OVER_TRANSLATION_MAX_RATIO = _over_translation_max_ratio()

# Hard cap on an inbound API document upload (the /api/documents route reads the
# whole body into memory, so an unbounded read is a trivial memory-exhaustion
# vector). The web UI upload is trusted/session-authed; this bounds the token API.
#
# Raised 25 MiB -> 100 MiB (2026-07-27, the maintainer): real-world TBX termbases (the
# Microsoft Terminology Collection is ~54 MB) exceed 25 MB, so the old default
# rejected a legitimate, commonly-used file outright. Uncapped was considered and
# REJECTED: uploads are read fully into memory (read_capped_upload / the TBX+TMX
# parsers below) and the production Fly machine is small, so an unbounded upload
# is a live OOM vector. 100 MiB clears the known-largest real termbase with
# headroom while keeping a hard ceiling. See tm.import_tmx / glossary_tbx.parse_rows
# for the streaming (iterparse) parsers that keep the PARSE-side peak bounded rather
# than materializing the whole tree.
#
# MEASURED CAVEAT (adversarial review 2026-07-27) — streaming bounds the XML tree,
# NOT the import as a whole. The write side accumulated one ORM row per unit plus
# the session identity map before a single commit: a realistically-shaped 28 MB TMX
# (150k small units) peaked at ~732 MB of Python heap, i.e. ~26x the file, which
# projects to several GB at 100 MiB on a 1 GB machine.
#   * TMX — FIXED (2026-07-27, same day): tm.import_tmx now flushes + expunges every
#     tm.TMX_IMPORT_FLUSH_BATCH units inside a SAVEPOINT and keys idempotency off
#     primitives. Same file: 49.7 MB peak (1.76x), re-import 78 MB (2.8x). Pinned by
#     tests/verify_tm_import_bounded_memory.py (peak must stay under 5x the file).
#     NOT A PURE WIN — see the "COST, NOT YET ADJUDICATED" bullet in
#     tm.import_tmx: the batched writes hold SQLite's single WRITER LOCK for the
#     WHOLE import (minutes at this cap) where the old shape held it only for the
#     final flush, so a big TM import can now stall every other writer in the
#     process. Traded an OOM risk for a lock-contention risk; needs a product call.
#   * TBX/CSV — FIXED (2026-07-29, chip task_67a4223c): glossary_tbx.parse_rows
#     now STREAMS its rows as a generator instead of materializing every row as a
#     dict before any write, and glossary._apply_rows flushes + expunges every
#     GLOSSARY_IMPORT_FLUSH_BATCH rows inside a SAVEPOINT (holding primitives, not
#     live GlossaryEntry objects, for anything already flushed) instead of holding
#     one live entry per distinct term for the whole import and committing per new
#     term. Same pattern as the TMX fix above; see both docstrings for the
#     measured before/after peaks on a synthetic ~100k-term TBX and CSV. Pinned by
#     tests/verify_glossary_import_bounded_memory.py.
MAX_UPLOAD_BYTES = int(_get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))

# Hard cap on a fetched connector ATTACHMENT before it is read into memory and
# routed through the file-filter pipeline (Jira outbound attachments, SPEC-
# attachments Phase A). The same memory-exhaustion reasoning as MAX_UPLOAD_BYTES
# applies — a hostile/huge attachment must be rejected by its declared size BEFORE
# download and by the actual byte length AFTER. Defaults to MAX_UPLOAD_BYTES.
MAX_ATTACHMENT_BYTES = int(_get("MAX_ATTACHMENT_BYTES", str(MAX_UPLOAD_BYTES)))

# Batch 14 per-string image context: hard cap on the RAW bytes of one uploaded
# in-context image (multipart stream AND the DECODED length of a base64-in-JSON
# attach), passed to deps.read_capped_upload and enforced after b64decode. Kept small
# relative to MAX_UPLOAD_BYTES -- a context image is a screenshot, not a document. Never
# above MAX_UPLOAD_BYTES.
KEY_IMAGE_MAX_BYTES = min(int(_get("KEY_IMAGE_MAX_BYTES", str(8 * 1024 * 1024))), MAX_UPLOAD_BYTES)

# Host-configured width of labelled student IDs; bounded to keep the detector predictable.
PII_INSTITUTION_ID_DIGITS = int(_get("LOC_TMS_PII_INSTITUTION_ID_DIGITS", "8"))
if not 4 <= PII_INSTITUTION_ID_DIGITS <= 20:
    raise ValueError("LOC_TMS_PII_INSTITUTION_ID_DIGITS must be between 4 and 20")
# Ceiling on the RAW (pre-decode) JSON body of the base64 attach variant, streamed and
# aborted at this size BEFORE json.loads/b64decode (the webhook stream-cap pattern,
# D2-OOM). base64 inflates ~4/3, plus JSON envelope overhead; a small fixed slack covers
# the {"image_base64":"...","mime":"...","caption":"..."} wrapper.
KEY_IMAGE_MAX_RAW_BODY_BYTES = (KEY_IMAGE_MAX_BYTES * 4 // 3) + 8192

# Batch 14 Phase 1.5 (the include_string_images export option): caps on ONE key
# export bundle's image subtree. Unlike the per-key attach caps above, an export can span a
# whole project (many keys x up to MAX_IMAGES_PER_KEY each), so the bundle carries its OWN
# aggregate ceilings, refused BEFORE any partial ZIP is staged. Both are settings-backed
# (key_image_export_max_files / key_image_export_max_bytes) so an operator can tune them;
# these config values are the fallback defaults. FILES = the total count of image members;
# BYTES = the total decoded (plaintext) bytes of those images inside the ZIP. Defaults sized
# for a screenshot-per-key project (500 files ~= 256 MiB of downscaled PNGs).
KEY_IMAGE_EXPORT_MAX_FILES = int(_get("KEY_IMAGE_EXPORT_MAX_FILES", "500"))
KEY_IMAGE_EXPORT_MAX_BYTES = int(_get("KEY_IMAGE_EXPORT_MAX_BYTES", str(256 * 1024 * 1024)))

# F4 (review, unbounded volume): a per-run ceiling on total writes (field PUTs +
# comment POSTs, summed across issues x locales x comments) that any single
# connector translate run will perform before stopping cleanly. Protects a live
# tenant from a first-ever run against a large backlog turning into thousands of
# API calls / comments in one shot; remaining work is simply picked up by the next
# run (nothing is lost — the hash gate still suppresses what already landed).
# Generous default so normal runs never hit it; override per-deployment if needed.
MAX_WRITES_PER_RUN = int(_get("MAX_WRITES_PER_RUN", "500"))

# Hard cap on an inbound connector webhook delivery body (/webhooks/connectors/{id}
# reads the whole body BEFORE the signature can even be checked, and that route is
# unauthenticated at the session/API layer by design — so an unbounded read there is
# a memory-exhaustion vector reachable with NO credentials at all). Webhook payloads
# are thin by contract (id + metadata, never article content — see webhooks.py), so
# this is deliberately small relative to MAX_UPLOAD_BYTES.
WEBHOOK_MAX_BODY_BYTES = int(_get("WEBHOOK_MAX_BODY_BYTES", str(256 * 1024)))

# Max characters of `text` accepted by POST /api/translate/text — the one endpoint
# that takes unbounded free text and sends it as a SINGLE run_completion call (no
# chunking, unlike the document path). Caps both the memory of the buffered body and
# a per-call token bomb; a caller with a large document should use /api/documents
# (which chunks). 200k chars ~= a very long article, far past any single UI string.
TRANSLATE_TEXT_MAX_CHARS = int(_get("TRANSLATE_TEXT_MAX_CHARS", str(200 * 1024)))

# Max code points in ONE user-perceived character (an extended grapheme cluster: a base
# plus its combining marks, joiners, variation selectors and emoji modifiers) accepted by
# POST /api/translate/text (#599). "e" + 5000 combining accents is ONE character of 5001
# code points; an engine tokenizes every mark, so a short-looking string becomes a huge
# output that overflows the input-sized token budget and walks the truncation-escalation
# ladder for minutes. Real text stays far below this: Unicode's stream-safe format allows at
# most 30 combining marks in a row, and the longest emoji ZWJ family with skin tones is 15.
TRANSLATE_TEXT_MAX_CLUSTER_CODEPOINTS = _get_num(
    "TRANSLATE_TEXT_MAX_CLUSTER_CODEPOINTS", 32, minimum=8
)

# Max characters of the raw `--context-json` / `context` string (SPEC-context-
# metadata phase 1) BEFORE it is even handed to json.loads. Two reviewed defects
# this closes: (1) a deeply-nested payload ({"a":{"a":{"a":...) blows Python's C
# recursion limit INSIDE json.loads, raising an uncaught RecursionError past the
# narrow `except (ValueError, TypeError)` at both call sites (app/web/api.py's
# api_upload_document and app/cli/cmd_doc.py's _doc_upload) -> a raw 500/traceback
# instead of the clean 422/CliError the context surface promises everywhere else;
# (2) the `context` Form field was otherwise unbounded (unlike MAX_UPLOAD_BYTES for
# the file itself). A real TranslationContext package renders to <= 600 chars
# (context_pkg.BLOCK_CAP) even before sanitization caps trim it further, so 8 KiB of
# raw JSON is already generous headroom — anything past it is rejected BEFORE
# parsing, so the recursion never has a chance to happen.
CONTEXT_JSON_MAX_CHARS = int(_get("CONTEXT_JSON_MAX_CHARS", str(8 * 1024)))

# Security review 2026-09 (#287): request BODY caps, enforced by the outermost body-limit
# middleware (app/web/body_limit.py) from the Content-Length header before a byte is read,
# and by counting a streamed (chunked) body. Nothing else bounded a body: the CSRF check
# read a form body before authentication and FastAPI parsed a JSON body before the API key
# check, so one anonymous 512 MiB POST peaked at over 2 GiB of memory.
#   * REQUEST_MAX_ANON_BODY_BYTES caps every request that is not signed in (no valid
#     session, no valid API key) and every sign-in, webhook and SCIM path. Sign-in forms,
#     SAML responses, webhook and SCIM payloads are all far below 1 MiB.
#   * REQUEST_MAX_BODY_BYTES caps a signed-in caller (or any caller while sign-in is off).
#     The default is the largest legitimate body: a MAX_UPLOAD_BYTES file sent base64 in
#     JSON (the CLI --remote imports and the key import), plus envelope room.
REQUEST_MAX_ANON_BODY_BYTES = int(_get("REQUEST_MAX_ANON_BODY_BYTES", str(1024 * 1024)))
REQUEST_MAX_BODY_BYTES = int(
    _get("REQUEST_MAX_BODY_BYTES", str(MAX_UPLOAD_BYTES * 4 // 3 + 1024 * 1024))
)

# Security review 2026-09 (#224): zip-bomb guard for the OOXML formats (DOCX, XLSX). The
# upload cap bounds the COMPRESSED file only; python-docx and openpyxl inflate every part
# into memory. Checked from the zip's central directory before either library opens the
# file (app/filters/zip_guard.py), then verified against actual decompression in bounded
# chunks. A forged size cannot hide expansion from the guard.
#   * ZIP_MAX_UNCOMPRESSED_BYTES: the sum of every member's uncompressed size.
#   * ZIP_MAX_MEMBERS: the number of members.
#   * ZIP_MAX_RATIO: the largest uncompressed/compressed ratio of one member, applied to
#     members larger than ZIP_RATIO_MIN_BYTES (small, highly repetitive XML parts are
#     normal and harmless).
ZIP_MAX_UNCOMPRESSED_BYTES = int(_get("ZIP_MAX_UNCOMPRESSED_BYTES", str(256 * 1024 * 1024)))
ZIP_MAX_MEMBERS = int(_get("ZIP_MAX_MEMBERS", "10000"))
ZIP_MAX_RATIO = int(_get("ZIP_MAX_RATIO", "200"))
ZIP_RATIO_MIN_BYTES = int(_get("ZIP_RATIO_MIN_BYTES", str(1024 * 1024)))
# ZIP batch upload (Batch 13, app/filters/zip_batch.py): the most documents one uploaded
# .zip may create. Over it the whole batch is refused and nothing is stored.
ZIP_BATCH_MAX_DOCUMENTS = int(_get("ZIP_BATCH_MAX_DOCUMENTS", "500"))

# A tiny sparse sheet or merged range can expand into billions of cells in openpyxl.
# Bound structure before loading, including rules imports and export reopens. The text
# default is Excel's cell-text limit; rectangle/row limits bound object amplification.
# 500k accommodates ordinary 20,000 x 14 catalogs (280k cells) while retaining a
# rectangle bound: the filter's iter_rows also creates otherwise empty cells.
XLSX_MAX_CELLS = int(_get("XLSX_MAX_CELLS", "500000"))
XLSX_MAX_ROWS = int(_get("XLSX_MAX_ROWS", "100000"))
XLSX_MAX_CELL_CHARS = int(_get("XLSX_MAX_CELL_CHARS", "32767"))
# Shared-string references can multiply a tiny package into gigabytes of ingestion JSON.
XLSX_MAX_TOTAL_TEXT_CHARS = int(_get("XLSX_MAX_TOTAL_TEXT_CHARS", str(8 * 1024 * 1024)))

# Count empty and table paragraphs too, before costly per-paragraph processing.
DOCX_MAX_PARAGRAPHS = int(_get("DOCX_MAX_PARAGRAPHS", "50000"))
# XAML filter: also translate the Content/Text/ToolTip/Header/Title attributes of plain views
# (off by default: only sys:String / x:String resources are translated). Environment, restart.
_xaml_view_attrs = _get("XAML_VIEW_ATTRIBUTES", "").strip().lower()
XAML_VIEW_ATTRIBUTES = _xaml_view_attrs in ("1", "true", "yes", "on")
# HTML Help (.hhc/.hhk/.hhp): when a translation cannot be written in the target language's
# Windows ANSI code page, write UTF-8 with a logged warning instead of refusing the export.
_hh_utf8 = _get("HTMLHELP_UTF8_FALLBACK", "").strip().lower()
HTMLHELP_UTF8_FALLBACK = _hh_utf8 in ("1", "true", "yes", "on")

# #571: one per-document segment cap for EVERY format, checked while parsing and again
# while splitting into sentences, BEFORE any Segment row is written. A 4 MB .txt of 200000
# short lines used to take a single-machine install offline for 13 minutes. The default
# matches DOCX_MAX_PARAGRAPHS; the cap counts SEGMENTS (sentences after splitting), so a
# DOCX under its paragraph cap with more than 50000 sentences, or an XLSX with more than
# 50000 text cells, is now refused. Every parse budget (lines, markup, runs, PDF objects)
# is a multiple of this value. Over the cap is a 413 on /api, a page message on the web and an
# error in the CLI; the document is not stored. Raise it with the MAX_DOCUMENT_SEGMENTS environment
# variable (Fly secret or .env) and restart; each extra target locale multiplies the rows.
MAX_DOCUMENT_SEGMENTS = _get_num("MAX_DOCUMENT_SEGMENTS", 50000, minimum=1)
# #571: longest single unit (one line, paragraph, cell or string) an upload may carry. A
# 4 MB line with no sentence ends is one slow, giant segment; real prose is far shorter.
MAX_SEGMENT_CHARS = _get_num("MAX_SEGMENT_CHARS", 100000, minimum=1000)
# #571 review: ingest writes one Segment row per (segment x target language), so the segment
# cap alone allows 50000 x 40 languages = 2 million rows from one upload. This caps the ROW
# total at upload (refused before anything is stored); 500000 is 50000 segments x 10
# languages or 10000 segments x 50. Environment variable, then restart.
MAX_DOCUMENT_ROWS = _get_num("MAX_DOCUMENT_ROWS", 500000, minimum=1)
# Batch 15 wiki filter: template parameters that ARE translated in MediaWiki uploads
# (every other template text is protected). Comma-separated `Template:param` pairs, e.g.
# `Quote:text,Note:1` (positional parameters by number). Environment variable, then restart.
WIKI_TEMPLATE_PARAMS = _get("WIKI_TEMPLATE_PARAMS", "")
# #587: ceiling on the TOTAL characters (as JSON) one connector run may buffer while fetching
# records from an external source; crossing it refuses the run whole. Python holds a text
# character in 1 byte (ASCII) to 4 bytes plus per-object overhead. Measured at the #587 gate
# (peak working set of one run at this default): about 70 MB for 1 KB ASCII articles, 130 MB for
# CJK, 240 MB for a CJK pull that also builds the document, and about 830 MB for a pathological
# source of tiny JSON objects, which leaves room on a shared 8 GB box that also runs the app,
# SQLite and Caddy. The budget used is the smaller of this ceiling and
# MAX_DOCUMENT_SEGMENTS x MAX_SEGMENT_CHARS / 10, and never below one maximum-size record.
# Environment variable CONNECTOR_RUN_CHAR_BUDGET, then restart.
CONNECTOR_RUN_CHAR_BUDGET = _get_num("CONNECTOR_RUN_CHAR_BUDGET", 50_000_000, minimum=1000)

# Security review 2026-09 (#272): PDF parse limits. A page with too little text is
# rendered to a bitmap for OCR; its size is chosen by the file, so the render scale is
# lowered until the longer edge fits PDF_OCR_MAX_PIXEL_DIM (a normal page at 150 dpi is
# about 1754 px). PDF_MAX_PAGES refuses a document with more pages than that (each blank
# page is one OCR call).
PDF_OCR_MAX_PIXEL_DIM = int(_get("PDF_OCR_MAX_PIXEL_DIM", "4000"))
PDF_MAX_PAGES = int(_get("PDF_MAX_PAGES", "1000"))
# About 1000 A4 scans at 150 dpi; preflight the complete document before rendering.
PDF_OCR_MAX_TOTAL_PIXELS = int(_get("PDF_OCR_MAX_TOTAL_PIXELS", "2500000000"))
# Image localization (#546/#589): total-pixel cap per uploaded image, checked from the header
# before decode (the per-side MAX_PIXEL_DIM guard alone allows 64 MP, about 256 MB of RGBA).
IMAGE_MAX_TOTAL_PIXELS = max(int(_get("IMAGE_MAX_TOTAL_PIXELS", "25000000")), 1)

# --- Security keys ---
# Prefer env vars in production; otherwise persisted to protected keyfiles in the
# data dir (so sessions + encrypted data survive restarts). Treat as secrets.
import secrets  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402


def _keyfile(name: str, generate) -> tuple[str, bool]:
    """Read (or, if absent, generate + write) the keyfile named `name` in DATA_DIR.
    Returns (value, created) where `created` is True iff THIS call generated the
    file (no keyfile existed before). Callers use `created` to tell a persisted
    keyfile from a freshly-generated one — the difference the embedded regenerate-
    trap warning turns on."""
    private_directory(DATA_DIR)
    p = DATA_DIR / name
    if p.exists():
        existing = p.read_text(encoding="utf-8").strip()
        if existing:
            return existing, False
        # #480: a 0-byte or blank keyfile (crash between create and write, ENOSPC)
        # must never be trusted as the secret. Nothing can have been signed or
        # encrypted under an empty key worth keeping, so regenerate atomically.
        _logger.warning("Keyfile %s was empty; generating a new value.", p)
        val = generate()
        _write_keyfile_atomic(p, val, replace=True)
        return p.read_text(encoding="utf-8").strip() or val, True
    val = generate()
    try:
        _write_keyfile_atomic(p, val, replace=False)
    except FileExistsError:
        # Another worker won the race; its file always appears whole (hard link of a
        # fully written temp file), so this read never sees a partial value.
        return p.read_text(encoding="utf-8").strip(), False
    return val, True


def _write_keyfile_atomic(p, val: str, *, replace: bool) -> None:
    """Write `val` to a private temp file in the same directory, fsync it, then move it
    into place so no reader ever sees an empty or partial keyfile. replace=False links it
    (fails with FileExistsError if `p` exists); replace=True overwrites."""
    import tempfile as _tempfile

    fd, tmp = _tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(val)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(tmp, p)
        else:
            try:
                os.link(tmp, p)
            except FileExistsError:
                raise
            except OSError:
                # Filesystem without hard links: fall back to an exclusive rename-free
                # create, still failing with FileExistsError if another worker won.
                fd2 = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd2, "w", encoding="utf-8") as out:
                    out.write(val)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _secret(env_key: str, keyfile_name: str, generate, alt_env: str | None = None):
    """Resolve one runtime secret. Returns (value, source) where source is 'env'
    or 'keyfile'.

    RESOLUTION ORDER (the ordering IS the safety property — see SPEC Train S):
      (a) prefixed env  (LOC_TMS_*)                -> 'env'
      (b) EXISTING keyfile                         -> 'keyfile'
      (c) unprefixed env (alt_env), NON-EMBEDDED   -> 'env'   (no keyfile written)
      (d) generate + write keyfile                 -> 'keyfile'

    Why (b) out-ranks (c): a persisted keyfile is what keeps every ALREADY-RUNNING
    install byte-identical. Honoring the newly-accepted unprefixed spelling must
    never silently switch the key an install already runs under (that would make
    encrypted uploads / connector tokens / the SQLCipher DB unreadable). So the
    unprefixed fallback is consulted ONLY when no keyfile exists yet.

    The unprefixed spelling behaves EXACTLY like the prefixed one when used: pure
    env, source='env', and NO keyfile is ever written for it — so provenance is
    stable across boots (two consecutive boots on the same data dir with only the
    unprefixed var set both resolve source='env', no keyfile created). The SOLE
    behavioral difference between the spellings is precedence: an existing keyfile
    out-ranks the unprefixed var, never the prefixed one.

    EMBEDDED (H2): `SESSION_SECRET`/`ENCRYPTION_KEY` are generic names a HOST app
    very plausibly exports; an embedded Localization OS must not silently adopt the
    host's secret (host rotates -> our data dies). So (c) is gated on
    `not IS_EMBEDDED`; embedded honors only the prefixed spelling.

    Two boot-time warnings (loud via logging), each narrowly scoped:
      * differ-warning: a keyfile out-ranks a DIFFERING unprefixed var (rotation
        trap) -- non-embedded only; identical values fire nothing.
      * embedded ignore-warning: embedded boot lands on a FRESHLY generated keyfile
        while an unprefixed var is present but deliberately ignored."""
    env_val = _get(env_key)
    if env_val:
        # Both spellings set to DIFFERENT values: the prefixed one wins (unchanged),
        # but warn that the unprefixed value is ignored -- non-embedded only. In
        # embedded mode the unprefixed var is never consulted at all (the
        # host-exports-a-generic-SESSION_SECRET archetype must not be nagged), so
        # there is nothing to reconcile and this stays silent. Identical values are
        # silent everywhere.
        alt_val = _get(alt_env) if alt_env else ""
        if alt_val and not IS_EMBEDDED and alt_val != env_val:
            _logger.warning(
                "Secret: both env spellings are set to DIFFERENT values -- the "
                "prefixed %s and the unprefixed %s. The prefixed spelling WINS, so "
                "the %s value is being IGNORED. Set only one spelling (or set both "
                "to the same value) to silence this warning. See docs/SECRETS.md.",
                env_key,
                alt_env,
                alt_env,
            )
        return env_val, "env"

    alt_val = _get(alt_env) if alt_env else ""
    keyfile_exists = (DATA_DIR / keyfile_name).exists()

    # (b) An existing keyfile out-ranks the unprefixed var -> running installs untouched.
    if keyfile_exists:
        val, _created = _keyfile(keyfile_name, generate)
        if alt_val and not IS_EMBEDDED and alt_val != val:
            _logger.warning(
                "Secret: an existing keyfile (%s) is in use, but env var %s is ALSO "
                "set to a DIFFERENT value. The keyfile OUT-RANKS the env var, so the "
                "%s value is being IGNORED and the app keeps running under the keyfile. "
                "If you are trying to ROTATE this secret, setting %s alone does NOT "
                "rotate it -- see the rotation caveats in docs/SECRETS.md section 3. "
                "(Identical values fire no warning.)",
                str(DATA_DIR / keyfile_name),
                alt_env,
                alt_env,
                alt_env,
            )
        return val, "keyfile"

    # (c) Unprefixed env, non-embedded only. Behaves exactly like the prefixed
    #     spelling: pure env, no keyfile written -> provenance stays 'env' forever.
    if alt_val and not IS_EMBEDDED:
        return alt_val, "env"

    # (d) Generate + persist the keyfile (byte-identical to the historical default).
    val, created = _keyfile(keyfile_name, generate)
    if IS_EMBEDDED and alt_val and created:
        _logger.warning(
            "Secret: env var %s is set but is DELIBERATELY IGNORED in embedded mode "
            "(LOC_TMS_EMBEDDED=1). Embedded installs honor ONLY the prefixed spelling "
            "%s, so a generic host-exported %s is never adopted as an install "
            "secret. A keyfile was just generated in DATA_DIR instead -- on a "
            "container WITHOUT a persistent DATA_DIR volume it regenerates every boot, "
            "invalidating all sessions and making encrypted data unreadable. Set %s "
            "(the prefixed spelling) from your secret manager. See docs/SECRETS.md.",
            alt_env,
            env_key,
            alt_env,
            env_key,
        )
    return val, "keyfile"


# SecretStore port seam (composable-core phase 1, Train 4). The two secrets are
# resolved through the named port (`ports.secrets.get().resolve(...)`) rather than a
# bare `_secret(...)` call, so a future KMS/vault adapter can supply the same
# (value, source) pair without touching config. The DEFAULT adapter delegates straight
# back to `_secret` above, so this is byte-identical: same resolution order, same
# provenance markers, same Train-S boot warnings. `app.ports.secrets` imports config
# LAZILY (inside resolve) precisely so this config-import-time consultation does not
# re-enter a half-built config -- see that module's docstring. `secrets` here is the
# stdlib module (imported above for token_hex); the port is aliased to avoid the clash.
from app.ports import secrets as _secrets_port  # noqa: E402

SESSION_SECRET, SESSION_SECRET_SOURCE = _secrets_port.get().resolve(
    "LOC_TMS_SESSION_SECRET",
    ".session_secret",
    lambda: secrets.token_hex(32),
    alt_env="SESSION_SECRET",
)
# #480: whatever its source (env or keyfile), the cookie-signing key must be long
# enough to resist forgery; refuse to boot rather than sign sessions with a weak key.
SESSION_SECRET_MIN_LENGTH = 16
if len(SESSION_SECRET) < SESSION_SECRET_MIN_LENGTH:
    raise RuntimeError(
        f"SESSION_SECRET is too short ({len(SESSION_SECRET)} characters, minimum "
        f"{SESSION_SECRET_MIN_LENGTH}; source: {SESSION_SECRET_SOURCE}). Set "
        "LOC_TMS_SESSION_SECRET to a long random value (for example 64 hex characters), "
        "or delete the data/.session_secret file to have a new one generated "
        "(existing sessions will be signed out)."
    )
ENCRYPTION_KEY, ENCRYPTION_KEY_SOURCE = _secrets_port.get().resolve(
    "LOC_TMS_ENCRYPTION_KEY",
    ".enc_key",
    lambda: Fernet.generate_key().decode(),
    alt_env="ENCRYPTION_KEY",
)


# --- Browser-surface hardening (Batch 10, gaps G-01 / G-02) ------------------
class BrowserSurfaceConfigError(ValueError):
    """A LOC_TMS_SECURE_COOKIES / LOC_TMS_FRAME_ANCESTORS value that cannot be
    honored. Raised at IMPORT (i.e. boot) so a mis-set browser-hardening knob
    fails LOUD there instead of silently degrading security at request time."""


# Secure session cookies (G-02). SessionMiddleware's https_only is fixed at boot
# (add_middleware is import-time), so we resolve ONE boolean here and hand it to the
# customer app's SessionMiddleware. (There were two mounts until 2026-09-20; the
# back-office and its own _ScopedSessionMiddleware were removed.) `auto` (the default)
# turns the Secure flag ON only when we can be sure the app is served over https
# — under Fly (FLY_APP_NAME is set in the machine env) or when an explicit https
# public base is configured (LOC_TMS_PUBLIC_BASE_URL) — so a plain local
# `http://localhost` dev login keeps working (a Secure cookie would never be sent
# back over http). `1`/`0` force it. An UNRECOGNIZED value fails loud (F-3): a typo
# like 'ture' must NOT silently fall back to auto and ship non-Secure cookies.
_SECURE_COOKIES_TRUE = ("1", "true", "yes", "on")
_SECURE_COOKIES_FALSE = ("0", "false", "no", "off")
_SECURE_COOKIES_AUTO = ("auto", "")


def _resolve_secure_cookies(raw: str, *, fly_app_name: str, public_base: str) -> bool:
    r = (raw or "auto").strip().lower()
    if r in _SECURE_COOKIES_TRUE:
        return True
    if r in _SECURE_COOKIES_FALSE:
        return False
    if r in _SECURE_COOKIES_AUTO:
        # auto: enabled iff we can infer an https deployment.
        if (fly_app_name or "").strip():
            return True
        return (public_base or "").strip().lower().startswith("https://")
    raise BrowserSurfaceConfigError(
        f"LOC_TMS_SECURE_COOKIES={raw!r} is not a recognized value. Use one of: "
        "auto (default), or 1/true/yes/on, or 0/false/no/off."
    )


fly_app_name = _get("FLY_APP_NAME", "").strip()
SECURITY_ALLOWED_HOSTS = ""  # settings.apply_to_config pushes the validated runtime value
APP_BASE_URL = ""  # Effective external URL, mirrored by settings.apply_to_config.
TRUSTED_PROXY_IPS = ""  # same host setting feeds scheme forwarding and client_ip

SECURE_COOKIES = _resolve_secure_cookies(
    _get("LOC_TMS_SECURE_COOKIES", "auto"),
    fly_app_name=fly_app_name,
    public_base=_get("LOC_TMS_PUBLIC_BASE_URL", ""),
)


# Anti-framing (G-01). Standalone default = DENY (the app is never legitimately
# framed: the embedded/headless profile serves NO web surface at all — only /api
# + /health — so there is no in-repo iframe consumer). A deployment that DOES need
# to embed the web UI can set LOC_TMS_FRAME_ANCESTORS to a space-separated origin
# list, which the security-header middleware emits as a `frame-ancestors` CSP
# directive INSTEAD of X-Frame-Options (X-Frame-Options cannot express an origin
# allowlist). Blank (default) => X-Frame-Options: DENY.
def _validate_frame_ancestors(raw: str) -> str:
    """F-6: the value is emitted VERBATIM into a Content-Security-Policy response
    header, so reject anything that could inject a second directive or corrupt the
    header — CR/LF and other control chars, non-ASCII (the response path utf-8/
    ascii-encodes header values, so 0x80-0xFF corrupts or crashes at request time),
    and ';' (the CSP directive separator). Fail loud at boot, never a per-request
    500."""
    fa = (raw or "").strip()
    if not fa:
        return ""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in fa):
        raise BrowserSurfaceConfigError(
            "LOC_TMS_FRAME_ANCESTORS contains control characters (CR/LF/etc.); this "
            "would allow CSP header injection. Provide a plain space-separated "
            "origin list, e.g. 'https://host.example https://embed.example'."
        )
    if ";" in fa:
        raise BrowserSurfaceConfigError(
            "LOC_TMS_FRAME_ANCESTORS must not contain ';' (the CSP directive "
            "separator). It is a single frame-ancestors source list only."
        )
    try:
        fa.encode("ascii")
    except UnicodeEncodeError as exc:
        raise BrowserSurfaceConfigError(
            "LOC_TMS_FRAME_ANCESTORS must be ASCII (HTTP header value); "
            "non-ASCII characters are not allowed."
        ) from exc
    return fa


FRAME_ANCESTORS = _validate_frame_ancestors(_get("LOC_TMS_FRAME_ANCESTORS", ""))


def _validate_csp_mode(raw: str) -> str:
    value = raw.strip().lower() or "enforce"
    if value not in {"enforce", "report-only", "off"}:
        raise BrowserSurfaceConfigError("LOC_TMS_CSP_MODE must be enforce, report-only, or off.")
    return value


CSP_MODE = _validate_csp_mode(_get("LOC_TMS_CSP_MODE", "enforce"))


def secret_source(name: str) -> str:
    """Resolved provenance ('env' or 'keyfile') of a runtime secret, so surfaces
    (e.g. the admin Security page) never hand-roll an env check that misses the
    unprefixed spelling or the keyfile-wins rule. `name` accepts 'session' /
    'encryption' (or the full constant/env spellings)."""
    key = (name or "").strip().lower()
    if key in ("session", "session_secret", "loc_tms_session_secret", "session_secret_source"):
        return SESSION_SECRET_SOURCE
    if key in ("encryption", "encryption_key", "loc_tms_encryption_key", "encryption_key_source"):
        return ENCRYPTION_KEY_SOURCE
    raise KeyError(f"unknown secret name: {name!r}")


def sqlcipher_hex_key(encryption_key: str | None = None) -> str:
    """A stable 32-byte SQLCipher raw key, as 64 hex chars, derived from
    ENCRYPTION_KEY.

    SQLCipher's `PRAGMA key = "x'<64 hex>'"` form takes the raw AES key with NO
    key-derivation, so we must hand it exactly 32 bytes. We do NOT pass the raw
    Fernet key bytes (ambiguous: a Fernet key is url-safe-base64 of 32 bytes, but
    feeding those *characters* to SQLCipher would silently mean something else).
    Instead we HKDF-SHA256 the ENCRYPTION_KEY *string* under a fixed,
    domain-separated label into 32 bytes — deterministic across boots, distinct
    from any other use of the same secret, and unambiguous at the PRAGMA."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hk = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"loc-tms:sqlcipher-db-key:v1")
    # `encryption_key` lets a caller derive from an ARCHIVED key (verifying a SQLCipher
    # backup on a host whose live key differs, #525); the default is the live key.
    secret = ENCRYPTION_KEY if encryption_key is None else encryption_key
    return hk.derive(secret.encode("utf-8")).hex()
