"""Localization OS — a local, MT-first Translation Management System.

Chunks 0–1: create customers/projects, upload documents (segmented or full-text),
parse DOCX into segments, and view the source grid + chunk plan. Later chunks add
TM, glossary, the workflow builder/orchestrator, the CAT editor, LQA, and export.

The HTTP routes live in APIRouter modules under app/web/ (one per feature area);
this module only assembles the app: middleware, error handlers, lifecycle events.
"""

import logging
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import exc as _sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import branding, config, features
from app import db as _db
from app.db import init_db
from app.filters.base import InputLimitError
from app.logging_config import configure_logging, redact_access_path
from app.output_safety import InvalidOutputURL
from app.services import ai_optout as _ai_optout
from app.services import audit, dbcrypt, jobs, languages, mfa, security, settings, vault
from app.services import orchestrator as _orchestrator
from app.web import ALL_ROUTERS, API_ROUTERS, HEALTH_ROUTERS, auth_mfa
from app.web import background_calls as _background_calls_mw
from app.web import body_limit as _body_limit
from app.web import page_guard as _page_guard
from app.web import route_path as routing
from app.web.deps import WorkerBusy as _WorkerBusy
from app.web.deps import (
    _error_page,
    api_identity,
    connector_api_token,  # noqa: F401
)
from app.web.deps import static_error_page as _static_error_page
from app.web.streaming import retry_after as _stream_retry_after

# Configure logging BEFORE the app/_logger is created. No-op unless LOG_FORMAT=json
# (the verify suites never set it, so this changes nothing for them).
configure_logging()

BASE = Path(__file__).resolve().parent

app = FastAPI(
    title=f"{branding.brand_name()} API",
    version="0.4.0",
    description=(
        "Local, MT-first Translation Management System. Every screen in the app is "
        "backed by these HTTP endpoints. Machine-readable schema at **/openapi.json** "
        "(signed-in users and API keys); the human reference is the in-app **/help** page."
    ),
    # Issue #286: FastAPI's default /docs and /redoc pages load swagger-ui / redoc from a
    # public CDN (a floating major version, no Subresource Integrity) and run it on this
    # origin, where it could act as any signed-in user who opened the page. The repo does
    # not vendor those bundles, so the pages are switched off rather than trusted.
    # /openapi.json is served by _openapi_schema below (sign-in or API key), not FastAPI.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _live_title_openapi():
    """OpenAPI schema whose `info.title` is stamped from the LIVE brand on every call.

    The FastAPI constructor `title=` freezes at import (the DB is not up yet). The
    expensive schema body is still built + cached once; only the display title is
    refreshed each render, so an admin brand rename reaches /openapi.json and /docs
    without a restart — the same live-rename guarantee as the web chrome (H1)."""
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema is None:
        app.openapi_schema = get_openapi(
            title=f"{branding.brand_name()} API",
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
    app.openapi_schema["info"]["title"] = f"{branding.brand_name()} API"
    return app.openapi_schema


app.openapi = _live_title_openapi

_OPENAPI_DENIED = "Sign in, or send an API key in the X-Loc-Token header, to read the API schema."


def _openapi_schema(request: Request):
    """GET /openapi.json (issue #286): the full route map, for callers the install knows.

    With the login wall off (single-operator and test installs) it answers everyone, as
    before. With the wall on it answers a signed-in session or any valid API key (the MCP
    server's openapi resource sends its key), and a 401 JSON otherwise, so
    an anonymous visitor no longer gets every route of the install. It stays listed in
    _ALLOW_EXACT so a token caller is not bounced to the sign-in page; this function is
    the gate. /utcp (a read-only GET subset with its own kill switch) is unchanged."""
    if security.auth_required():
        has_session = "session" in request.scope and security.session_user(request) is not None
        if not has_session and api_identity(request) is None:
            return JSONResponse({"ok": False, "error": _OPENAPI_DENIED}, status_code=401)
    return JSONResponse(app.openapi())


app.add_route("/openapi.json", _openapi_schema, include_in_schema=False)

# Embedded/headless profile (Train 0a): no web surface, so no /static mount. A default
# (non-embedded) boot mounts it exactly as before — byte-identical.
if not config.IS_EMBEDDED:
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

_logger = logging.getLogger("loc_tms")


@app.exception_handler(InvalidOutputURL)
async def _invalid_output_url(request: Request, exc: InvalidOutputURL):
    return _error_page(request, 400, str(exc))


@app.exception_handler(InputLimitError)
async def _input_limit_error(request: Request, exc: InputLimitError):
    # Stored files are checked again on export. Cover every HTTP door, including bundles.
    return _error_page(request, 413, str(exc))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """#580: a path/query/form validation failure on /api answers in the app's own error
    shape ({ok:false, error}) like every other /api refusal, instead of FastAPI's bare
    {detail}. `detail` stays alongside so a caller that already reads it keeps working, and
    the status stays 422. Anything outside /api keeps FastAPI's default response."""
    if not routing.route_path(request.scope).startswith("/api/"):
        return await request_validation_exception_handler(request, exc)
    problems = exc.errors()
    first = problems[0] if problems else {}
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    msg = str(first.get("msg") or "invalid request")
    return JSONResponse(
        {
            "ok": False,
            "error": f"invalid {where}: {msg}" if where else f"invalid request: {msg}",
            "detail": jsonable_encoder(problems),
        },
        status_code=422,
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return _error_page(request, exc.status_code, exc.detail or "Request failed.")


def _is_id_overflow(exc: BaseException) -> bool:
    """True ONLY for the SQLite driver's out-of-range-integer bind failure — a
    request-supplied id or numeric query param wider than a signed 64-bit int
    reaching `session.get(...)` / a DB filter (Phase-2 F1/F2). The message is
    CPython's own sqlite3 text ("Python int too large to convert to SQLite
    INTEGER"). Scoped THIS tightly on purpose: an OverflowError from unrelated
    arithmetic (a genuine defect) must NOT be laundered into a clean 4xx — it
    still falls through to the 500 path below."""
    return "too large to convert to sqlite" in str(exc).lower()


@app.exception_handler(OverflowError)
async def _overflow_error(request: Request, exc: OverflowError):
    """Phase-2 F1/F2 shared boundary: a huge id/param that never passed through
    app.services.ids.coerce_id fails only at the SQLite bind, deep past any route
    logic, and the catch-all below turned it into a 500. The already-correct
    sibling cases return a clean 4xx (negative/zero id -> 404, non-numeric/float
    -> 422); an oversized id was the last hole. Closing it HERE at the one DB
    boundary every route shares gives all 149 by-id path routes AND the numeric
    query-param routes (changes?since=, context/resolve?project=&customer=) the
    SAME clean refusal without editing 149 handler signatures. 422 is the code
    pydantic already returns for a non-numeric id on these same routes, so valid
    ids and every other malformed shape are unchanged."""
    if _is_id_overflow(exc):
        return _error_page(request, 422, "Numeric value is out of range.")
    # Not the id-bind signature: a real OverflowError. Do NOT mask it — log and
    # 500 exactly as the catch-all handler would.
    # #537 gate: the path can carry the invite bearer token (/auth/accept/<token>).
    _logger.exception(
        "Unhandled error on %s %s",
        request.method,
        redact_access_path(routing.route_path(request.scope)),
    )
    return _error_page(request, 500, "Something went wrong. The error has been logged.")


@app.exception_handler(_db.WriteContentionError)
async def _write_contention_error(request: Request, exc: _db.WriteContentionError):
    """Flood R2-F1: a LAUNCH write (the run / batch / resume row a POST creates) could not
    commit after the bounded, jittered SQLite writer-lock retry. HONEST + RETRYABLE — no
    partial launch was recorded (the failing transaction rolled back atomically) — so surface
    a 503 with a Retry-After hint (JSON on /api + /webhooks/, themed page on web), never the
    500 traceback the flood produced. DISTINCT from a validation 4xx: the operator should
    simply retry once the write herd drains, not fix their request. One WARN per shed launch
    (not exception+stack) keeps a sustained flood from drowning the log."""
    retry_after = getattr(exc, "retry_after", _db.LAUNCH_CONTENTION_RETRY_AFTER_S)
    _logger.warning(
        "Write contention shed a launch on %s %s (retry_after=%ss)",
        request.method,
        routing.route_path(request.scope),
        retry_after,
    )
    path = routing.route_path(request.scope)
    if path.startswith("/api") or path.startswith("/webhooks/"):
        return JSONResponse(
            {"ok": False, "error": str(exc), "retry_after": retry_after},
            status_code=503,
            headers={"Retry-After": str(retry_after)},
        )
    resp = _error_page(request, 503, str(exc))
    resp.headers["Retry-After"] = str(retry_after)
    return resp


@app.exception_handler(settings.InstallWideRefused)
async def _install_wide_refused(request: Request, exc: settings.InstallWideRefused):
    """#281 / gate round 1 L3: a request tried to write an install-wide security setting
    under multi-org. Nothing was written (set_many refuses before any write). 403 with the
    host-operator sentence: JSON on /api + /webhooks/, the themed page on the web."""
    path = routing.route_path(request.scope)
    if path.startswith("/api") or path.startswith("/webhooks/"):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
    return _error_page(request, 403, str(exc))


def _retryable_503(request: Request, message: str, retry_after: int, reason: str):
    """The shared CAPACITY refusal: an honest, retryable 503 with a Retry-After hint and a
    machine-readable `reason`, JSON on /api + /webhooks/ and a themed page on the web.

    Same shape as the write-contention 503 above (one refusal vocabulary across the app),
    plus `reason` so a client can branch without string-matching English prose.

    THE WEB HALF READS NOTHING (gate A round 1, HIGH-1). It renders the static page, not
    the themed template: `error.html` calls `app_meta()` (org name, auth state, spend cap)
    and inherits every Jinja global, so the themed render is several database reads — under
    the exact condition this refusal exists for, they cannot get a connection either. The
    gate measured that: `GET /` under an exhausted pool spent 120s inside the ERROR PAGE
    and returned a bodyless 500."""
    if routing.route_path(request.scope).startswith("/api") or routing.route_path(
        request.scope
    ).startswith("/webhooks/"):
        return JSONResponse(
            {"ok": False, "error": message, "reason": reason, "retry_after": retry_after},
            status_code=503,
            headers={"Retry-After": str(retry_after)},
        )
    return _static_error_page(503, message, retry_after)


@app.exception_handler(_orchestrator.RunCapacityError)
async def _run_capacity_error(request: Request, exc):
    """GitHub #99/#100: a launch REFUSED by the run-admission gate.

    The flood's lesson in one handler. An install can only execute so many runs at once —
    each pins a DB connection for its whole life — and admitting more than that did not
    queue them politely: it exhausted the pool, crashed the runs on a connection checkout,
    turned every /api call into a 500 at exactly the 30s pool timeout, and stalled the
    DB-free /health for minutes. Refusing at the door with a retryable 503 keeps the
    install answering, and tells the operator exactly which dial to turn.

    A WARN line (never exception+stack): a refusal is a designed outcome, and a sustained
    burst must not drown the log."""
    _logger.warning(
        "Run capacity refused %s %s on %s %s (in_flight=%s cap=%s)",
        "an" if exc.operation == "export" else "a",
        exc.operation,
        request.method,
        routing.route_path(request.scope),
        getattr(exc, "in_flight", "?"),
        getattr(exc, "cap", "?"),
    )
    return _retryable_503(request, str(exc), exc.retry_after, "run_capacity")


@app.exception_handler(_orchestrator.RunConflictError)
async def _run_conflict_error(request: Request, exc):
    """GitHub #213 part 2: a launch REFUSED because a run of a DIFFERENT workflow is
    already in flight on the same (document, locale) — two workflows writing back to one
    segment set interleave their output (the prod story behind the refusal).

    NOT retryable, so deliberately NOT the 503 shape above: retrying changes nothing
    until the named run is cancelled. 409 with the plain message naming the running run
    and how to cancel it; on /api + /webhooks/ the machine-readable twin
    `{"error": ..., "code": "run_conflict", "run_id": N}`, on the web the themed error
    page (house style for a designed launch refusal, cf. the AI opt-out 409). The CLI
    arms and MCP read the same sentence out of the same body."""
    _logger.warning(
        "Run conflict refused a launch on %s %s (running run #%s)",
        request.method,
        routing.route_path(request.scope),
        getattr(exc, "run_id", "?"),
    )
    path = routing.route_path(request.scope)
    if path.startswith("/api") or path.startswith("/webhooks/"):
        return JSONResponse(
            {"ok": False, "error": str(exc), "code": exc.code, "run_id": exc.run_id},
            status_code=409,
        )
    return _error_page(request, 409, str(exc))


@app.exception_handler(_WorkerBusy)
async def _worker_busy(request: Request, exc):
    """#495: an offloaded blocking call (`deps.offload_parse`, the reviewed bounded stream-worker
    pool) found no free worker in time. Nothing was started, so the refusal is retryable."""
    _logger.warning(
        "Bounded worker pool busy on %s %s", request.method, routing.route_path(request.scope)
    )
    return _retryable_503(request, str(exc), _stream_retry_after(), "worker_busy")


@app.exception_handler(_ai_optout.AIOptOutRefused)
async def _ai_opt_out_refused(request: Request, exc):
    """AI opt-out (HECVAT DPAI-08): an AI feature reached for an opted-out project's
    content. Nothing was sent (the refusal is raised before any engine call). A designed
    outcome, not a crash: 409 with the plain message, JSON on /api + /webhooks/, the
    themed error page on the web."""
    path = routing.route_path(request.scope)
    if path.startswith("/api") or path.startswith("/webhooks/"):
        return JSONResponse(
            {"ok": False, "error": str(exc), "reason": "ai_opt_out"}, status_code=409
        )
    return _error_page(request, 409, str(exc))


@app.exception_handler(_sa_exc.TimeoutError)
async def _pool_exhausted(request: Request, exc: _sa_exc.TimeoutError):
    """GitHub #100: a request could not get a DB connection before `pool_timeout`.

    This is the ONLY exception type the 2026-09-06 flood produced (74 of them in one
    45-minute window), and every one of them reached the client as an opaque 500 —
    including on `GET /api/prompt-tap/runs`, the only live progress signal a long run
    has, so the surface an operator needs most under load was the surface that broke.
    It is a CAPACITY answer, not a server bug: nothing was half-written, and retrying
    once the running work drains is the correct client behaviour. Registered centrally
    because the exhaustion can surface at ANY route's first query, not just a launch."""
    _logger.warning(
        "DB connection pool exhausted on %s %s (%s)",
        request.method,
        routing.route_path(request.scope),
        exc,
    )
    return _retryable_503(
        request, _db.POOL_EXHAUSTED_NOTE, _db.POOL_EXHAUSTED_RETRY_AFTER_S, "db_pool_exhausted"
    )


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    # LAST-DITCH CAPACITY CATCH (GitHub #100). The registered handlers above live inside
    # Starlette's ExceptionMiddleware, so they only see exceptions raised by a ROUTE. This
    # one is the ServerErrorMiddleware handler — the outermost — and is therefore the only
    # place a pool timeout raised by our own MIDDLEWARE (`_security_audit` and
    # `_feature_gate` both read settings, which is a query) can still be answered honestly.
    # Without this, saturating the pool turned those into 500s on every non-exempt path.
    if _db.is_pool_timeout_error(exc):
        return await _pool_exhausted(request, exc)
    # #537 gate: the path can carry the invite bearer token (/auth/accept/<token>).
    _logger.exception(
        "Unhandled error on %s %s",
        request.method,
        redact_access_path(routing.route_path(request.scope)),
    )
    return _error_page(request, 500, "Something went wrong. The error has been logged.")


_ALLOW_PREFIX = ("/static", "/auth", "/api", "/webhooks/", "/scim/")
# a11y T9: the session cookie's signature window (see the SessionMiddleware mount).
SESSION_COOKIE_MAX_AGE = 24 * 60 * 60
# /webhooks/ (trailing slash — SUBPATH-ONLY, the /back-office prefix-glue lesson:
# a future sibling like /webhooks-settings must NOT inherit this carve-out):
# /webhooks/connectors/{id} is SIGNATURE-authed (per-connection webhook secret),
# session-blind by design — an external provider (Zendesk) can't carry our session
# cookie or CSRF token. It reads no request.session and mutates nothing directly
# (it enqueues a job only after verifying the HMAC), so it is safe to exempt from
# the session/CSRF wall exactly like /api. INVARIANT: every route under
# /webhooks/ MUST verify a per-connection signature before doing any work — the
# carve-out is only sound while that holds. See app/services/connectors/webhooks.py.
# /webhooks/vendor-returns is the Wave B sibling under the same carve-out, but the signature
# it verifies is per-VENDOR (the vendor's shared signing_secret, selected by the claim token
# in the X-LocStudio-Claim header), not per-connection. The INVARIANT above still holds
# exactly -- a per-caller signature is verified before any work -- it is just keyed per vendor
# there. See app/services/vendor_return.py:authenticate_callback.
# /api is token-authed, not session-authed. (/back-office was a sixth entry here until
# 2026-09-20; the operator console and its separate session were removed, and the prefix
# went with them. An allow-prefix for a mount that no longer exists is a standing invitation
# to the next route that happens to live under that path.)
# /utcp is the UTCP discovery manual (BACKLOG 6b): a machine-readable document generated
# from the OpenAPI schema. It exposes only a strict READ-ONLY SUBSET (allow-listed GET
# tools) of the /api surface, and gating it would break its purpose (a client fetches the
# manual before it holds any credential). Unlike the FULL schema at /openapi.json, which
# since issue #286 needs a session or an API key when the wall is on, /utcp stays public.
# Its own kill switch (utcp_manual_enabled) 404s it when off. See app/web/utcp.py.
# /metrics (Train Q2) is the Prometheus scrape endpoint. It is TOKEN-authed, NOT
# session-authed (a scraper carries no session cookie), so like /api it is exempt from
# the session wall here and does its OWN admin-key check (app/web/metrics.py
# ._metrics_denied -> deps._require_admin_scope + is_admin -> 401/403 when not an admin
# key). When metrics is off no /metrics route exists (the request 404s in routing
# regardless of this carve-out), so listing it is inert on a default install.
# /openapi.json stays here only so an API-key caller is not redirected to the sign-in
# page: its own handler (_openapi_schema above) requires a session or a key when the wall
# is on. /docs and /redoc were removed with the CDN-loaded pages (issue #286), so those
# paths now get the wall like any other unknown page.
# /connectors/oauth/client-metadata.json (#758) is the OAuth Client ID Metadata Document an
# MCP server's authorization server fetches with no session; it carries no secret (see
# app/web/connectors_common.py). /.well-known/oauth-authorization-server (#753) is the RFC 8414
# metadata an MCP client fetches before it holds any credential; it 404s unless the opt-in
# MCP OAuth authorization server is on (app/services/mcp_as.py). /oauth/token and /oauth/register
# are its machine endpoints (an MCP client has no session); /oauth/authorize only validates and
# redirects to the walled /oauth/consent page (through sign-in with the request in `next`).
_ALLOW_EXACT = {
    "/connectors/oauth/client-metadata.json",
    "/.well-known/oauth-authorization-server",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/register",
    "/openapi.json",
    "/favicon.ico",
    "/health",
    "/ready",
    "/utcp",
    "/metrics",
}
# Content-free public probes bypass only the Host allowlist, never Host syntax.
_HOST_ALLOWLIST_EXEMPT = {"/health", "/ready"}

#: The container liveness surface (app/web/health.py). These two paths must answer
#: WITHOUT touching the database — not just in their handlers, but through the whole
#: middleware chain — because they are what a proxy, Docker, k8s and Fly ask when the
#: install is in trouble, which is exactly when a DB read cannot be relied on to return.
#: See GitHub #99: a saturated connection pool made these two stall 20-145s.
_LIVENESS_PATHS = frozenset({"/health", "/ready"})


def _viewer_writable(path: str) -> bool:
    """Web mutations a VIEWER (rank 0, below can_write) may reach — the route does its OWN
    finer RBAC. SPEC-wave3 §4.5: a viewer (a customer-bound client is capped at viewer rank,
    §2.3) MUST be able to OPEN / reply / answer a query, which is exactly the population RV-1
    names; a blanket can_write gate would exclude them. So the four /queries POST routes opt
    OUT of the blanket can_write gate below — and ONLY those. The query_threads SERVICE still
    enforces the tenant ring (out-of-scope → 404) and the §4.5 role rules (close/reopen need
    author/assignee/reviewer+, assign needs author/reviewer+), so a viewer reaching the route
    can still only do the view-tier verbs. CSRF + auth are unaffected (this carve-out is
    strictly the can_write RANK gate, checked after the session is resolved). Kept a tight
    exact/suffix match so a sibling like /queries-export can never inherit it.

    L-2 (gate fix): normalize a trailing slash before matching. Starlette's redirect_slashes
    357s POST /queries/ -> /queries for a can_write user, but the middleware runs BEFORE
    routing and saw the raw "/queries/" — so a viewer was 403'd on the slash form while a
    can_write user sailed through the redirect. Stripping the trailing slash makes the two
    behave identically (the viewer then follows the same 307 to the no-slash handler)."""
    p = path.rstrip("/") or "/"
    if p == "/queries":
        return True
    return p.startswith("/queries/") and (
        p.endswith("/posts") or p.endswith("/status") or p.endswith("/assign")
    )


#: Train L1 (editable page layouts): the ONLY self-service web writes below can_write rank.
# fullmatch, never `$`: `$` also matches before a trailing newline.
_LAYOUT_PATH_RE = re.compile(r"/account/layouts/[a-z][a-z0-9_]{0,31}")


def _self_service_writable(method: str, path: str) -> bool:
    """Web mutations EVERY signed-in role may reach because they only ever touch the
    caller's OWN row (SPEC-project-layout-editor section 9: every signed-in user edits
    their own layout, viewers included). Exactly PUT and DELETE on
    /account/layouts/<page_key>: the route derives the owner from the session, never
    from input, so a viewer can change nothing but their own page arrangement. Kept
    separate from _viewer_writable (whose L-1 rule keeps /queries POST-only) and an exact
    anchored match, so a sibling path or another verb never inherits it. CSRF, Origin
    and the sign-in wall are unaffected (this relaxes the can_write RANK gate only).

    TOTP train: plus exactly POST on the three two-step sign-in self-service routes. Each
    acts on the session's own account only (app/web/auth_mfa.py derives it from the
    session), so a viewer can set up, replace the recovery codes of, or turn off their OWN
    second factor, and nothing else. Exact set membership, never a prefix.

    Viewer self-service train: plus exactly POST on the five /account settings forms
    (password, display name, interface language, appearance, Get started). Each handler in
    app/web/auth.py takes the account from the session and never from the form or query, so
    a viewer can change their OWN password (current password + policy still checked) and
    preferences, and nothing else. /account/apikeys stays behind can_write."""
    if method == "POST" and (
        path in _MFA_SELF_SERVICE_POSTS or path in _ACCOUNT_SELF_SERVICE_POSTS
    ):
        return True
    return method in ("PUT", "DELETE") and bool(_LAYOUT_PATH_RE.fullmatch(path))


#: The two-step sign-in routes every signed-in role may POST to (their own account only).
_MFA_SELF_SERVICE_POSTS = frozenset(
    {"/account/mfa/setup", "/account/mfa/recovery-codes", "/account/mfa/disable"}
)

#: The /account settings forms every signed-in role may POST to (their own account only).
_ACCOUNT_SELF_SERVICE_POSTS = frozenset(
    {
        "/account/password",
        "/account/name",
        "/account/locale",
        "/account/theme",
        "/account/get-started",
    }
)


#: THE RELIEF VALVE (gate A round 2, MED-7; narrowed and made to WORK in round 3, HIGH-4).
#: The only paths the capacity doorman lets through while it is shedding everything else,
#: because they are how an operator makes the saturation STOP.
#:
#: AN EXPLICIT, CLOSED LIST OF ROUTE SHAPES, not prefixes. Round 2 matched any path ending in
#: `/cancel` plus everything under `/settings/` and `/api/settings/` — 17 routes on the day
#: the gate counted, including `POST /settings/features/{slug}` (install-wide feature flips),
#: `POST /settings/retention`, and `/settings/enterprise/enable|disable`, growing silently
#: with every new route under those prefixes. The valve has to stay small enough that it
#: cannot become the load, so it is enumerated and `verify_stress_liveness_pool` [E2d] pins
#: the enumeration against the app's real route table.
#:
#: * the two run-cancel routes (web + /api). Ending in-flight work is
#:   the single most effective thing an operator can do about a full pool.
#: * `GET /settings` — the page that shows the capacity dials and the install's state.
#:
#: AND THEY GET A CONNECTION. Being exempt from the door is worthless on its own: the only
#: state the valve is ever exercised in is "every pooled connection is checked out", which is
#: exactly the state in which these handlers cannot get one either — the gate measured both
#: cancel routes and `/settings` still answering 503, now after 5.02s of a pinned worker
#: thread each. So the doorman ALSO opens a relief scope (`db.begin_relief_scope`), and every
#: session these requests open comes off the pool-less engine.
_RELIEF_PATHS = re.compile(
    r"^(?:"
    r"/runs/\d+/cancel"
    r"|/api/runs/\d+/cancel"
    r"|/settings"
    r")$"
)


def _is_relief(path: str, method: str = "GET") -> bool:
    p = (path or "").rstrip("/") or "/"
    return _RELIEF_PATHS.match(p) is not None and method == ("GET" if p == "/settings" else "POST")


# Relief sessions are sequential within each request; admit at most four requests.
RELIEF_MAX_CONCURRENT = 4
_relief_slots = threading.BoundedSemaphore(RELIEF_MAX_CONCURRENT)


#: Shed-log rate limiting (gate A round 2, LOW-1). One WARNING per shed request is one line
#: per request for the whole duration of a sustained wedge — the sibling handler
#: `_run_capacity_error` already carries the rule ("a sustained burst must not drown the
#: log") and the doorman IS the burst. So: the first shed of an episode logs immediately,
#: and after that at most one line per minute, carrying how many were shed since the last
#: one. Nothing is hidden, the count is the signal.
_SHED_LOG_INTERVAL_S = 60.0
_shed_log_lock = threading.Lock()
_shed_log_last = 0.0
_shed_log_suppressed = 0


def _shed_episode_over() -> None:
    """A request PASSED the door, so the shedding episode (if any) has ended (round 3,
    LOW-4). Without this the "the first shed of an episode logs immediately" promise held
    only when the previous episode ended more than a minute ago: a second episode ten
    seconds later logged NOTHING at all until the window rolled."""
    global _shed_log_last, _shed_log_suppressed
    if not _shed_log_last:
        return
    with _shed_log_lock:
        if _shed_log_suppressed:
            _logger.warning(
                "Shedding ended; %s request(s) were shed since the last line", _shed_log_suppressed
            )
        _shed_log_last, _shed_log_suppressed = 0.0, 0


def _log_shed(method: str, path: str) -> None:
    global _shed_log_last, _shed_log_suppressed
    now = time.monotonic()
    with _shed_log_lock:
        if _shed_log_last and (now - _shed_log_last) < _SHED_LOG_INTERVAL_S:
            _shed_log_suppressed += 1
            return
        suppressed, _shed_log_suppressed, _shed_log_last = _shed_log_suppressed, 0, now
    _logger.warning(
        "Shedding %s %s at the door: every pooled connection is checked out%s",
        method,
        path,
        f" ({suppressed} more shed in the last {int(_SHED_LOG_INTERVAL_S)}s)" if suppressed else "",
    )


def _is_static(path: str) -> bool:
    """Segment-exact match for the /static mount (the _is_back_office lesson): the mount
    itself or a path under it, never a sibling like /static-assets that a bare
    startswith('/static') would wrongly skip."""
    return path == "/static" or path.startswith("/static/")


def _has_get_route(path: str) -> bool:
    """True when some route answers GET at this app-relative path (so `next` may send
    the user back to it after sign-in without landing on a 405)."""
    from starlette.routing import Match

    from app.web._routes import walk_routes

    probe = {"type": "http", "path": path, "root_path": "", "method": "GET"}
    # walk_routes flattens FastAPI's included routers (their wrapper matches FULL with no
    # usable child scope) into the real routes, in registration order.
    for route in walk_routes(app):
        try:
            match, child = route.matches(probe)
        except Exception:  # noqa: S112 -- a route that cannot match is not a `next` target
            continue
        if match != Match.FULL:
            continue
        # Starlette matches `/memories/{memory_id}` for `/memories/draft` too; FastAPI then
        # answers 422 because memory_id is an int. The FIRST full match is the route the
        # request would really reach, so its path parameters must also validate.
        dependant = getattr(route, "dependant", None)
        params = (child or {}).get("path_params") or {}
        for field in getattr(dependant, "path_params", None) or []:
            try:
                _value, errors = field.validate(params.get(field.alias), {}, loc=("path",))
            except Exception:
                return False
            if errors:
                return False
        return True
    return False


def _expired_next(request: Request, path: str) -> str:
    """Where sign-in should return to after this request found the session gone (a11y T9,
    I5). A navigation (GET/HEAD) returns to its own path. A form POST returns to the PAGE
    the form was on, the same-origin Referer path + query, never the POST route itself;
    an external or unusable Referer is ignored. Everything passes the ONE `next`
    sanitizer the login route uses. Fallback for a POST with no usable Referer: its own
    path when that path also answers GET (today's behaviour), else the dashboard."""
    from app.services import oidc

    if request.method in ("GET", "HEAD"):
        return oidc._sanitize_next(path)
    referer = request.headers.get("referer") or ""
    if referer:
        ref = urlparse(referer)
        if ref.scheme in ("http", "https") and ref.netloc == request.headers.get("host", ""):
            ref_path = ref.path or "/"
            # The Referer carries the EXTERNAL path; `next` is app-relative like `path`.
            root = (request.scope.get("root_path") or "").rstrip("/")
            inside = not root or ref_path == root or ref_path.startswith(root + "/")
            if inside:
                ref_path = (ref_path[len(root) :] or "/") if root else ref_path
                candidate = ref_path + (f"?{ref.query}" if ref.query else "")
                sane = oidc._sanitize_next(candidate)
                # never bounce back to a sign-in / sign-out page after signing in, and
                # never to a POST-only route (some render a page AT their POST URL, so a
                # form on that page carries it as its Referer): GET there would be a 405.
                if (
                    sane == candidate
                    and not ref_path.startswith("/auth/")
                    and _has_get_route(ref_path)
                ):
                    return sane
    return oidc._sanitize_next(path) if _has_get_route(path) else "/"


def _session_expired_response(request: Request, path: str, reason: str = "idle"):
    """The ONE answer to a request whose session just expired (idle or absolute, a11y T9).

    A fetch/XHR call (it carries X-CSRF-Token) gets `401 {"error": "session_expired"}`, so
    its JS can say "you were signed out" instead of parsing the login page as data. A
    plain navigation or form post keeps the 303 to the login page with `expired=1`
    (`reason=max` added when the absolute limit ended it)."""
    if request.headers.get("x-csrf-token"):
        return JSONResponse(
            {"error": "session_expired", "reason": reason},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    nxt = quote(_expired_next(request, path), safe="/")
    tail = "&reason=max" if reason == "max" else ""
    return RedirectResponse(f"/auth/login?next={nxt}&expired=1{tail}", status_code=303)


def _same_origin(request: Request) -> bool:
    """CSRF guard: browsers always send Origin (or at least Referer) on cross-site
    form POSTs, so a mutating request claiming to come from another host is forged.
    Requests without either header (curl, companion apps, tests) aren't browser
    cookie-bearing requests and pass. SameSite=lax cookies are the first layer."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return True
    return urlparse(source).netloc == request.headers.get("host", "")


async def _csrf_ok(request: Request) -> bool:
    """Synchronizer-token CSRF check for mutating, session-authed requests.

    Compares the session token (request.session["csrf"], lazily minted when a
    page renders — see app/web/deps.csrf_token) against the X-CSRF-Token header
    or, for plain HTML form posts, the csrf_token form field. Constant-time
    compare. A request with no session token cannot be validated -> reject.

    The header is checked FIRST and the request body is only read when the
    header is absent AND the content-type is form/multipart. This matters:
    we must NOT consume the body via request.form() here — under Starlette
    1.6.0 that parses through Request.stream(), which sets _stream_consumed
    but never _body, so the middleware _CachedRequest.wrapped_receive() then
    replays an EMPTY body downstream, so required route Form(...) fields are
    missing -> 422. Instead we read the raw body once with request.body(), which
    DOES set request._body; wrapped_receive() replays that full body to the
    route (base.py: "if _body is not None: return it"). For urlencoded we parse
    the csrf_token field out of the raw bytes ourselves; for multipart we can
    then safely call request.form(), because after _body is set Request.stream()
    yields _body (requests.py: `if hasattr(self, "_body"): yield self._body`),
    so form() parses from the cache without re-consuming the receive channel.
    JSON/no-body requests never touch the body, keeping the common path cheap.
    """
    try:
        expected = request.session.get("csrf")
    except (AssertionError, AttributeError):
        expected = None
    if not expected:
        return False
    sent = request.headers.get("x-csrf-token") or ""
    if not sent:
        ctype = (request.headers.get("content-type") or "").lower()
        if ctype.startswith("application/x-www-form-urlencoded"):
            # Read (and cache) the raw body, then parse the field ourselves so
            # we never route the body through form()/stream().
            raw = await request.body()
            values = parse_qs(raw.decode("utf-8", "replace")).get("csrf_token")
            sent = values[0] if values else ""
        elif ctype.startswith("multipart/form-data"):
            # Populate request._body first (safe replay downstream), THEN parse;
            # form() reads from the cached body via stream(), not the channel.
            await request.body()
            form = await request.form()
            sent = form.get("csrf_token") or ""
    # constant_time_eq (not a raw secrets.compare_digest): `sent` is the latin-1-decoded
    # X-CSRF-Token header (or a form field), so a non-ASCII value would make
    # compare_digest(str, str) raise TypeError -> an unauthenticated 500 in this middleware
    # (task_3cc0e09a). A non-ASCII token can never match the minted session token, so this is
    # a clean CSRF mismatch -> the normal 403 / re-login path below.
    return bool(sent) and security.constant_time_eq(str(sent), str(expected))


# #728/#744/#747: `Prefer: respond-async` and `Idempotency-Key` on the billing /api routes.
# Registered FIRST so it is the innermost middleware: every guard added below (auth audit,
# feature gate, body caps, AI scope, capacity) has already run when it sees a request.
app.add_middleware(_background_calls_mw.BackgroundCallMiddleware)


@app.middleware("http")
async def _security_audit(request: Request, call_next):
    """Enforce auth + RBAC when enabled, and audit every mutating request."""
    path = routing.route_path(request.scope)
    skip = path.startswith(_ALLOW_PREFIX) or path in _ALLOW_EXACT
    mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")
    # Access-log client identity (see ACCESS_LOG_CLIENT). Liveness paths keep the socket
    # peer: they must not read settings, and their caller is the local health checker.
    access_holder = request.scope.get(ACCESS_LOG_CLIENT)
    if access_holder is not None and path not in _HOST_ALLOWLIST_EXEMPT:
        resolved = security.client_ip(request)
        if resolved:
            access_holder.append(resolved)
    # /api is token-authed (X-Loc-Token), NOT session-cookie-authed, so it is
    # exempt from the Origin+CSRF checks here. INVARIANT: this is safe ONLY while
    # no /api route reads request.session — a cross-site forged request cannot
    # forge the token header, and carries no session to abuse. If any /api route
    # ever starts trusting the session cookie, remove this carve-out for it.
    # /back-office is a self-contained mount with its OWN Origin/CSRF handling on
    # its own session; the customer-app CSRF here would read the wrong cookie AND
    # consume the request body (breaking the sub-app's Form parsing). Treat it like
    # /api: bypass the customer CSRF/audit and let the mount govern itself.
    # /scim (Train I) is the SCIM 2.0 provisioning mount: token-authed (Bearer or
    # X-Loc-Token via the ApiKey path), session-blind by design — an IdP (Okta/Entra)
    # carries no session cookie or CSRF token. Its own auth (scim.resolve_identity:
    # active + scim-scoped + admin-owned) runs before any body parse, and no /scim route
    # reads request.session, so it is exempt exactly like /api. INVARIANT: this holds
    # only while no /scim route trusts the session cookie.
    # /auth/saml/acs (Train I) is the SAML assertion consumer: an IdP POSTs a signed
    # SAML Response here via the browser, cross-origin and carrying no CSRF token. The
    # signed assertion IS the credential (validated in app/services/saml.process_acs), so
    # this ONE path is CSRF/Origin-exempt like the other credential-bearing POST surfaces.
    # Scoped to the exact path (not the /auth prefix) so no other /auth route loses CSRF.
    _self_governed = (
        path.startswith("/api")
        or path.startswith("/webhooks/")
        or path.startswith("/scim/")
        or path == "/auth/saml/acs"
        # #753: the MCP OAuth token and registration endpoints. An MCP client POSTs here
        # cross-origin with no session or CSRF token; the credential is in the body (code +
        # PKCE verifier, refresh token, or the resource-server secret), validated by
        # app/services/mcp_as.py, and neither route reads request.session. Exact paths only.
        or path in ("/oauth/token", "/oauth/register")
    )
    if mutating and not _self_governed:
        if not _same_origin(request):
            return _error_page(request, 403, "Cross-origin request blocked.")
        # Token-based CSRF, gated behind the login wall: auth-off installs have no
        # distinguishable victim session, so the Origin check alone suffices there
        # (and it keeps single-operator/test mode working without a token). With
        # the wall on, every browser form carries the synchronizer token.
        if security.auth_required() and not await _csrf_ok(request):
            # A fetch/XHR call carries X-CSRF-Token: keep the plain 403 so its JS
            # error handler can react. A plain HTML form post (no such header) is
            # a full-page navigation — a dead-end 403 strands the user, so send
            # them cleanly to re-login exactly like the idle-timeout path does
            # (e.g. after a cross-tab logout/login rotated the token).
            if request.headers.get("x-csrf-token"):
                return _error_page(
                    request, 403, "Your session expired. Please refresh and try again."
                )
            # a11y T9 (I5): return to the page the form was on, never the POST route.
            nxt = quote(_expired_next(request, path), safe="/")
            return RedirectResponse(f"/auth/login?next={nxt}&expired=1", status_code=303)
    # `not skip` FIRST (GitHub #99). `security.auth_required()` is a settings read — a DB
    # query behind a short cache — and Python evaluates the left operand first, so the
    # pre-fix order made every already-exempt path (including /health and /ready, which
    # are in _ALLOW_EXACT precisely because they must always answer) pay for a database
    # round trip it then threw away. With the connection pool saturated that read blocks
    # for pool_timeout, which is how a liveness probe that touches no data of its own
    # still stalled for tens of seconds. Pure short-circuit reorder: identical semantics,
    # since the body only runs when BOTH are true.
    if not skip and security.auth_required():
        # #252: an existence probe, not a count of (or a load of) every account.
        if not security.has_users():
            return RedirectResponse("/auth/setup", status_code=303)
        # a11y T9: idle AND absolute limits; fetch callers get 401 JSON (I5).
        expired = security.enforce_session(request)
        if expired:
            return _session_expired_response(request, path, expired)
        user = security.session_user(request)
        if user is None:
            # #248: quoted like the expired branch, so an encoded "&" in the path cannot
            # add parameters (err=, next=) to the sign-in page URL.
            nxt = quote(path, safe="/")
            return RedirectResponse(f"/auth/login?next={nxt}", status_code=303)
        # TOTP train, gate round 1 LOW-3: mfa_required is enforced on every request, not
        # only at sign-in. A session that never proved a second factor (VERIFIED_KEY unset)
        # is ended as soon as the policy applies to its user: the policy was raised, or the
        # person was promoted to admin under "admins". They sign in again, which asks for
        # the code or sends them to set it up first. Cheap: a cached setting read plus the
        # role and auth_source already on the resolved user; never a factor lookup.
        if request.session.get(auth_mfa.VERIFIED_KEY) != 1 and mfa.required_for(user):
            return auth_mfa.end_unverified_session(request, user, path)
        # Password policy: a session signed in with a password that must change (an admin
        # reset, or older than password_max_age_days) reaches nothing but the change page
        # (/auth/*, which includes sign-out, is exempt above) until the change succeeds.
        if request.session.get("pwc"):
            nxt = quote(path if request.method == "GET" else "/", safe="/")
            return RedirectResponse(f"/auth/password-change?next={nxt}", status_code=303)
        if (
            mutating
            and not security.can_write(user)
            # L-1: the carve-out is POST-only (the query routes are all POST) — a
            # future PUT/PATCH/DELETE under /queries/* must NOT inherit the viewer
            # bypass without a deliberate review.
            and not (request.method == "POST" and _viewer_writable(path))
            and not _self_service_writable(request.method, path)
        ):
            return _error_page(request, 403, "Your role does not allow this action.")

    # #263: one charge at the shared API seam, before handlers or engine work.
    # Session-only web requests and endpoints outside /api keep their own policy.
    if path == "/api" or path.startswith("/api/"):
        from app.web.deps import _rate_limit_block

        ident = api_identity(request)
        if ident is not None:
            limited = _rate_limit_block(request, ident)
            if limited is not None:
                return limited

    from app.services.egress import CALLER_ACTOR, CALLER_IP

    def egress_actor():
        if path.startswith("/api"):
            ident = api_identity(request)
            return ident["actor"] if ident else security.unauthenticated_actor(request)
        email = getattr(security.session_user(request), "email", None)
        if email:
            return email  # auth OFF: the synthetic SYSTEM operator's "system"
        # Session-less web request (auth ON, only the /auth and /webhooks carve-outs get
        # here): a webhook's work runs on the provider's verified signature and keeps
        # "system", the same split _audit_mutating_request makes; anything else is the
        # caller, never the app (gate audit-actor round 1, LOW-4).
        if path.startswith("/webhooks/"):
            return "system"
        return security.unauthenticated_actor(request)

    actor_token = CALLER_ACTOR.set(egress_actor)
    # Liveness paths keep the socket peer: client_ip reads trusted_proxy_ips from the DB,
    # and /health and /ready must touch no DB (#99, #495).
    _peer = (
        (request.client.host if request.client else "") if path in _HOST_ALLOWLIST_EXEMPT else None
    )
    # #578: client_ip is cheap on the loop. It parses a few strings and reads the
    # in-process settings cache (no DNS, no file, no query when warm; the periodic cache
    # probe is shared with every other settings.get). verify_run_state_578 pins that.
    token = CALLER_IP.set(_peer if _peer is not None else security.client_ip(request))
    try:
        # #235: under the fail-closed audit posture the row for a signed-in state-changing
        # request is written BEFORE the handler runs, so a shed audit write refuses the
        # request (retryable 503) and nothing changes unrecorded. Fail-open (the desktop
        # default) keeps the historical write-after shape below, byte for byte.
        try:
            prerecorded = _audit_mutating_request(request, prerecord=True)
        except _db.WriteContentionError as exc:
            return await _write_contention_error(request, exc)
        # Gate round 1 L1: once the request's own row is written, a later audit row in the
        # same request (a handler's detail row) that sheds must not answer 503 "nothing was
        # written" after the handler's change landed. audit.log reads this flag and treats
        # such a shed as fail-open (ERROR + counted, no raise): the request is on record.
        recorded_token = audit.REQUEST_RECORDED.set(prerecorded)
        # Gate round 1 L3: settings writes inside this request are request-door writes, so
        # set_many refuses install-wide security keys under multi-org (one list, every door).
        door_token = settings.REQUEST_DOOR.set(True)
        try:
            response = await call_next(request)
        finally:
            settings.REQUEST_DOOR.reset(door_token)
            audit.REQUEST_RECORDED.reset(recorded_token)
    finally:
        CALLER_IP.reset(token)
        CALLER_ACTOR.reset(actor_token)

    if not prerecorded:
        if response.status_code >= 500:
            # The request ALREADY failed and its handler answered a clean error page (for
            # example sign-out when the revocation storage is down, #138 MED-1; since #251
            # sign-out is a POST, so it gets this middleware row too). When the audit write
            # fails as well (usually the same storage), log it and keep that clean page rather
            # than replace it with a raw traceback. A successful request still fails loud.
            try:
                _audit_mutating_request(request, response.status_code)
            except Exception:
                _logger.exception("audit write failed for a request that had already failed")
            return response
        try:
            _audit_mutating_request(request, response.status_code)
        except _db.WriteContentionError:
            # Gate round 1 L2: under fail-closed a write-after row (a sign-in, a refused
            # /api call, a webhook) that sheds answers a clean retryable 503, never an
            # unhandled 500 out of the middleware. The handler has run, so the message
            # says so rather than claiming nothing was written.
            return _retryable_503(
                request,
                "The request was handled, but its audit record could not be written "
                "because the database is busy. Please retry.",
                _db.LAUNCH_CONTENTION_RETRY_AFTER_S,
                "audit_contention",
            )
    return response


def _audit_mutating_request(
    request: Request, status: int | None = None, *, prerecord: bool = False
) -> bool:
    """Write the audit row _security_audit records for a mutating request. Factored out
    so `_feature_gate` can preserve the trail when it short-circuits with a pre-auth 404
    (see its docstring) -- the audit policy lives in ONE place, so the two callers can
    never disagree about which requests get a row. Returns True when a row was written.

    `prerecord=True` (#235) is the BEFORE-the-handler call: it writes only when audit is on,
    the posture is fail-closed (audit_fail_open=0) and the request resolved an identity (an
    unidentified request's row carries the refusal status, so it stays after). A shed write
    then raises db.WriteContentionError out of here, and the caller answers 503 without
    running the handler. Under fail-open it is a no-op returning False.

    `status` is the HTTP status the request was answered with. It is recorded only on a
    row for a request that resolved NO identity (actor "unauthenticated <ip>", see
    security.unauthenticated_actor), as "refused <status>" in the detail column, so a
    scanner probe reads as the refusal it was. Identified rows are unchanged."""
    path = routing.route_path(request.scope)
    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and not _is_static(path)
        # /scim (Train I) writes its OWN precise, attributed audit rows for every
        # provisioning mutation (scim.user.create/update/deactivate, scim.group.*), keyed
        # to the acting admin + the SCIM key prefix — a generic "POST /scim/... by system"
        # row here would be a poorly-attributed duplicate. The service is the authority.
        and not path.startswith("/scim/")
        # An overlapping retention purge did no work. Its 409 must not create
        # either a purge-completion row or a generic mutation row.
        and not (
            path == "/api/admin/retention/purge"
            and (prerecord or getattr(request.state, "retention_purge_busy", False))
        )
    ):  # the mount audits its own actions
        from app.services import bookkeeping

        if prerecord and not _prerecord_wanted():
            return False
        with bookkeeping.scope():
            # No identity resolved -> "unauthenticated <ip>" (never "anonymous", and never
            # "system", which would pin a stranger's request on the app itself). With auth
            # OFF session_user is the synthetic SYSTEM operator, so only an auth-ON request
            # with no session (the /auth and /webhooks carve-outs) takes that branch on the web
            # side; on /api it is any request without a valid key or trust principal.
            unidentified = False
            resolve_failed = False
            try:
                if path.startswith("/api"):
                    # token-authed requests have no session: attribute the audit row
                    # to the API-key holder (per-user key -> their email; shared
                    # connector token -> "companion-app"), not a generic "system"
                    ident = api_identity(request)
                    unidentified = not ident
                    actor = ident["actor"] if ident else security.unauthenticated_actor(request)
                else:
                    u = security.session_user(request)
                    ident = None
                    actor = getattr(u, "email", "") or ""
                    if not actor and prerecord:
                        # Gate round 1 M1: a session-less web request (a webhook, a sign-in)
                        # is attributed by its OUTCOME (an accepted webhook is "system", a
                        # refused one is `unauthenticated <ip>` + `refused <status>`), which
                        # only exists after the handler. It keeps the write-after row.
                        return False
                    if not actor:
                        # A signature-authed webhook that was ACCEPTED is the provider acting
                        # through a verified per-connection/per-vendor secret; its row keeps
                        # the historical "system" attribution. A refused one is a stranger.
                        if path.startswith("/webhooks/") and (status or 0) < 400:
                            actor = "system"
                        else:
                            unidentified = True
                            actor = security.unauthenticated_actor(request)
            except Exception:
                # #513(4): a failure while RESOLVING the identity (a bad X-Loc-Token during
                # a pool timeout) is not the app acting on itself. Recording "system" pinned
                # an anonymous request on the app and skipped the refusal row; it is an
                # unidentified request, attributed and recorded like any other.
                ident = None
                unidentified = True
                resolve_failed = True
                actor = security.unauthenticated_actor(request)
            # Trust-host-auth (Train 0b): keep the audit ACTOR the trust principal's real
            # email (honest attribution + org-scoped audit filtering both key on it), and
            # make trust-mode requests DISTINGUISHABLE by marking the ACTION. The action is
            # free-form descriptive text (never used for org/actor filtering), so the marker
            # cannot break either. Absent for every token/session identity.
            # #537: a credential path segment (the invite token in /auth/accept/<token>)
            # is a bearer secret; the audit row (and any SIEM feed) names the route
            # without it, the same redaction the access log uses.
            from app.logging_config import redact_access_path

            action = f"{request.method} {redact_access_path(path)}"
            detail = ""
            if isinstance(ident, dict) and ident.get("auth_kind") == "trust":
                action += " (auth=trust)"
                # (a) attribution-only: the host-authenticated end-user the trust proxy
                # forwarded (hard-coupled to the (b) secret; absent otherwise).
                # HIGH-1 (round-2 gate): this lands in the DETAIL column, NOT the action.
                # The canonical path is caller-controlled and decoded, so a forged
                # `on-behalf-of=<victim>` CAN appear inside `action` via the path -- but the
                # AUTHORITATIVE attribution marker lives in `detail`, which is never populated
                # from the request path on this middleware path, so a forged marker is never
                # attributable. `detail` is inside the HMAC payload (tamper-evident) AND is
                # masked by gdpr._mask_other_emails on a subject-access export (MED-2) -- which
                # `action` is not. validate_email already rejected any CRLF/control chars and
                # audit rendering escapes, so an XSS-shaped local part stores + renders escaped.
                obo = ident.get("on_behalf_of")
                if obo:
                    detail = f"on-behalf-of={obo}"
            if unidentified:
                if prerecord:
                    return False  # its row carries the refusal status: written after
                # #513 gate: the anonymous-refusal sampler bounds stranger NOISE. A request
                # the app ACCEPTED (status < 400) whose identity merely failed to resolve
                # here is a real mutation; its row is never sampled away (it used to be
                # written as "system", always).
                accepted = resolve_failed and (status or 0) < 400
                if not accepted and not audit.allow_refusal_record():
                    return False
                detail = security.unauthenticated_detail(request, status)
            detail = "; ".join(p for p in (detail, security.client_ip_detail(request)) if p)
            audit.log(
                actor=actor,
                action=action,
                detail=detail,
                # Shared resolver checks socket trust before Fly or XFF headers.
                ip=security.client_ip(request),
            )
            return True
    return False


def _prerecord_wanted() -> bool:
    """#235: True when the audit row must be written BEFORE the handler (audit on and the
    fail-closed posture). A settings read that fails (a locked DB) answers False, the
    ratified fail-open shape, exactly as audit.log's own guarded posture snapshot does."""
    try:
        return audit.enabled() and not audit.fail_open()
    except Exception:
        return False


@app.middleware("http")
async def _feature_gate(request: Request, call_next):
    """Kill-switch layer 1: a disabled REMOVED-semantics feature's web/api/hooks
    routes answer 404 (never 403 — a switched-off capability is non-enumerable),
    before auth (matching keys.py's accepted posture: unenumerable even to an
    unauthenticated prober). This DUPLICATES the per-route `_flag_off_block` gates
    that stay as layer 2; it closes the gap that a new route under a flagged pattern
    forgets one. Kept out of `_security_audit` (which already carries four jobs and
    the CSRF body-caching subtlety). Placed between `_security_audit` and the
    `_ScopedSessionMiddleware` add below, so the onion is
    session -> _feature_gate -> _security_audit -> router (flag before auth). Hot
    path when nothing is disabled: a few 2s-cached settings reads, no match work.

    AUDIT MIRRORING: a pre-auth 404 here short-circuits _security_audit, which records
    its row AFTER call_next -- i.e. only for requests that already cleared the auth
    wall. Mirroring its SELECTION policy alone (mutating verbs, skip /static +
    /back-office) is not enough: writing a row for an ANONYMOUS probe on an auth-ON
    install would be an unauthenticated, unthrottled audit-write primitive (the chain
    lock serialises the whole app), plus forensic misattribution and log injection via
    the path. So the audit is ALSO gated on REACHABILITY parity -- only requests that
    would have reached _security_audit's post-wall audit (auth off, or an authenticated
    session) get a row. The write is wrapped: an audit failure warns but never changes
    the 404 (the prompt-tap lesson -- warn, don't swallow, never alter the response).
    ACCEPTED RESIDUAL DIVERGENCE: a logged-in user's CSRF-failing mutating request is
    audited here (it 404s before the CSRF check) but would be 403'd unaudited by
    _security_audit; a signed-in probe leaving a trail is the safe direction."""
    path = routing.route_path(request.scope)
    if path in _LIVENESS_PATHS:
        # GitHub #99: the container liveness surface is answered without asking the
        # database anything, ever. `features.path_is_off` is a settings read, and under a
        # saturated pool that read blocks for pool_timeout — so the DB-FREE /health was
        # not DB-free as a REQUEST, only as a handler. Skipping costs no protection: the
        # anti-lockout invariant (tests/verify_features.py [6]) already guarantees no
        # feature may ever gate /health or /ready, so this branch could only ever have
        # returned False for them.
        return await call_next(request)
    if not _is_static(path) and features.path_is_off(path):
        if not security.auth_required() or security.session_user(request) is not None:
            try:
                _audit_mutating_request(request, 404)
            except Exception:
                _logger.warning(
                    "feature-gate: audit write failed for %s %s",
                    request.method,
                    path,
                    exc_info=True,
                )
        return _error_page(request, 404, features.REMOVED_REFUSAL)
    return await call_next(request)


#: Scope key for the per-request access-log client holder. The server runs with
#: --no-proxy-headers, so its access log would name the reverse proxy (Caddy, the Fly
#: edge) for every request. `_security_audit` fills the holder with security.client_ip,
#: the one authority (Fly-Client-IP from trusted peers on Fly, else XFF through
#: trusted_proxy_ips), and `_access_log_send` copies it into the SERVER's own scope at
#: response start, which only the access log reads. The app keeps its copied scope
#: with the socket peer, so no security decision changes.
ACCESS_LOG_CLIENT = "loc_tms.access_log_client"


def _access_log_send(server_scope, holder, send):
    async def wrapped(message):
        if holder and message["type"] in ("http.response.start", "websocket.accept"):
            peer = server_scope.get("client")
            server_scope["client"] = (holder[-1], peer[1] if peer else 0)
        await send(message)

    return wrapped


class HostBoundaryMiddleware:
    """Reject ambiguous Host before sessions, auth, metrics or routing."""

    def __init__(self, app, rejection_app=None):
        self.app = app
        self._scheme_forwarder = None
        self._scheme_trusted = None
        self._outer_proxy_headers = None
        self._proxy_error_logged = False
        self.rejection_app = rejection_app or PlainTextResponse("Invalid Host", status_code=400)

    def _configure_proxy(self):
        # Settings writes push config live; rebuild only when effective trust changes.
        trusted = config.TRUSTED_PROXY_IPS
        if self._scheme_forwarder is None or trusted != self._scheme_trusted:
            self._scheme_forwarder = ProxyHeadersMiddleware(self._forwarded, trusted_hosts=trusted)
            self._scheme_trusted = trusted

    async def _forwarded(self, scope, receive, send):
        scope["headers"] = scope.pop("_ingress_original_headers")
        await self.app(scope, receive, send)

    def _check_proxy_stack(self):
        # Best-effort boot self-check. ASGI has no standard transport-peer or
        # server-config field. Inspect the active wrapper once, without retaining
        # frames; a custom server/task boundary can hide it. Never a trust signal.
        frame = sys._getframe(1)
        try:
            while frame is not None:
                owner = frame.f_locals.get("self")
                if type(owner).__module__ == "uvicorn.middleware.proxy_headers":
                    return True
                frame = frame.f_back
            return False
        finally:
            del frame

    async def __call__(self, scope, receive, send):
        if self._outer_proxy_headers is None:
            self._outer_proxy_headers = self._check_proxy_stack()
        if scope["type"] == "lifespan":
            if self._outer_proxy_headers and not self._proxy_error_logged:
                self._proxy_error_logged = True
                _logger.error(
                    "uvicorn proxy headers are ON; client identity is forgeable; "
                    "launch with --no-proxy-headers"
                )

            async def startup_send(message):
                if message["type"] == "lifespan.startup.complete":
                    self._configure_proxy()
                await send(message)

            await self.app(scope, receive, startup_send)
            return
        if scope["type"] in ("http", "websocket"):
            hosts = [v.decode("latin-1") for k, v in scope["headers"] if k.lower() == b"host"]
            try:
                if len(hosts) != 1:
                    if hosts or scope.get("http_version", "1.1") != "1.0":
                        raise ValueError("Invalid Host")
                    hosts = [scope["server"][0]]
                name = security.host_name(hosts[0])
                # A liveness request must not acquire a database connection.
                # Startup/settings writes push this through apply_to_config.
                allowed = security.ingress_entries(config.SECURITY_ALLOWED_HOSTS)
                local_pin = config.local_host_pin()
                if local_pin:
                    allowed = [*allowed, "localhost", "127.0.0.1", "[::1]"]
                    if config.APP_BASE_URL:
                        from urllib.parse import urlsplit

                        allowed.append(urlsplit(config.APP_BASE_URL).netloc)
                if (
                    allowed
                    and (local_pin or routing.route_path(scope) not in _HOST_ALLOWLIST_EXEMPT)
                    and name not in {security.host_name(h) for h in allowed}
                ):
                    raise ValueError("Invalid Host")
            except (ValueError, KeyError, TypeError):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await self.rejection_app(scope, receive, send)
                return
            self._configure_proxy()
            scheme_scope = dict(scope)
            scheme_scope["ingress_proxy_headers_on"] = bool(self._outer_proxy_headers)
            # Normalize stripping proxies before Starlette mounts extend root_path.
            root = scope.get("root_path", "")
            path = scope["path"]
            if root and path != root and not path.startswith(root + "/"):
                scheme_scope["path"] = root + path
            scheme_scope["_ingress_original_headers"] = scope["headers"]
            scheme_scope["headers"] = [
                (k, v) for k, v in scope["headers"] if k.lower() != b"x-forwarded-for"
            ]
            holder: list[str] = []
            scheme_scope[ACCESS_LOG_CLIENT] = holder
            await self._scheme_forwarder(
                scheme_scope, receive, _access_log_send(scope, holder, send)
            )
            return
        await self.app(scope, receive, send)


def apply_host_boundary(app, *, rejection_app=None):
    """Wrap the final lazy stack, including subsequently installed middleware."""
    build = app.build_middleware_stack
    app.build_middleware_stack = lambda: HostBoundaryMiddleware(build(), rejection_app)


# Security review 2026-09 (#248, #260, #270): the page guard sits just INSIDE the session
# middleware (added before it, so the session wraps it and scope["session"] is live both
# ways) and outside _feature_gate / _security_audit, so every route, template and wall
# redirect sees the filtered query string. It removes free-text message parameters
# (?err=, ?msg=, ?notice=, ...) this session was not sent by the server, records the
# ones a mutating request's redirect carries, and marks signed-in HTML no-store.
app.add_middleware(_page_guard.PageGuardMiddleware)

# Security review 2026-09 (#287): request body caps. Added after the page guard and before
# the session middleware, so it runs just INSIDE the session (scope["session"] is decoded,
# which it reads to tell a signed-in caller from a stranger) and OUTSIDE the page guard,
# _feature_gate and _security_audit: an oversized body is refused with 413 before the CSRF
# check or any route reads a byte of it.
app.add_middleware(_body_limit.BodyLimitMiddleware)

# Added after the security middleware so it wraps it (request.session available).
#
# 2026-09-20: this used to be a _ScopedSessionMiddleware subclass whose ONLY job was to go
# inert on /back-office, because that mount ran its own SessionMiddleware over a separate
# "bo_session" cookie and Starlette hardcodes the session to scope["session"], so both
# running on one request would have collided on that key. With the operator console gone
# there is exactly one session in the app again, and the plain middleware is correct.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    same_site="lax",
    # G-02: Secure flag resolved once at boot (config.SECURE_COOKIES). `auto`
    # default = on under Fly / an https public base, off for local http dev.
    https_only=config.SECURE_COOKIES,
    # a11y T9: the signed cookie's window is a FIXED 24 h bound (it used to be 12 h, which
    # silently capped a longer session_max_hours). The real limits are enforced server-side
    # from the session's own stamps: session_idle_minutes (la) and session_max_hours (iat),
    # both validated to fit inside this window. The window still slides with each re-signed
    # response, so it is a backstop, not the absolute limit.
    max_age=SESSION_COOKIE_MAX_AGE,
)


class _CapacityMiddleware:
    """THE DOORMAN (gate A round 1, HIGH-1). Two jobs, both about not WAITING.

    1. It opens this request's pool-wait budget (`db.begin_pool_budget`), so the request
       may spend at most ONE `pool_timeout` on connection checkouts no matter how many
       queries it runs. The gate measured a single `GET /` performing EIGHTEEN sequential
       checkout timeouts — ~90s of a pinned anyio worker thread, then a bodyless 500.

    2. When the pool has NOTHING left (`db.pool_has_no_capacity` — a non-blocking read of
       the pool's own counters), it answers the retryable 503 immediately, before the
       request touches the database at all. Zero checkout timeouts, milliseconds instead
       of the pool timeout, and — the point — no worker thread parked for the duration.
       Parked threads are what turned the 2026-09-06 stall into an OOM kill (GitHub #109):
       shedding load while saturated is how the pool gets a chance to drain.

    A PURE ASGI middleware, not `@app.middleware("http")`, for two reasons: it runs in the
    request's own task so the ContextVar it sets is visible to everything downstream
    (BaseHTTPMiddleware runs the app in a child task), and it is the cheapest possible
    thing to put in front of every request.

    Added LAST, so it is the OUTERMOST user middleware — outside `_security_audit` and
    `_feature_gate`, both of which read settings (a query). Under an exhausted pool those
    reads were themselves the stall, so a doorman inside them would guard nothing.

    NEVER refuses the liveness surface or static files: `/health` and `/ready` must answer
    while the install is in trouble (that is the whole of #99), and neither touches the
    pool the doorman is protecting.

    AND NEVER REFUSES THE RELIEF VALVE (gate A round 2, MED-7). Shedding 100% of everything
    else includes the actions that would REDUCE the load, so a wedged pool left the operator
    with no in-app move at all and no path back but a process restart — "the flood's worst
    outcome was the silence after it", one layer up. `_is_relief` names the small set that
    is exempt: cancelling a run or a batch, and the settings surface that holds the
    concurrency dials. They are short, bounded, and they are what an operator reaches for
    mid-incident. Nothing else is exempt: the valve must stay small enough that it cannot
    become the load."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        _db.begin_pool_budget()
        path = routing.route_path(scope)
        if path in _LIVENESS_PATHS:
            return await self.app(scope, receive, send)
        if _is_static(path):
            return await self.app(scope, receive, send)
        # Capacity can disappear before the first checkout. The second door check
        # below sheds that race promptly; a later race spends the one-request pool
        # budget. Healthy requests keep the pool as their capacity signal.
        if (
            _is_relief(path, scope.get("method", "GET"))
            and _db.pool_has_no_capacity()
            and _relief_slots.acquire(blocking=False)
        ):
            _db.begin_relief_scope()
            _shed_episode_over()
            try:
                return await self.app(scope, receive, send)
            finally:
                _relief_slots.release()
        if _db.pool_has_no_capacity():
            _log_shed(scope.get("method", "?"), path)
            request = Request(scope, receive)
            response = _retryable_503(
                request,
                _db.POOL_EXHAUSTED_NOTE,
                _db.POOL_EXHAUSTED_RETRY_AFTER_S,
                "db_pool_exhausted",
            )
            return await response(scope, receive, send)
        _shed_episode_over()
        await self.app(scope, receive, send)


# AI opt-out (DPAI-08): scope each request to the project/document/run/segment its URL
# names, so any AI call a route makes passes ai_optout.guard with project context. Pure
# ASGI, reads no database. Added BEFORE the capacity doorman so that stays outermost.
app.add_middleware(_ai_optout.AIScopeMiddleware)
app.add_middleware(_CapacityMiddleware)

# G-01: application-wide security-header middleware. Installed by WRAPPING
# build_middleware_stack (see apply_security_headers) so it is the TRUE outermost
# layer — outside Starlette's ServerErrorMiddleware, so even an unhandled 500
# (rendered by @app.exception_handler(Exception) above) carries the headers, and
# outside the observability metrics/OTel middleware added later at import time. It
# stamps every response — inner auth 303s / CSRF 403s / feature-gate 404s / 500
# error pages — never clobbering a header a route already set. The back-office
# sub-app installs its own copy too (defense-in-depth; the not-present check keeps
# the two copies from duplicating a header).
from app.web import security_headers as _security_headers  # noqa: E402

_security_headers.apply_security_headers(app, frame_ancestors=config.FRAME_ANCESTORS)
apply_host_boundary(
    app,
    rejection_app=_security_headers.SecurityHeadersMiddleware(
        PlainTextResponse("Invalid Host", status_code=400), frame_ancestors=config.FRAME_ANCESTORS
    ),
)


@app.on_event("startup")
async def _configure_worker_limit() -> None:
    import sys

    from anyio import to_thread

    # #615: keep a request responsive while a CPU-bound thread (import parse, export build,
    # search scoring) runs in the same process. See config.GIL_SWITCH_INTERVAL_S.
    sys.setswitchinterval(config.GIL_SWITCH_INTERVAL_S)

    to_thread.current_default_thread_limiter().total_tokens = config.ANYIO_WORKERS


# Required runtime libraries that the app imports lazily, on first use (test speed,
# 2026-10-08; verify_lazy_imports). Before that change a broken image without one of them
# failed at import time; this keeps it failing at server start instead of at the first
# upload or OIDC sign-in. find_spec locates a package without importing it.
_LAZY_REQUIRED_LIBS = {
    "openpyxl": "openpyxl",
    "docx": "python-docx",
    "pypdfium2": "pypdfium2",
    "jwt": "PyJWT",
}


def _require_lazy_libraries() -> None:
    import importlib.util

    missing = []
    for module, dist in _LAZY_REQUIRED_LIBS.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(dist)
    if missing:
        raise RuntimeError(
            "required libraries are not installed: "
            + ", ".join(missing)
            + " (install requirements.lock)"
        )


@app.on_event("startup")
def _startup() -> None:
    _require_lazy_libraries()
    # Data-dir protection guard (issue #13): refuse to serve a `.protected` data
    # dir unless LOC_TMS_ALLOW_REAL_DATA=1 is set. This runs FIRST — before
    # init_db() touches any file — so dev/QA tooling that launched a bare
    # `uvicorn app.main:app` against the real production dir fails loudly here
    # instead of silently serving it. run.py sets the override automatically, so
    # the sanctioned local flow is unchanged; verify suites use throwaway data
    # dirs with no marker, so the guard is inert for them.
    config.assert_data_dir_allowed()
    # On the sanctioned path (override set — i.e. launched via run.py), make the
    # marker ambient: create it in the real repo data dir if absent, so the NEXT
    # unguarded launch is refused. Only for the DEFAULT (real) dir — never stamp a
    # throwaway/temp dir a caller pointed us at explicitly.
    if config.ALLOW_REAL_DATA and config.DATA_DIR_IS_DEFAULT:  # noqa: SIM102
        if config.create_protect_marker():
            _logger.info(
                "Created data-dir protection marker %s (issue #13); "
                "future unguarded server launches against this dir will "
                "refuse to start.",
                config.PROTECT_MARKER,
            )
    init_db()
    if config.IS_SQLITE and settings.get("encrypt_db") == "1" and not dbcrypt.enabled():
        raise RuntimeError(vault.RETIRED_MESSAGE)
    # P-F1: install the real getaddrinfo-backed DNS resolver so the shared connector/vendor
    # SSRF guard (connectors.base._validate_target) resolves-and-checks every hostname target
    # against the private/loopback/link-local/metadata/CGNAT set before any egress. The verify
    # suites deliberately leave _RESOLVER=None (resolution skipped, numeric+localhost bypasses
    # still closed); production must have it on, hence wiring it at startup.
    from app.services.connectors import base as _connbase

    _connbase.install_default_resolver()
    settings.apply_to_config()
    security.check_fly_proxy_at_boot()
    security.check_login_trusted_networks_at_boot()
    if config.APP_BASE_URL and not config.SECURITY_ALLOWED_HOSTS and not config.local_host_pin():
        _logger.warning(
            "security_allowed_hosts is unset; the ingress Host pin is OFF "
            "(app_base_url is configured)"
        )
    from app import extensions as _extensions

    # Optional S3 add-on (Enterprise infrastructure): a no-op when it is not installed.
    _extensions.warn_orphaned_addon_data()  # a downgraded install: add-on rows nobody reaps
    s3_blobstore = _extensions.use("blobstore_s3")  # register the S3 BlobStore
    s3_blobstore.configure_at_boot()  # adapter IFF enabled+configured; else a no-op
    s3_exportstore = _extensions.use("exportstore_s3")  # S3 ExportStore adapter
    s3_exportstore.configure_at_boot()  # exportstore S3 adapter IFF blobstore_s3_enabled; else noop
    from app.services import pii  # NEW (Train P): opt-in Presidio PII NER layer

    pii.configure_at_boot()  # builds the analyzer IFF pii_ner_enabled=1 (fail-loud); else no-op
    from app.services import saml as _saml  # Train I: SAML SP capability check

    _saml.configure_at_boot()  # FAILS BOOT if saml_enabled but python3-saml is missing
    from app.ports import jobqueue as _jobqueue  # Train S: durable job-queue backend selector

    _jobqueue.configure_at_boot()  # arms the DB queue IFF job_queue_backend=db (fail-loud)
    # F5: the offline mock-fallback escape hatch is TEST/OFFLINE-ONLY. If it is ON at
    # startup, warn loudly (never a hard fail) — on a real install it re-opens the
    # silent-mock-fallback footgun this release closes (2026-08-07 incident), so an
    # operator who set it in production must see it every boot.
    if config.MOCK_FALLBACK_OK:
        _logger.warning(
            "LOC_TMS_MOCK_FALLBACK is ON: a keyless CONFIGURED provider will resolve to "
            "the offline MockProvider instead of failing loud. This switch is for the "
            "hermetic tests / an intentionally offline box only and MUST NOT be set on a "
            "real install."
        )
    # KMS-readiness safety net: a Postgres (non-SQLite) deploy is a "real" install,
    # but if its secrets came from auto-generated keyfiles rather than env vars,
    # a container without a persistent DATA_DIR volume regenerates them each boot —
    # silently logging everyone out and making encrypted data unreadable. Warn
    # loudly (never a hard fail). The SQLite/dev default path (incl. tests) is exempt.
    if not config.IS_SQLITE:
        from_keyfile = [
            name
            for name, source in (
                ("SESSION_SECRET", config.SESSION_SECRET_SOURCE),
                ("ENCRYPTION_KEY", config.ENCRYPTION_KEY_SOURCE),
            )
            if source == "keyfile"
        ]
        if from_keyfile:
            _logger.warning(
                "Secrets %s came from auto-generated keyfiles in DATA_DIR, not env "
                "vars. On a container without a persistent DATA_DIR volume these "
                "regenerate each boot, invalidating all sessions and making "
                "encrypted uploads/connector tokens unreadable. Set "
                "LOC_TMS_SESSION_SECRET / LOC_TMS_ENCRYPTION_KEY from your secret "
                "manager or KMS. (The unprefixed SESSION_SECRET / ENCRYPTION_KEY "
                "also work on a non-embedded install with NO existing keyfile -- an "
                "existing keyfile out-ranks them; see docs/SECRETS.md. Embedded mode "
                "honors only the prefixed spelling.)",
                ", ".join(from_keyfile),
            )
    # G-02 visibility (F-2): a self-hosted install behind an nginx / cloudflared /
    # enterprise TLS proxy sets its public https URL as the `app_base_url` SETTING
    # but carries no FLY_APP_NAME, so LOC_TMS_SECURE_COOKIES=auto leaves the Secure
    # flag OFF and session cookies ship without it. There is no way to detect the
    # proxy's scheme at boot (SessionMiddleware is static), so warn LOUDLY (never a
    # hard fail) when the operator's own declared base URL says https but Secure
    # resolved off — the one discoverable signal that the knob needs setting.
    if not config.SECURE_COOKIES:
        base = (settings.get("app_base_url") or "").strip().lower()
        if base.startswith("https://"):
            _logger.warning(
                "Session cookies are NOT marked Secure (LOC_TMS_SECURE_COOKIES "
                "resolved OFF) but app_base_url is an https URL (%s). Behind a "
                "TLS-terminating proxy this ships session cookies without the "
                "Secure flag. Set LOC_TMS_SECURE_COOKIES=1 (or "
                "LOC_TMS_PUBLIC_BASE_URL=https://... so auto-detection engages) "
                "to close G-02.",
                base,
            )
    # A restart loses the in-memory job queue: re-queue never-started jobs and
    # mark the ones that were mid-run as failed (visible, not silently lost).
    # #728/#747: background /api calls (api_task rows) a restart cut off are failed with a
    # message that says the work may have partly run, before the generic sweep below.
    _background_calls_mw.fail_interrupted()
    interrupted = jobs.recover_interrupted(at_boot=True)
    if interrupted:
        _logger.warning("Recovered %s interrupted background job(s) after restart", interrupted)
    # A restart also loses the in-memory batch dispatcher threads (SPEC Phase 5a): a
    # batch cut off mid-flight is left finished_at NULL with members stuck RUNNING, so it
    # would read RUNNING forever and its status page would auto-reload without end. Heal
    # them here (fail orphaned members, finalize the batch) — the batch analogue of the
    # job recovery above.
    from app.services import batch_runs

    interrupted_batches = batch_runs.recover_interrupted_batches(at_boot=True)
    if interrupted_batches:
        _logger.warning(
            "Recovered %s interrupted workflow batch(es) after restart", interrupted_batches
        )
    # ...and the PLAIN single runs, which neither healer above touched (GitHub #44).
    # A plain run's daemon thread dies with the restart while its WorkflowRun row stays
    # RUNNING forever — and start_run's double-submit guard then re-attaches EVERY later
    # attempt at that (workflow, document, locale) to the corpse, so the locale reports
    # "started" and translates nothing, permanently. Fail the orphans (PAUSED is durable
    # human state and is left alone, same rule as the batch healer).
    from app.services import orchestrator as _orchestrator

    interrupted_runs = _orchestrator.recover_interrupted_runs()
    if interrupted_runs:
        _logger.warning("Recovered %s interrupted workflow run(s) after restart", interrupted_runs)
    # ...and RE-ADOPT the fan-out QUEUE (gate A round 3, HIGH-3). A key project's "translate
    # all" larger than the run ceiling parks its remainder as durable QUEUED run rows, which
    # is precisely so a redeploy or a machine auto-stop — both documented-normal on this
    # deployment — does not lose it. Nothing is repaired here: the rows are already right,
    # the admission scheduler is simply started for them again.
    adopted = _orchestrator.adopt_queued_runs()
    if adopted:
        _logger.warning("Adopted %s queued workflow run(s) after restart", adopted)
    # ...and any run-recovery sweep an installed add-on registered (app/extensions.py
    # "tick_runs"); nothing registered means nothing runs and nothing is logged.
    # Best-effort: a failure here must never block startup.
    _extensions.run_sweeps("tick_runs", _logger)
    # Prompt-tap PURGE-STAGING RECONCILIATION (gate round-1 MED-5). A hard kill during a
    # guarded ledger purge can leave `<stem>.purge-<hex>` raw-capture files: settle them
    # against their TapAction row at boot (committed purge -> delete; uncommitted -> restore),
    # independent of the raw-capture switch. Best-effort: never blocks startup.
    try:
        from app.services import ledger_controls

        # force=True: bypass the per-tick reconcile throttle at boot (gate round-2 LOW-1).
        ledger_controls.reconcile_staged_purges(force=True)
    except Exception:
        _logger.exception("Prompt-tap purge-staging reconciliation failed at startup")
    # Gate audit-actor round 2 (LOW-7): flag accounts that predate the lookalike rules. One
    # query, one WARNING naming ids only, never raises, changes nothing.
    security.warn_lookalike_accounts()
    # Real installs (local dev OR a container) turn the login wall on and run the
    # scheduler; tests (throwaway LOC_TMS_DATA_DIR, no LOC_TMS_SERVER) stay
    # unauthenticated + deterministic. See config.IS_TEST / config.IS_SERVER.
    if not config.IS_TEST:
        # P3: fold the legacy local-engine settings into a first-class
        # "Local (migrated)" EngineProfile card (idempotent; no-op once migrated or
        # when no local server is configured). Real installs only, so the verify
        # suites (which drive engines.* directly) stay deterministic.
        from app.services import engines

        engines.migrate_legacy_local()
        # P6: fold the legacy single custom .srx upload into a "Custom (migrated)"
        # SegmentationProfile row (idempotent; no-op when no custom .srx is
        # configured). Real installs only — same reasoning as the engine seed.
        from app.services import segmentation_profiles

        segmentation_profiles.migrate_legacy_custom()
        # Embedded/headless profile (Train 0a). Everything ABOVE this tail (init_db,
        # resolver install, settings apply, crash recovery) already ran — embedded needs
        # all of it. Here the three worlds diverge on wall + seeding; daemons follow.
        if config.IS_EMBEDDED:
            # Embedded/headless: turn the login wall ON. `security_auth_required` is the
            # MASTER switch for /api RBAC — with it OFF, _role_block no-ops and ANY valid
            # token writes at ANY role (a viewer key reaches manager/admin verbs), and
            # multi-org / customer confinement fall open too. /api was NEVER session-
            # authed (it is under _ALLOW_PREFIX, token-only via X-Loc-Token), so walling
            # costs the headless surface nothing while making the role/scope gates real.
            # NO dev-account seeding — bootstrap the first admin headlessly with
            # `python -m app.cli init --from-env --issue-key` (seeds the admin AND enables
            # the wall). NO in-process daemons: an external cron drives POST
            # /api/admin/tick + POST /api/admin/retention/purge.
            security.enable_login_wall()
            # Trust-host-auth (Train 0b): validate + provision the trust principal IFF
            # the embedded_trust_host_auth switch is also on (the double condition). This
            # runs AFTER enable_login_wall so multi_org_active() reads correctly, and
            # RAISES loudly here (failing the boot) if the switch is on but the principal
            # env is missing / the multi-org binding is invalid — never a silent
            # degrade at request time. A strict no-op when the switch is off.
            from app.services import trust_auth

            trust_auth.configure_at_boot()
        elif config.IS_SERVER:
            # Container / declared server install: turn the login wall on but seed
            # NO dev accounts — first visit hits /auth/setup to create the admin
            # (the security middleware redirects there while user_count() == 0).
            security.enable_login_wall()
            # #414: print the one-time setup token /auth/setup asks for (no account yet).
            security.prepare_setup_token_at_boot()
        elif not config.loopback_bind():
            # Plain `run.py` with HOST bound to a LAN/public address is a server
            # posture even without LOC_TMS_SERVER (red-team audit 2026-10-03
            # finding 03): seeding admin/admin + linguist + user1 there would put
            # well-known credentials on the network until first sign-in. Behave
            # exactly like the IS_SERVER branch above: wall ON, seed nothing,
            # first visit creates the admin at /auth/setup.
            security.enable_login_wall()
            _logger.warning(
                "Dev-account seeding skipped: HOST is %r, not a loopback address, "
                "so this boot takes server posture (login wall ON, no dev accounts "
                "seeded). Create the first admin at /auth/setup.",
                config.HOST,
            )
            security.prepare_setup_token_at_boot()
        else:
            # Local dev: seed the default admin/linguist/user1 accounts (which also
            # turns the wall on) so `run.py` is usable out of the box.
            security.seed_initial()
            security.ensure_linguist()  # backfill the linguist persona on older installs
            languages.seed_defaults()  # starter Language Sets on a fresh install
        # Background daemons, routed through the TimeDriver port (composable-core phase 1,
        # Train 2). ports.scheduler.get() resolves the default driver chosen from
        # config.IS_EMBEDDED at import: NON-embedded -> InProcessTimeDriver.start() starts
        # BOTH daemons (byte-identical to the old inline scheduler.start(); retention.start());
        # embedded -> ExternalTickDriver.start() is a no-op (an external cron drives the
        # POST /api/admin/tick + POST /api/admin/retention/purge pair). tests stay
        # deterministic and drive scheduler.tick()/retention.purge() directly. The driver
        # itself carries the embedded gate, so the startup call is unconditional.
        from app.ports import scheduler as scheduler_port

        scheduler_port.get().start()


@app.on_event("shutdown")
def _shutdown() -> None:
    # File-encryption at rest only applies when the DB is a local SQLite file.
    # On Postgres there is no local file to lock; encryption is the server's job.
    if dbcrypt.enabled():
        # Always-on SQLCipher mode: the file is already encrypted on disk at all
        # times, so the shutdown vault is redundant. No-op with a log line.
        if settings.get("encrypt_db") == "1":
            _logger.info(
                "SQLCipher always-on DB encryption active; shutdown vault "
                "skipped (DB is already encrypted at rest)."
            )
        return
    if config.IS_SQLITE and settings.get("encrypt_db") == "1":
        vault.lock_and_remove(config.DB_PATH)


# Embedded/headless profile (Train 0a): mount ONLY the container health surface + the
# token-authed /api routers (no Jinja/web surface). A default boot mounts ALL_ROUTERS
# — byte-identical to before. The health routers are in both sets, so /health + /ready
# stay live either way (the embedded container's liveness answer).
_ROUTERS = (HEALTH_ROUTERS + API_ROUTERS) if config.IS_EMBEDDED else ALL_ROUTERS
for r in _ROUTERS:
    app.include_router(r)


# The BACK-OFFICE operator console was mounted here (multi-org P5) and was REMOVED on
# 2026-09-20 by the maintainer's decision, together with the super-admin account it authenticated.
# Organizations still run side by side; changing one now means signing in to that one.
#
# The capability did not move to another web surface, it left the web entirely: org
# lifecycle (create / rename / suspend / unsuspend / move a user) lives at `cli org` and
# `cli user org`, gated by require_operator, which keys on LOC_TMS_OPERATOR_KEY rather than
# on any account flag. Operator power now requires shell access on the host.
#
# What went with it: the audited, time-boxed impersonation used for cross-tenant
# remediation. There is deliberately no cross-tenant door left to leak.


# Observability (Train Q2): arm the Prometheus metrics ENGINE + OpenTelemetry tracing IFF
# their switches are on, then mount the auth-gated /metrics ROUTE. Called at IMPORT time
# (NOT the startup event) on purpose: the HTTP metrics middleware and the OTel FastAPI
# instrumentation both ADD MIDDLEWARE, which Starlette forbids once the app has started (the
# stack is built on the first request/lifespan, i.e. after startup). A STRICT NO-OP when both
# switches are off (flag-check-first, the s3_blobstore fail-loud shape): a default install
# imports neither opt-in library and adds no route/middleware, so the in-process suites are
# untouched. RAISES loudly (fails the boot) when a switch is on but its opt-in library /
# required config is missing.
#
# LAYERING (reverse_core_no_shell): the ENGINE is the core service app.services.observability
# (no web/framework import); the ROUTE + its admin auth live in the WEB module
# app.web.metrics (which may import Starlette + app.web.deps + app.kernel.security). main.py
# is the app/shell composition root -- it may import both -- so it WIRES them here by handing
# the engine module to install_metrics_route. Services never call into web, web never imports
# app.services. The "/metrics" _ALLOW_EXACT wall-bypass above stays as is.
from app import extensions as _extensions  # noqa: E402


def _arm_observability(app) -> None:
    """Optional Enterprise add-on (OpenTelemetry + Prometheus): armed only when its code is
    present; a strict no-op otherwise (no registry, no middleware, no /metrics route)."""
    if not _extensions.use("observability").installed:
        return
    from app.web import metrics as _obs_metrics_web

    observability = _extensions.observability.module()
    observability.configure_at_boot(app)  # build the registry + middleware + OTel (flag-gated)
    _obs_metrics_web.install_metrics_route(app, observability)  # mount /metrics IFF armed


_arm_observability(app)
