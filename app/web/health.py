"""Liveness / readiness probes — the container health surface.

Extracted from home.py (composable-core phase 0, Train 0a) into their own router so
the embedded/headless profile can keep exactly these two endpoints mounted while it
drops the whole Jinja/web surface (home + every other WEB_ROUTER). These are the
container liveness answers for a reverse proxy / Docker / k8s, so they must exist in
EVERY mode. Mounted in ALL modes via app.web.HEALTH_ROUTERS; a normal (non-embedded)
boot serves them at the identical paths with the identical handlers as before.

THE RULE THIS FILE NOW ENFORCES (GitHub #99): a probe answers, always, fast.

Both handlers used to be SYNC `def`, which means FastAPI ran them in the shared anyio
worker threadpool — the same configurable workers every other sync handler uses. On 2026-09-06
eight concurrent classic runs exhausted the DB connection pool; every waiting caller sat
on a worker thread for the full 30s pool timeout; the threadpool filled; and `/health` —
a handler that returns a literal dict and touches nothing — stalled for 20 to 145
seconds behind the queue. Fly's health-check budget is 5s, so on prod that became 35
minutes of 503s ending in an OOM kill.

So: `/health` is a pure `async def` that never enters the threadpool at all, and
`/ready` runs its one DB query on its OWN dedicated thread, on a POOL-LESS connection,
under a wall-clock budget (config.READY_PROBE_TIMEOUT_S) — so it can neither borrow a
shared worker, nor queue behind the connection pool, nor outlast the health-check timeout.
Neither probe can be starved by application load again.

AND A BUSY INSTALL IS NOT AN UNREADY ONE (gate A round 2, MED-1). `/ready` answers 503 only
when the DATABASE cannot answer. A saturated connection pool — the state the doorman in
`app/main.py` exists to survive — is reported as `{"pool": "at_capacity"}` on a 200,
because a machine that is shedding gracefully and draining is a machine that must stay in
the load balancer's healthy set. The alternative is the 2026-09-06 outage: one machine,
`min_machines_running = 1`, so a failing check means the proxy has nowhere to send traffic
and 503s everything, including the graceful capacity page.

Documented in: docs/architecture/deployment.md
"""

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import select

from app import config, db
from app.kernel import saml, security, settings
from app.models import Setting
from app.ports import jobqueue
from app.release import app_version, coarse_version

log = logging.getLogger("loc_tms.health")

router = APIRouter()

# ONE dedicated thread for the readiness query, deliberately not the shared anyio worker
# pool: a probe must never compete with (or be starved by) application work, and it must
# never consume a worker that application work needs. max_workers=1 also bounds the blast
# radius: a stuck probe can pin exactly one thread, never the shared route-worker pool.
_probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="loc-ready")

# The probe currently inside the database, as a plain concurrent Future, so overlapping
# callers SHARE it (see _current_probe). Guarded by its own lock because it is read and
# replaced from the event loop while the worker thread writes its result.
_probe_lock = threading.Lock()
_probe_cf: Future | None = None


def db_ready() -> tuple[bool, str]:
    """The readiness QUERY: does the DATABASE answer? Returns (ok, detail).

    SATURATED IS NOT UNREADY (gate A round 2, MED-1). This query runs on
    `db.emergency_session()` — the pool-LESS engine — not on the shared QueuePool, and that
    is the whole finding. Round 1 moved Fly's health check onto `/ready` while `/ready`
    still ran its SELECT through the pool, so the one state the doorman was built to
    survive (every connection checked out, the app shedding gracefully in 0.00s and
    draining) read as "this machine is not ready". At `min_machines_running = 1` that takes
    the only machine out of the proxy's healthy set and the proxy then 503s EVERYTHING,
    including the graceful capacity page and the operator's own attempt to look at the
    install: the 2026-09-06 outage shape, reproduced by its own fix.

    A pool-less connection separates the two questions cleanly. "Can this process reach the
    database at all" is answered honestly whatever the pool is doing; "is the pool full" is
    reported as a FIELD (`pool` in the /ready body) that a human or a dashboard can read,
    never as a failure. NullPool opens a connection on demand and closes it on release, so
    it owes nothing to the exhausted QueuePool — the same property that lets a crashed run
    record WHY it died.

    Still DELIBERATELY DB-COUPLED: a readiness probe that touches no database is a liveness
    probe, and `/health` already is one. It is a plain module-level function, not an inline
    closure, so the query under test is nameable and patchable from a suite
    (`verify_stress_liveness_pool`) without going through HTTP.

    Historical note: the R1-F1 volume flood saw this probe stall >10s because the prompt-tap
    enrichment held a pooled connection AND a stale-snapshot write across a ~2s replay under
    25-way contention; the cure for THAT was shortening the tap write transaction
    (app/services/tap_enrich.enrich's PHASE 1/PHASE 2 split). The 2026-09-06 flood found the
    other half: the probe answering LATE is itself the outage. Hence the caller-side budget
    AND the pool-less connection, not a weakened check."""
    try:
        with db.emergency_session() as s:
            s.exec(select(Setting).limit(1)).first()
        return True, "ok"
    except Exception as e:
        return False, type(e).__name__


def _current_probe() -> Future:
    """The in-flight readiness query, submitting one only if none is running.

    OVERLAPPING PROBES SHARE ONE QUERY. Two things must both be true at once: a wedged
    database must not accumulate a growing pile of probe work, and a merely CONCURRENT
    probe (a proxy check landing on top of a monitor's, or a client retrying) must never
    be answered 503 by an install that is perfectly healthy. Refusing the second caller
    buys the first property at the cost of the second, and on a single-machine deployment
    a false 'not ready' takes the machine out of rotation exactly as hard as a real one.

    Joining the in-flight future gives both: the DB sees at most ONE readiness query at a
    time (max_workers=1 would serialise them anyway), no queue can grow behind a stuck
    probe, and every caller gets the true answer under its own wall-clock budget. A
    finished future is never reused, so no caller is ever served a cached verdict."""
    global _probe_cf
    with _probe_lock:
        cf = _probe_cf
        if cf is None or cf.done():
            cf = _probe_pool.submit(db_ready)
            _probe_cf = cf
        return cf


@router.get("/health")
async def health(request: Request):
    """Anonymous liveness stays DB-free and does not disclose operational details."""
    return JSONResponse({"status": "ok", "version": coarse_version(app_version())})


@router.get("/admin/health")
def health_admin(request: Request):
    """Operational details require an administrator's signed-in session."""
    if not request.scope.get("session", {}).get("uid") or not security.is_admin(
        security.session_user(request)
    ):
        return JSONResponse({"error": "Administrator sign-in required."}, status_code=403)
    return _health_details(request)


def _health_details(request: Request):
    """Operational diagnostics for the signed-in administrator.

    This synchronous handler is separate from anonymous, async liveness. It reads
    cached service state without querying the database. The detail map retains
    ingress, egress, admission and ledger diagnostics for operator checks.
    """
    from app.kernel import orchestrator

    detail = {"admission_daemon": orchestrator.ADMISSION.health_detail()}
    # The ingress_config VALUE repeats the "ingress_config:" prefix inside each element on
    # purpose (gate round 11 LOW-4): verify_ingress_boundary matches the raw ASGI bytes
    # b"ingress_config: uvicorn proxy headers on" on the body, so main's exact word has to
    # survive the merge into detail["ingress_config"]. The doubled prefix is load-bearing.
    ingress = ["ingress_config: invalid " + key for key in sorted(settings.INGRESS_ERRORS)]
    fly_trust = security.fly_proxy_trust_status()
    if fly_trust:
        ingress.append("ingress_config: " + fly_trust)
    if request.scope.get("ingress_proxy_headers_on"):
        ingress.append("ingress_config: uvicorn proxy headers on")
    detail["ingress_config"] = "; ".join(ingress) if ingress else "ok"
    if not settings.EGRESS_VIEW["valid"]:
        detail["egress_config"] = "invalid"
    elif settings.EGRESS_VIEW["value"].strip():
        detail["egress_config"] = "ok"
    if not getattr(config, "LEDGER_ENVELOPES_ENABLED", True):
        detail["ledger_envelopes"] = "off"
    if jobqueue.DB_BACKEND_ARMED:
        foreign_rows = jobqueue.FOREIGN_INSTANCE_ROWS
        detail["job_queue"] = (
            f"db; foreign instance rows seen at boot ({foreign_rows})" if foreign_rows else "db"
        )
    if saml.PROXY_WARNING:
        detail["saml_proxy"] = saml.PROXY_WARNING
    if config.APP_BASE_URL and not config.SECURITY_ALLOWED_HOSTS and not config.local_host_pin():
        detail["host_pin"] = "unset"
    return JSONResponse(
        {"status": "ok", "version": coarse_version(app_version()), "detail": detail}
    )


@router.get("/ready")
async def ready():
    """Readiness: verifies the DATABASE answers, within a bounded budget.

    Three outcomes, all of them prompt:
      * 200 {"status":"ok","pool":"ok"|"at_capacity"} — the DB answered;
      * 503 {"status":"error","detail":<Type>}        — the DB raised (unchanged shape);
      * 503 {"status":"error","detail":"Timeout"}     — it did not answer inside
        config.READY_PROBE_TIMEOUT_S, which for a readiness probe IS a negative answer.

    `pool` IS A FIELD, NOT A VERDICT (gate A round 2, MED-1). A full connection pool is the
    state the doorman exists to survive — the process is up, answering in milliseconds and
    draining — so it must never take the machine out of the proxy's healthy set. It is
    REPORTED (`"at_capacity"`) so a human or a dashboard can see it, and the SELECT itself
    runs pool-less (see `db_ready`) so a saturated pool cannot make the probe lie.

    A slow answer is a wrong answer here: Fly's check budget is 5s, so a probe that
    blocks for the pool timeout takes the machine out exactly as hard as a probe that
    returns 503 — but without telling anyone why. Timing out honestly lets the operator
    see 'not ready' in the check log instead of a silent timeout.

    Concurrent probes JOIN the one in-flight query rather than queuing or being refused
    (see _current_probe), so the database sees at most one readiness SELECT at a time and
    a healthy install never answers 503 merely because two probes overlapped."""
    pool_state = "at_capacity" if db.pool_has_no_capacity() else "ok"
    try:
        cf = _current_probe()
    except RuntimeError:  # executor gone (interpreter shutdown) — fail closed, not 500
        log.warning("readiness probe could not be scheduled", exc_info=True)
        return JSONResponse({"status": "error", "detail": "Unavailable"}, status_code=503)
    try:
        # shield() is load-bearing, not decoration. `wait_for` cancels what it is waiting
        # on, and `wrap_future` chains cancellation THROUGH to the concurrent future — so
        # an unshielded timeout would call cf.cancel(). A running probe shrugs that off
        # (a thread cannot be cancelled), but a probe still QUEUED would actually be
        # cancelled, and every OTHER caller joined to that same future would then get a
        # CancelledError instead of a verdict: a 500 on the readiness probe, produced by
        # one prober timing out. Shielding keeps the cancellation on this caller's side
        # of the wrapper, which is what the outcomes above promise.
        ok, detail = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(cf)), config.READY_PROBE_TIMEOUT_S
        )
    except (TimeoutError, asyncio.TimeoutError):
        # Only THIS caller stops waiting; the worker thread keeps going and its result
        # still serves whoever else is joined to it.
        return JSONResponse(
            {"status": "error", "detail": "Timeout", "pool": pool_state}, status_code=503
        )
    if ok:
        return JSONResponse({"status": "ok", "pool": pool_state})
    return JSONResponse({"status": "error", "detail": detail, "pool": pool_state}, status_code=503)
