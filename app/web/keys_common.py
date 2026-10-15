"""keys: shared leaf helpers (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

import logging
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app import config
from app.kernel import (
    keys as keys_svc,
)
from app.kernel import (
    security,
)
from app.models import SegStatus
from app.web import deps
from app.web.deps import (
    _actor_email,
    _error_page,
    _my_customer_id,
    _my_org_id,
)

_logger = logging.getLogger("loc_tms")


api_router = APIRouter()


KEY_STATUSES = (
    SegStatus.NEW,
    SegStatus.MT,
    SegStatus.EDITED,
    SegStatus.NEEDS_REVIEW,
    SegStatus.APPROVED,
)


_KEY_PAGE_MAX = 1000


_NOT_FOUND = {"ok": False, "error": "not found"}


_FORBIDDEN = {"ok": False, "error": "your role does not allow this action."}


# The token-authed /api surface for key projects (Delta API + key CRUD) lives on a
# SEPARATE router so the embedded/headless profile can mount it while dropping the
# Jinja key-grid pages above. Both are registered in a default boot (app.web.__init__:
# keys.router -> WEB_ROUTERS, keys.api_router -> API_ROUTERS), so the served route set
# is unchanged. Every @api_router route below is /api-prefixed.
#: The segment statuses the grid/API/CLI offer as a `status` filter.
#: The ceiling `keys.page_keys` clamps a key-list page to; the /api list refuses
#: anything outside [1, this] rather than clamping silently (review B18).
# ============================================================================
# shared gates
# ============================================================================
def _flag_off_block(request: Request):
    """Layer-2 WEB kill switch (the middleware is layer 1): 404 when the 'keys' feature
    is off. Delegated to the ONE shared deps.feature_block, whose features.is_enabled
    uses the SAME strict `str(get(...)) == "1"` predicate as the old inline
    `keys_svc.enabled()` -- behaviour-identical for every value, non-canonical values
    failing toward OFF at both layers. Every call site below is UNMOVED (defense in
    depth); the create-form type selector + grid affordances are template-gated via the
    single `feature_enabled('keys')` Jinja global."""
    return deps.feature_block(request, "keys")


def _api_flag_block():
    # Layer-2 /api kill switch, delegated to the shared deps.feature_api_block; the JSON
    # 404 body ({"ok": False, "error": "not found"}) is byte-identical to `_NOT_FOUND`,
    # and its strict is_enabled matches the old inline keys_svc.enabled() for every value.
    return deps.feature_api_block("keys")


def _image_flag_off_block(request: Request):
    """Layer-2 WEB kill switch for the Batch-14 image-context feature (SEPARATE from the
    `keys` slug -- `_flag_off_block` is hardcoded to `keys`, M-7). A disabled feature 404s
    at layer 1 via the `/projects/*/keys/*/images` ownership pattern AND here at layer 2."""
    return deps.feature_block(request, "key_images")


def _image_api_flag_block():
    """Layer-2 /api kill switch for the image-context feature (SEPARATE from `keys`)."""
    return deps.feature_api_block("key_images")


def _write_gate(request):
    """can_write parity with every other content-mutating web route."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    return None


def _can_write(request) -> bool:
    return (not security.auth_required()) or security.can_write(security.session_user(request))


def _scope(request) -> dict:
    """The org/customer guards + actor every service call from a WEB route gets."""
    return {"org_id": _my_org_id(request), "customer_id": _my_customer_id(request)}


def _actor(request) -> str:
    return _actor_email(request) or "web"


_EXPORT_NOTICE_COOKIE = "loc_keys_export_notice"


_EXPORT_NOTICE_CAP = 900


_NOTICE_OVERFLOW_ROOM = 80


#: The one-shot grid notice (live-QA K2). A cookie because the download response IS
#: the file: there is no page to render a warning on, and the operator must not have to
#: read HTTP headers to learn that the file they just took contains English.
#:
#: IT CARRIES IMPORT NOTES TOO (fix-round F4). The import route flashed COUNTS only, so
#: the `notes[]` naming every key whose write the K1(c) floor skipped -- the whole
#: point of naming them -- reached the CLI, `--json` and /api and died on the one
#: surface the download -> hand-off -> re-upload story actually happens on. The label
#: rides INSIDE the payload so one cookie, one renderer, serves both.
#: Bounded, and PERCENT-ENCODED on the way in: a notice names key names, which are user
#: data, and a raw comma or semicolon in a Set-Cookie value is a parsing hazard.
#: Truncation happens BEFORE the encoding, so a cut can never land mid-escape.
#: Characters reserved so the "(+N more note(s))" marker itself always fits.
def _notice_path(pid: int) -> str:
    return "/projects/%d/keys" % pid  # noqa: UP031


def _set_export_notice(resp: Response, pid: int, notices, label="Last export") -> None:
    """Park a notice for the grid to render ONCE.

    WHOLE notices, never a hard slice (final round, blocker 2). The cap used to cut
    the joined body mid-word, so the notice ended in the middle of a sentence and the
    reader had no way to tell how much was missing. A notice that does not fit is
    DROPPED and COUNTED instead."""
    if not notices:
        return
    kept: list[str] = []
    used = len(label) + 2
    for note in notices:
        if used + len(note) + 1 > _EXPORT_NOTICE_CAP - _NOTICE_OVERFLOW_ROOM:
            break
        kept.append(note)
        used += len(note) + 1
    dropped = len(notices) - len(kept)
    if dropped > 0:
        kept.append(
            "(+%d more note(s) -- the command line and the --json report carry them all.)" % dropped  # noqa: UP031
        )
    if not kept:
        return
    body = ("{}: {}".format(label, " ".join(kept)))[:_EXPORT_NOTICE_CAP]
    resp.set_cookie(
        _EXPORT_NOTICE_COOKIE,
        "%d|%s" % (pid, quote(body)),  # noqa: UP031
        max_age=600,
        path=_notice_path(pid),
        httponly=True,
        samesite="lax",
        secure=config.SECURE_COOKIES,  # G-02: match the session-cookie Secure posture
    )


def _take_export_notice(request: Request, pid: int) -> str:
    """The parked notice for THIS project, or "". The caller clears the cookie."""
    raw = request.cookies.get(_EXPORT_NOTICE_COOKIE) or ""
    owner, _, body = raw.partition("|")
    if not body or owner != str(pid):
        return ""
    return unquote(body)[:_EXPORT_NOTICE_CAP]


_RUNS_COOKIE = "loc_keys_runs"


_RUNS_COOKIE_MAX = 6


_MAX_RUN_ID = 2**31 - 1


#: The launched-run ids the grid's live progress strip tracks (GitHub #47). Same
#: one-shot-cookie mechanism as the export notice above, and for the same reason: the
#: translate POST answers with a 303 to the grid, so the ids have to survive exactly
#: ONE redirect and then be gone.
#:
#: WHY NOT A QUERY PARAM. The strip auto-reloads the grid once every tracked run is
#: terminal, so ids that persist in the URL would be re-read by that reload and the
#: page would reload forever. A cookie the grid CONSUMES makes "exactly once"
#: structural rather than a flag the widget has to remember.
#: At most this many ids ride along. SIZED BY THE BROWSER, NOT THE COOKIE (review
#: F12): each strip row holds an EventSource open, HTTP/1.1 caps a host at ~6
#: concurrent connections, and every open stream also parks one thread in the ASGI
#: sync pool for up to observe_run's 15-minute window. Six rows means a fan-out over
#: a wide project shows the first six and the rest are simply not tracked -- the runs
#: are unaffected, and the widget additionally serialises to 5 live streams so the
#: page keeps one connection free for its own navigation.
#: Sanity bound on a cookie-supplied id (review F9). The value is client-controlled,
#: and an id with 25 digits sailed through int() and into session.get(), where SQLite
#: raised OverflowError -> HTTP 500 -> a grid that stayed bricked for the cookie's
#: whole lifetime, because the handler died before it could clear it.
def _set_run_cookie(resp: Response, pid: int, run_ids) -> None:
    """Park the launched run ids for the grid's progress strip to pick up ONCE."""
    ids = [int(r) for r in run_ids][:_RUNS_COOKIE_MAX]
    if not ids:
        return
    resp.set_cookie(
        _RUNS_COOKIE,
        "%d|%s" % (pid, ",".join(str(i) for i in ids)),  # noqa: UP031
        max_age=1800,
        path=_notice_path(pid),
        httponly=True,
        samesite="lax",
        secure=config.SECURE_COOKIES,  # G-02: match the session-cookie Secure posture
    )


def _take_run_cookie(request: Request, pid: int) -> list:
    """The parked run ids for THIS project, or []. The caller clears the cookie.

    EVERYTHING HERE IS UNTRUSTED, and the failure mode is worse than a wrong render:
    this cookie is set with a 30-minute lifetime on the grid's own path, so anything
    that can make this function (or its caller) raise bricks the page for half an
    hour -- the handler dies before it can clear the cookie, and the browser sends it
    again on every retry (review F9). So: the owner id must match this project, every
    token must parse as an int, and every id must be a plausible primary key
    (0 < id <= _MAX_RUN_ID) rather than a 25-digit number that only blows up later,
    inside SQLite. A malformed token is skipped, never fatal; the caller clears the
    cookie whenever one was present at all, however little of it survived."""
    raw = request.cookies.get(_RUNS_COOKIE) or ""
    owner, _, body = raw.partition("|")
    if not body or owner != str(pid):
        return []
    out = []
    for tok in body.split(",")[:_RUNS_COOKIE_MAX]:
        try:
            rid = int(tok)
        except (TypeError, ValueError):
            continue
        if 0 < rid <= _MAX_RUN_ID:
            out.append(rid)
    return out


def _live_runs(session, project, run_ids: list) -> list:
    """[{id, locale, status}] for the grid's progress strip -- ONLY for runs that
    really belong to THIS project's internal documents.

    The ids arrive in a client-settable cookie, so this is the check that keeps a
    forged id from putting a foreign run's locale on the page. (/runs/stream enforces
    its own project-visibility ring independently, so a forged id could never have
    STREAMED anything either; this closes the render half of the same door.)"""
    if not run_ids:
        return []
    from app.models import WorkflowRun

    doc_ids = {d.id for d in keys_svc.translatable_documents(session, project)}
    rows = []
    for rid in run_ids:
        run = session.get(WorkflowRun, rid)
        if run is None or run.document_id not in doc_ids or run.status == "queued":
            continue
        rows.append({"id": run.id, "locale": run.target_locale or "", "status": run.status})
    return rows


def _queued_run_pills(session, project):
    """At most six static waiting pills plus one overflow sentinel."""
    from sqlmodel import select

    from app.models import Document, RunStatus, WorkflowRun

    return session.exec(
        select(WorkflowRun)
        .join(Document, Document.id == WorkflowRun.document_id)
        .where(Document.project_id == project.id, WorkflowRun.status == RunStatus.QUEUED)
        .order_by(WorkflowRun.id)
        .limit(7)
    ).all()


def _back(pid: int, request: Request, **extra) -> RedirectResponse:
    """Back to the grid, preserving the caller's filters + carrying a flash."""
    keep = [
        (k, v)
        for k, v in request.query_params.items()
        if k
        in ("namespace", "tag", "q", "status", "untranslated_in", "archived", "deleted", "cursor")
    ]
    parts = [f"{k}={quote(str(v))}" for k, v in keep]
    parts += [f"{k}={quote(str(v))}" for k, v in extra.items() if v not in (None, "")]
    qs = ("?" + "&".join(parts)) if parts else ""
    return RedirectResponse("/projects/%d/keys%s" % (pid, qs), status_code=303)  # noqa: UP031


# ============================================================================
# /api gates — the "keys" scope, copied from deps._require_doc_scope
# ============================================================================
def _require_keys_scope(request: Request):
    """Gate for the key endpoints. Returns (ident, blocked).

      - no identity                       -> 401
      - not a per-user key (kind != user) -> 403  (the shared connector token is
                                                   rejected outright)
      - "keys" not in the key's scopes    -> 403  ("api" and "docs" do NOT grant it)

    A NEW scope rather than a reuse of "docs" on purpose: a CI sync consumer holding
    a key-scoped token has no business uploading and downloading documents, and a
    document-integration token has no business reading the whole string catalogue.
    """
    from app.kernel import apikeys

    ident = deps.api_identity(request)
    if ident is None:
        return None, JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if ident.get("kind") != "user" or "keys" not in apikeys.scope_set(ident.get("scopes", "")):
        return ident, JSONResponse(
            {"ok": False, "error": "This endpoint requires a keys-scoped user API key."},
            status_code=403,
        )
    return ident, None


def _api_gate(request: Request):
    """(ident, blocked) — scope + the shared-token org binding, like `_gate`."""
    ident, blocked = _require_keys_scope(request)
    if blocked:
        return None, blocked
    blocked = deps._api_org_block(request, ident)
    if blocked:
        return None, blocked
    ident["_cust_id"] = deps._ident_customer_id(ident)
    return ident, None


def _api_write_block(ident):
    """can_write parity: >= translator. Blocks a viewer's token. No-op with the login
    wall off, matching every other /api write gate."""
    if not security.auth_required():
        return None
    if security.can_write((ident or {}).get("user")):
        return None
    return JSONResponse(_FORBIDDEN, status_code=403)


def _api_scope(ident) -> dict:
    """The org/customer guards a service call from an /api route gets. THE SAME
    values `_api_project` filters on, so a seam refuses even if a future route here
    forgets to resolve the project through the helper."""
    return {"org_id": deps._ident_org_id(ident), "customer_id": (ident or {}).get("_cust_id")}


def _api_error(exc, status=400):
    body = {"ok": False, "error": str(exc)}
    # L4: surface a stable machine-readable `code` when the exception carries one (e.g. the
    # key-image export-cap refusal), so a client can branch on the code rather than sniff the
    # human message. Only a string code is emitted; absent/None leaves the shape unchanged.
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        body["code"] = code
    return JSONResponse(body, status_code=status)


def _limit_refusal(limit, ceiling, allow_zero: bool = False):
    """A 422 for a `limit` outside [1, ceiling], or None (review B18).

    422 rather than 400 to match the shape FastAPI itself uses for a parameter that is
    syntactically fine and semantically impossible. `allow_zero` is for `/changes`,
    where 0 has always meant "use the server default"; `ceiling=None` means "clamp the
    top end elsewhere, refuse only nonsense" (also `/changes` -- see its call site)."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "limit must be a whole number."}, status_code=422
        )
    if allow_zero and value == 0:
        return None
    if value < 1:
        return JSONResponse(
            {"ok": False, "error": "limit must be a positive whole number (got %d)." % value},  # noqa: UP031
            status_code=422,
        )
    if ceiling is not None and value > ceiling:
        return JSONResponse(
            {"ok": False, "error": "limit must be between 1 and %d (got %d)." % (ceiling, value)},  # noqa: UP031
            status_code=422,
        )
    return None


# ============================================================================
# WEB — the key grid
# ============================================================================
def _grid_filters(qp) -> dict:
    """The filter set the grid, `/api` list and `keys list` all share."""
    status = (qp.get("status") or "").strip()
    return {
        "namespace": qp.get("namespace") if qp.get("namespace") is not None else None,
        "tag": (qp.get("tag") or "").strip() or None,
        "q": (qp.get("q") or "").strip() or None,
        "status": status if status in KEY_STATUSES else None,
        "untranslated_in": (qp.get("untranslated_in") or "").strip() or None,
        "include_archived": str(qp.get("archived") or "") in ("1", "on", "true"),
        # Deleted keys are OFF by default (they are gone as far as the product is
        # concerned) but reachable, because the delete confirmation promises the key
        # is restorable and the restore route would otherwise be an affordance with
        # no way to reach it.
        "include_deleted": str(qp.get("deleted") or "") in ("1", "on", "true"),
    }


# ============================================================================
# WEB — per-string in-context images (Batch 14 T1; the _grid_key triple, LAW-IMG-2)
# ============================================================================
def _img_customer_id(project) -> int | None:
    return getattr(project, "customer_id", None)


def _api_workflow_visible(session, ident, wid):
    """The Workflow `wid` is visible to this caller (org, customer ring AND project
    ring), or None (404). The workflow_id arrives in the BODY of the keys-translate
    launch, so it must resolve exactly like /api/workflows/{id}: a hidden project
    workflow is the same 404 as a missing id (#372). Delegates to the authoritative
    api_cli_common._wf_in_org so the resolvers cannot drift."""
    from app.web.api_cli_common import _wf_in_org

    if wid is None:
        return None
    return _wf_in_org(session, wid, deps._ident_org_id(ident), ident)


async def _json_body(request: Request):
    """The request's JSON body, or None when it is not JSON. Never raises — a
    malformed body is a 400 the caller can read, not a 500."""
    try:
        return await request.json()
    except Exception:
        return None
