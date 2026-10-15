"""shared /api helpers (leaf)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import json
from functools import wraps

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app import db as _db
from app.db import engine
from app.kernel import (
    access,
    batch_runs,
    glossary,
    orchestrator,
    pair_providers,
    security,
)
from app.kernel import ids as ids
from app.kernel import keys as keys_svc
from app.models import (
    Customer,
    Document,
    Project,
    RunStatus,
    Segment,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
)
from app.web import deps
from app.web.route_path import route_path
from app.web.streaming import retry_after, stream_slot

_NOT_FOUND = {"ok": False, "error": "not found"}


_FORBIDDEN = {"ok": False, "error": "your role does not allow this action."}


_REVIEW_FORBIDDEN = {
    "ok": False,
    "error": "clearing a TM lock requires the reviewer role (or higher).",
}


_LQA_FINDING_FORBIDDEN = {
    "log": {"ok": False, "error": "logging an LQA finding requires the reviewer role (or higher)."},
    "edit": {
        "ok": False,
        "error": "editing an LQA finding requires the reviewer role (or higher).",
    },
    "withdraw": {
        "ok": False,
        "error": "withdrawing an LQA finding requires the reviewer role (or higher).",
    },
}


# Reviewer+-specific refusal (TM unlock): NAMES the required role, and is BYTE-IDENTICAL
# to the web route's refusal (documents.segment_unlock) so the two surfaces refuse the
# same way.
# LQA reviewer-finding refusals (#70): the /api finding endpoints are ALSO reviewer+,
# but their refusal must NAME the finding action, not the TM-unlock action — BYTE-
# IDENTICAL to the web twins (documents.lqa_finding_log/edit/withdraw) so the two
# surfaces refuse the same way. (_REVIEW_FORBIDDEN above is the TM-unlock wording and
# is WRONG for a finding write; using it here was the parity break #70 flags.)
# --- write-role RBAC (parity with the web routes) ----------------------------
# The /api surface is org- and customer-bound (see _gate), but historically
# enforced NO role: any valid docs-scoped lsu_ key could call every write
# endpoint regardless of the owner's role. These helpers mirror, per endpoint,
# the SAME role predicate the equivalent web route uses (the parity principle),
# so a viewer's token can no longer write, a translator can do can_write actions
# but not manage-level ones, etc. A strict no-op when auth is off (single-tenant
# dev with no login wall -> every predicate short-circuits to allowed), so the
# gate-off / admin-token behavior is byte-identical to before.
def _role_block(ident, predicate, body=_FORBIDDEN):
    """Return a ready-to-return 403 JSONResponse when the gated caller's User
    fails `predicate` (security.can_write / can_manage / can_tune_ai / can_review),
    else None. `body` lets a stricter gate name its required role (e.g. _review_block);
    it defaults to the generic role refusal. No-op when the login wall is off (mirrors
    the web routes' `auth_required()` guard) so single-tenant behavior is unchanged."""
    if not security.auth_required():
        return None
    if predicate((ident or {}).get("user")):
        return None
    return JSONResponse(body, status_code=403)


def _write_block(ident):
    """can_write parity: >= translator. Blocks viewer."""
    return _role_block(ident, security.can_write)


def _lqa_finding_review_block(ident, action: str):
    """can_review parity for an LQA reviewer-finding write (#70). Same predicate as
    _review_block, but the refusal body NAMES the finding action (log/edit/withdraw),
    byte-identical to the web twin — never the TM-unlock wording. Callers run this
    AFTER the document-visibility ring (visibility-before-role, matching the web
    route's RING-BEFORE-BODY docstring) so a foreign/invisible document 404s the same
    as its GET twin instead of leaking a role 403."""
    return _role_block(ident, security.can_review, body=_LQA_FINDING_FORBIDDEN[action])


def _manage_block(ident):
    """can_manage parity: >= manager. Blocks viewer/translator/linguist."""
    return _role_block(ident, security.can_manage)


def _ai_block(ident):
    """can_tune_ai parity: admin or linguist (the Language Set / AI-setup gate)."""
    return _role_block(ident, security.can_tune_ai)


def _customer_config_block(ident):
    """can_configure_customer parity: admin, manager or linguist (customer settings,
    defaults, context cards, language pairs). Blocks translator/reviewer/viewer."""
    return _role_block(ident, security.can_configure_customer)


def _admin_block(ident):
    """is_admin parity (SPEC-rename-delete): the strict admin gate EVERY new delete
    uses, mirroring the web deps._admin_block. Blocks everyone below admin; a
    customer-bound key is never admin, so it can never delete. No-op with the login
    wall off (single-tenant)."""
    return _role_block(ident, security.is_admin)


def _remote_admin_block(ident):
    """404 when the install has not opted into remote administration
    (`remote_admin_enabled` off, the default). Gates the IRREVERSIBLE remote admin
    verbs (SPEC-full-remote-parity Wave R2 — GDPR erase, retention purge-now, apikey
    revoke-all): a leaked admin-scoped key can never reach them on an install that
    never turned remote admin on. 404 (not 403) keeps the capability NON-ENUMERABLE
    (the kill-switch law), and the switch itself is settable ONLY locally
    (settings.REMOTE_IMMUTABLE_KEYS) — no /api route may ever flip it. NOT gated on
    the login wall: the switch is the perimeter whether or not auth is on."""
    from app.kernel import settings as _settings

    if _settings.get("remote_admin_enabled") == "1":
        return None
    return JSONResponse(_NOT_FOUND, status_code=404)


# --- shared gates ------------------------------------------------------------
def _gate(request: Request):
    """Resolve the caller (docs-scoped per-user key) + their org. Returns
    (ident, org_id, blocked): blocked is a ready-to-return JSONResponse when the
    caller is not allowed, else None. org_id is None with the gate off (no scoping).

    R2-3: the caller's CUSTOMER binding (if any) is stashed on the ident dict as
    ident["_cust_id"] so the shared _org_list / _*_in_org guards can confine a
    customer-bound user's lsu_ key to its own customer's rows WITHOUT changing every
    route's (ident, org_id, blocked) unpacking. None for internal-staff keys, so the
    remote CLI is byte-identical for them."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return None, None, blocked
    ident["_cust_id"] = deps._ident_customer_id(ident)
    org_id = deps._ident_org_id(ident)
    blocked = _path_project_block(request, ident, org_id)
    return ident, org_id, blocked


def _path_project_block(request, ident, org_id):
    """Resolve project-owned path IDs before role or input validation.

    The normal resource guards remain authoritative for body/query references. This
    preflight keeps inaccessible and missing path IDs on the same 404 path even when
    a handler has a stricter write role or requires an additional body field.
    """
    # Keep established validation ordering for see-all roles and the local operator.
    # Their resource guards below still enforce org/customer rings.
    if access.can_see_all((ident or {}).get("user")):
        return None
    params = request.path_params
    path = route_path(request.scope)
    refs = (
        ("/api/projects/", "pid", Project, None),
        ("/api/documents/", "doc_id", Document, "project_id"),
        ("/api/segments/", "sid", Segment, "document_id"),
        ("/api/runs/", "run_id", WorkflowRun, "document_id"),
        ("/api/batches/", "batch_id", WorkflowBatch, "document_id"),
    )
    for prefix, name, model, field in refs:
        if not path.startswith(prefix) or name not in params:
            continue
        with Session(_db.relief_engine_or(engine)) as session:
            rid = _int_id(params[name])
            row = session.get(model, rid) if rid is not None else None
            if row is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            if field == "document_id":
                row = session.get(Document, row.document_id)
                if row is None:
                    return JSONResponse(_NOT_FOUND, status_code=404)
            pid = row.id if model is Project else row.project_id
            if _project_in_org(session, pid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
    return None


def _gate_admin(request: Request):
    """Like _gate, but requires the `admin` scope grant instead of `docs`
    (SPEC-full-remote-parity Wave R2 — the identity/admin surface). Resolves the
    caller + their org + customer binding, returning (ident, org_id, blocked); blocked
    is a ready-to-return 403 when the key lacks the admin scope. Callers layer the
    is_admin ROLE gate (_admin_block) and — for the destructive verbs — the
    remote_admin switch (_remote_admin_block) on top: scope, role and switch are three
    independent locks, and an admin-scoped route wants all three that apply to it."""
    ident, blocked = deps._require_admin_scope(request)
    if blocked:
        return None, None, blocked
    ident["_cust_id"] = deps._ident_customer_id(ident)
    return ident, deps._ident_org_id(ident), None


def _int_id(raw) -> int | None:
    """A positive, DB-safe row id or None -- the single app-wide id law
    (app/services/ids.coerce_id). A malformed id (a Unicode digit int() rejects,
    or a value SQLite rejects at the bind layer) falls through to the same
    not-found / invalid path any nonexistent id takes, never a 500."""
    return ids.coerce_id(raw)


LIST_HARD_CAP = 1000


PROMPT_TAP_LIST_DEFAULT = 50


PROMPT_TAP_LIST_CAP = 500


#: AU-7: the generous hard cap every list route enforces even when the caller passes
#: no `limit`. Silent truncation is banned by project doctrine, so a bounded response
#: ALWAYS carries a `truncated` flag; a caller that wants more pages `offset` past it.
#: run-190 Item C / M4 (gate round 1): the LLM-Ledger actions list route is its own,
#: TIGHTER window — this is a diagnostic read whose per-action summary itself expands into
#: per-run/per-call rows, so the cap is 500 (not the house 1000) with a default of 50.
def _list_window(raw_l, raw_o, cap: int = LIST_HARD_CAP):
    """(limit, offset, err) for a bounded list route (AU-7), given the RAW `limit`/
    `offset` query values (strings, or None when absent). `err` is a ready 400
    JSONResponse for a malformed value, else None.

    A caller that passes NO `limit` gets `cap` rows (so an unpaginated request is
    bounded rather than unbounded), with `truncated: true` if more exist -- never a
    silent cutoff. An explicit `limit` is clamped to [1, cap]; `offset` defaults 0.
    Fetch `limit + 1` rows and slice with `_list_truncate` to fill the flag honestly."""
    try:
        limit = int(raw_l) if raw_l not in (None, "") else cap
    except (TypeError, ValueError):
        return (
            None,
            None,
            JSONResponse({"ok": False, "error": "limit must be an integer."}, status_code=400),
        )
    try:
        offset = int(raw_o) if raw_o not in (None, "") else 0
    except (TypeError, ValueError):
        return (
            None,
            None,
            JSONResponse({"ok": False, "error": "offset must be an integer."}, status_code=400),
        )
    if limit < 1:
        return (
            None,
            None,
            JSONResponse(
                {"ok": False, "error": "limit must be a positive integer."}, status_code=400
            ),
        )
    if offset < 0:
        return (
            None,
            None,
            JSONResponse({"ok": False, "error": "offset must be >= 0."}, status_code=400),
        )
    return min(limit, cap), offset, None


def _glossary_window(request):
    """Validate glossary paging without changing other bounded-list contracts."""
    limit, offset, err = _list_window(
        request.query_params.get("limit"), request.query_params.get("offset")
    )
    if err:
        return limit, offset, err
    try:
        glossary.parse_cursor(request.query_params.get("cursor"))
        raw_limit = request.query_params.get("limit")
        if raw_limit not in (None, "") and int(raw_limit) > LIST_HARD_CAP:
            raise ValueError(f"limit must be <= {LIST_HARD_CAP}.")
    except ValueError as exc:
        return None, None, JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return limit, offset, None


def _list_truncate(rows: list, limit: int) -> tuple[list, bool]:
    """Given `limit + 1` fetched (or a full in-memory list already sliced from
    `offset`), return (page, truncated). `page` is at most `limit` long; `truncated`
    is True when at least one more row existed past the page."""
    truncated = len(rows) > limit
    return rows[:limit], truncated


def _cust_of(ident):
    """The customer id a gated identity is confined to (R2-3), or None."""
    return (ident or {}).get("_cust_id")


def _project_in_org(s: Session, pid: int, org_id, ident=None):
    """Fetch a project in the caller's org/customer ring and current web visibility.

    Membership, creator and role rules come only from access.can_view_project. A
    missing identity fails closed with auth on. Missing and inaccessible IDs both
    resolve to None, so every remote CLI and MCP adapter uses the same 404.
    """
    project = s.get(Project, pid)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    cust = _cust_of(ident)
    if cust is not None and getattr(project, "customer_id", None) != cust:
        return None
    if not access.can_view_project(s, (ident or {}).get("user"), project):
        return None
    return project


def _visible_document(s: Session, doc_id):
    """A Document as the /api DOCUMENT routes may see it, or None.

    SPEC-key-based-projects 2.5 (Wave B fix round, A-F3): an internal key document
    (`fmt="keys"` — the per-namespace value stores AND the trash document holding
    soft-deleted keys' values) resolves as MISSING, so each caller answers its own
    existing 404 and a docs-scoped token cannot reach key internals through the
    document routes. The `keys` scope exists precisely so those are separate grants.

    The mirror of `deps._visible_doc` for this router, and the same idiom: the cheap
    `fmt` compare first, the keys service imported only inside that branch."""
    doc = s.get(Document, doc_id) if doc_id is not None else None
    if doc is not None and doc.fmt == keys_svc.KEY_DOC_FMT:
        return None
    return doc


def _customer_in_org(s: Session, cid: int, org_id, ident=None):
    cust_row = s.get(Customer, cid)
    if cust_row is None:
        return None
    if org_id is not None and getattr(cust_row, "org_id", None) != org_id:
        return None
    # R2-3: a customer-bound caller may only ever reach its OWN customer.
    bound = _cust_of(ident)
    if bound is not None and cid != bound:
        return None
    return cust_row


def _shared_write_block(ident, row_customer_id):
    """#468: the WRITE-side customer ring for /api. Call it AFTER the read seam
    (`_wf_in_org`, `_grp_in_org`, `_pt_in_org`, `_style_guide_in_scope`,
    `_langset_in_org`) found the row, on every route that changes it. A customer-bound
    caller may change only its own customer's rows; a global row (customer_id NULL) or
    an org-level row (a language set: pass None) is read-only for it (403, the shared
    message). No-op for an unbound caller."""
    if access.customer_write_blocks(_cust_of(ident), row_customer_id):
        return JSONResponse({"ok": False, "error": access.SHARED_ASSET_READ_ONLY}, status_code=403)
    return None


def _langset_in_org(ls, org_id):
    if ls is None:
        return None
    if org_id is not None and getattr(ls, "org_id", None) != org_id:
        return None
    return ls


def _wf_in_org(s: Session, wid: int, org_id, ident=None):
    """Fetch a Workflow, or None if missing / in another org / (R2-3) blocked by the
    customer ring for a customer-bound caller. Org-scoped only when the gate is on;
    customer-scoped only for a bound caller (ident given). GLOBAL-scope workflows
    (customer_id NULL) stay visible to everyone — the ring uses the SHARED
    access.customer_ring_blocks predicate the web _wf_customer_block uses, so /api and
    web cannot drift."""
    wf = s.get(Workflow, wid)
    if wf is None:
        return None
    if org_id is not None and getattr(wf, "org_id", None) != org_id:
        return None
    if access.customer_ring_blocks(_cust_of(ident), getattr(wf, "customer_id", None)):
        return None
    if wf.project_id is not None and _project_in_org(s, wf.project_id, org_id, ident) is None:
        return None
    return wf


def _creator(ident):
    """The acting User for org stamping (content.project_create etc. read .org_id)."""
    return (ident or {}).get("user")


def _org_list(stmt, model, org_id):
    """Append an org filter to a list statement when scoping is active."""
    if org_id is not None:
        stmt = stmt.where(model.org_id == org_id)
    return stmt


def _scoped_customers(ident, org_id):
    """R2-3: the Customer list statement for a gated caller — org-filtered, and (for a
    customer-bound lsu_ key) confined to its OWN customer. No-op for internal staff."""
    stmt = _org_list(select(Customer), Customer, org_id)
    cust = _cust_of(ident)
    if cust is not None:
        stmt = stmt.where(Customer.id == cust)
    return stmt


# ============================================================================
# Content types (CRUD) — SPEC-content-types phase 1. RBAC mirrors the web:
# create/rename/delete are can_tune_ai-gated (ContentType CRUD = linguists own
# language assets). Org + R2-3 customer bound exactly like the glossary module.
# ============================================================================
def _content_type_in_scope(s: Session, ct_id: int, org_id, ident):
    """Fetch a content type in the caller's org + customer scope, else None (404).
    Mirrors _glossary_in_scope — a foreign org's/customer's type is indistinguishable
    from a missing one."""
    from app.kernel import content_types as ctsvc

    return ctsvc.visible(s, ct_id, org_id=org_id, customer_id=_cust_of(ident))


# ============================================================================
# Dedicated glossary MODULE (Phase 5-B): first-class glossaries with an approval
# lifecycle + language-pair association. Org + R2-3 customer scoped; content edits
# are can_tune_ai-gated (glossary tuning parity), approval is can_manage-gated
# (matching the web routes' role split).
# ============================================================================
def _glossary_in_scope(s: Session, gid: int, org_id, ident):
    """Fetch a module Glossary in the caller's org + customer scope, else None (404).
    Mirrors _project_in_org — a foreign org's/customer's glossary is indistinguishable
    from a missing one."""
    from app.kernel import glossary_module as gm

    return gm.visible(s, gid, org_id=org_id, customer_id=_cust_of(ident))


# =============================================================================
# Workflow AUTHORING (SPEC-workflow-authoring-api): create a workflow + add / edit /
# delete / move its steps over /api. This is the /api arm of the `workflow create /
# add-step / edit-step / delete-step / move-step` CLI verbs (now --remote-capable)
# and the backend for the MCP create_workflow / edit_workflow tools.
#
# RBAC: can_tune_ai (_ai_block) -- EXACTLY the predicate the web builder enforces
# (app/web/workflows.py add_step/edit_step/delete_step/move_step all use _ai_block)
# and the predicate add_step itself is gated behind. Workflow DELETE stays is_admin
# (unchanged, above); a STEP delete is an authoring edit, so it is can_tune_ai like
# add/edit, mirroring the web delete_step route. Tenancy: _wf_in_org (foreign org /
# customer ring = 404, never 403). Scope: docs (via _gate).
#
# ONE VALIDATION SOURCE (house parity law -- never reimplement a rule in a route).
# The routes call the SAME seams the CLI and web builder call:
#   * prompts.validate_step_prompt -- the {text}/{prior} rule + its canonical 400
#     message, verbatim, plus the soft language warning surfaced as "warnings";
#   * glossary_module.options_for_step -- the approved+visible pin
#     re-validation (a blank pair: every approved glossary is offerable on a
#     language-agnostic workflow; a non-covering pin is skipped at run time with a
#     note), a hard 400 on an invalid pin, so the REMOTE CLI surfaces the
#     same clear error the in-process arm raises, not the web's silent drop);
#   * workflows.list_prompt_templates -- prompt-template binding visibility;
#   * workflows.add_step / update_step / delete_step / reorder_step -- dead-field
#     refusal, whole-graph re-validation, and the in-flight-run guard, all fail-
#     closed -> a clean 400.
# =============================================================================
def _api_workflow_customer(s: Session, ident, org_id, raw):
    """Resolve AND AUTHORIZE the customer_id a new workflow is scoped to. Returns
    (customer_id, err) where `err` is a ready-to-return JSONResponse.

    THE SAME S1/S2 FUNNEL LAW `_memory_customer` documents (adversarial review B1/H1):
    the body carried customer_id straight into create_workflow with NO org/customer
    check, so a customer-A-bound key could PLANT a workflow into customer B's ring (a
    200 it could then never see -- plant-and-forget) and a cross-org id could be stamped
    onto an org-A row. The fix is to funnel every customer_id through the SAME
    _customer_in_org ring every other customer-scoped route here uses:

      * a customer-BOUND caller (R2-3) may only ever scope a workflow to its OWN
        customer -- an explicit FOREIGN id is a 404 (never enumerable), and absent/blank
        becomes its own customer (#468: a bound caller never creates a global row);
      * an UNBOUND (staff) caller's explicit id resolves through _customer_in_org: a
        customer that does not exist, or lives in another ORG, is a 404;
      * a resolved, in-scope customer that is ARCHIVED is refused (web
        _archived_customer_block parity) -- WITHOUT leaking its name (H1: the old bare
        s.get put a foreign row's name in the 400, a cross-org enumeration oracle)."""
    cid = ids.coerce_id(raw)
    bound = _cust_of(ident)
    if cid is None:
        if bound is None:
            return None, None  # absent / blank / non-digit -> a global workflow (staff)
        # #468: a bound caller can never create a GLOBAL (shared) row; it gets its own
        # customer, through the same checks below.
        cid = bound
    if bound is not None and cid != bound:
        # A bound token can never reach another customer's ring.
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    cust = _customer_in_org(s, cid, org_id, ident)
    if cust is None:  # missing / foreign org / foreign customer -> 404
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    if getattr(cust, "archived_at", None) is not None:
        return None, JSONResponse(
            {
                "ok": False,
                "error": f"customer {cid} is archived - unarchive it first, or pick an active "
                "customer, to create a workflow for it.",
            },
            status_code=400,
        )
    return cid, None


def _grp_in_org(s: Session, gid: int, org_id, ident=None):
    """Fetch a WorkflowGroup, or None if missing / in another org / (R2-3) blocked by
    the customer ring for a bound caller (GLOBAL groups stay visible — same shared
    access.customer_ring_blocks predicate the web _grp_or_404 uses)."""
    from app.models import WorkflowGroup

    grp = s.get(WorkflowGroup, gid)
    if grp is None:
        return None
    if org_id is not None and getattr(grp, "org_id", None) != org_id:
        return None
    if access.customer_ring_blocks(_cust_of(ident), getattr(grp, "customer_id", None)):
        return None
    return grp


_WAIT_POLL_INTERVAL_S = 0.25


_WAIT_POLL_CEILING_S = 3600.0


# The `wait=true` worker's poll cadence. Each tick is a cheap DB read of the run's
# StepRun/WorkflowRun rows (via orchestrator.observe_run), NOT a held-open provider call,
# so a long poll never pins anything but this one threadpool worker. The ceiling bounds how
# long the worker tails a run stuck in RUNNING (a crashed background thread that never
# reached a terminal state) before it returns the run's last-known status honestly. Both
# are module-level so tests can shrink them (a broken terminal condition must fail fast, not
# hang for the whole ceiling — review F4).
# 1h: generous for real MT; the worker is off the event loop.
def _stream_wait_route(handler):
    """Wait admission is bounded. Saturation uses the same non-waiting launch arm."""

    @wraps(handler)
    async def wrapped(*args, **kwargs):
        request = kwargs.get("request") or next(arg for arg in args if isinstance(arg, Request))
        ident, org_id, blocked = _gate(request)
        if blocked:
            return blocked
        blocked = _write_block(ident)
        if blocked:
            return blocked
        request.state.stream_wait_identity = (ident, org_id, None)
        body = await _json(request)
        if not body.get("wait"):
            return await handler(*args, **kwargs)
        async with stream_slot() as admitted:
            request.state.stream_wait_admitted = admitted
            response = await handler(*args, **kwargs)
            if not admitted and isinstance(response, JSONResponse) and response.status_code == 200:
                out = json.loads(response.body)
                if out.get("run_id"):
                    with Session(engine) as session:
                        run = session.get(WorkflowRun, out["run_id"])
                        if run is not None:
                            out["status"] = str(run.status)
                elif out.get("id"):
                    with batch_runs._batch_session() as session:
                        batch = session.get(WorkflowBatch, out["id"])
                        if batch is not None:
                            out.update(
                                batch_runs.batch_as_dict(
                                    session, batch, detail="requested_steps" in out
                                )
                            )
                out.update(waited=False, retry_after=retry_after())
                headers = {
                    key: value for key, value in response.headers.items() if key != "content-length"
                }
                return JSONResponse(
                    _scrub_spend(ident, out),
                    status_code=response.status_code,
                    headers=headers,
                    background=response.background,
                )
            return response

    return wrapped


def _drain_run_to_terminal(run_id: int) -> dict:
    """Poll a launched run to a terminal/paused state and fold the observer's events into
    the finished-run summary shape the `wait=true` response returns:
    {run_id, status, steps, text, paused_step, error}.

    Drives `orchestrator.observe_run` — the SAME read-only observer the SSE run view uses.
    It REPLAYS already-recorded StepRun rows first, so a run that finished before we start
    observing (the common fast-MockProvider case, and the re-attach-to-an-already-terminal
    case) folds down exactly like one still in flight. Status strings mirror what
    `run_workflow` used to return, so the route's response mapping is byte-identical.

    On the ceiling, observe_run closes with the run's real (usually still-RUNNING) status;
    we read the row once more so the summary reports the honest current status rather than a
    None the route would misread as ok:true."""
    summary: dict = {
        "run_id": run_id,
        "status": None,
        "steps": [],
        "text": "",
        "paused_step": None,
        "error": None,
    }
    for _run, ev in orchestrator.observe_run(
        run_id, poll_interval=_WAIT_POLL_INTERVAL_S, max_seconds=_WAIT_POLL_CEILING_S
    ):
        t = ev.get("type")
        if t == "step":
            summary["steps"].append(ev)
            if "matches_offered" in ev.get("tm_match", {}):
                summary["tm_offers"] = ev["tm_match"]
        elif t == "paused":
            summary["status"] = "paused"
            summary["paused_step"] = ev.get("step_index")
        elif t == "done":
            summary["status"] = "completed"
            summary["text"] = ev.get("text", "")
        elif t == "closed":
            st = ev.get("status")
            # COMPLETED/PAUSED are already set by the done/paused events above; only the
            # non-happy terminals (and a ceiling timeout) are resolved from `closed`.
            if st == RunStatus.FAILED:
                summary["status"], summary["error"] = "failed", ev.get("error")
            elif st == RunStatus.CANCELLED:
                summary["status"], summary["error"] = "cancelled", ev.get("error")
    if summary["status"] is None:
        # observe_run hit its ceiling with the run still non-terminal (or the row vanished).
        # Report the honest current status instead of a None the route reads as a success.
        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            summary["status"] = str(run.status) if run is not None else "failed"
    return summary


# AI spend and token fields on run and batch JSON are manager-and-above data (ADR 0042, #540
# on the web pages, #574 here): the same figures the run page hides below manager.
_SPEND_FIELDS = frozenset({"cost_usd", "tokens_in", "tokens_out", "total_cost_usd", "run_cost_usd"})


def _sees_spend(ident) -> bool:
    """True when the caller may read AI spend: auth off (the system admin) or manager+.
    Fails closed when the login wall is on and no user rides the identity."""
    return (not security.auth_required()) or security.can_manage((ident or {}).get("user"))


def _scrub_spend(ident, payload):
    """`payload` with every spend/token field removed, recursively, unless the caller may
    read spend. Returns the same object untouched for a manager or admin."""
    if _sees_spend(ident):
        return payload
    if isinstance(payload, dict):
        return {k: _scrub_spend(None, v) for k, v in payload.items() if k not in _SPEND_FIELDS}
    if isinstance(payload, list):
        return [_scrub_spend(None, v) for v in payload]
    return payload


def _batch_wait_response(batch_id, *, detail=False, retried=None, ident=None):
    with batch_runs._batch_session() as s:
        batch = s.get(WorkflowBatch, batch_id)
        if batch is None:
            return JSONResponse(
                {"ok": False, "error": str(batch_runs.BatchMissing(batch_id))}, status_code=404
            )
        out = batch_runs.batch_as_dict(s, batch, detail=detail)
    extra = {"retried": retried} if retried is not None else {}
    return JSONResponse(_scrub_spend(ident, {"ok": out["status"] != "failed", **extra, **out}))


# --- helpers -----------------------------------------------------------------
async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _json_object(request: Request):
    """(body, None) for a JSON object (an empty body is {}), else (None, a ready 400).

    #580: `_json` quietly turns a body that is not JSON (or not an object) into {}, so a
    create route answered "name is blank" to a caller who sent form data or a list. The
    create routes use this instead so the 400 says what was actually wrong."""
    raw = await request.body()
    if not raw.strip():
        return {}, None
    try:
        body = json.loads(raw)
    except ValueError:
        return None, JSONResponse(
            {"ok": False, "error": "request body is not valid JSON (send a JSON object)."},
            status_code=400,
        )
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"ok": False, "error": "request body must be a JSON object."}, status_code=400
        )
    return body, None


def glossary_import_error_response(exc: Exception) -> JSONResponse:
    """The /api answer for a glossary import (project, module) that raised (#615).

    * a big import that failed after some chunks committed: 500 with `partial: true`,
      `rows_written` and `rows_total` (the message names only the exception class);
    * a database error: a generic 500 that says nothing was stored or what was stored is kept
      -- the error text carries SQL and bound row values (glossary terms), so it goes to the
      server log, never into the reply;
    * anything else (a bad header, a bad TBX, the row cap): the existing 400 with its
      user-facing message."""
    import logging

    from sqlalchemy.exc import SQLAlchemyError

    if isinstance(exc, glossary.GlossaryImportIncomplete):
        return JSONResponse(
            {
                "ok": False,
                "error": f"could not import glossary: {exc}",
                "partial": True,
                "rows_written": exc.rows_written,
                "rows_total": exc.rows_total,
            },
            status_code=500,
        )
    if isinstance(exc, SQLAlchemyError):
        logging.getLogger(__name__).warning("glossary import: database error", exc_info=exc)
        return JSONResponse(
            {
                "ok": False,
                "error": "could not import glossary: a database error stopped the import; "
                "nothing was stored. Try again; if it repeats, check the server log.",
            },
            status_code=500,
        )
    return JSONResponse(
        {"ok": False, "error": f"could not import glossary: {exc}"}, status_code=400
    )


def _b64(raw):
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except Exception:
        return None


# ============================================================================
# LLMemory (PROPOSAL-llmemory Phase 1, work package (c)): the /api mirror for
# every `memory` CLI verb's --remote arm + the web /memories module's remote
# equivalent. RBAC mirrors the web routes (app/web/memories.py) exactly, per
# verb: add-note/distill -> can_tune_ai; invalidate/invalidate-since ->
# can_manage; purge -> is_admin; read (list/show/events/export) -> the docs-
# scoped gate alone (customer-confined via _memory_customer below).
#
# No business logic here: every write calls app.services.llmemory (the SAME
# functions the web routes and the in-process CLI call) and every read/scope/
# serialization concern calls app.services.llmemory_view — the package (c)
# presentation seam all three surfaces share, so they cannot drift.
# ============================================================================
def _memory_customer(s: Session, ident, org_id, raw):
    """Resolve AND AUTHORIZE the customer_id scope for a memory operation. Returns
    (customer_id, err) where `err` is a ready-to-return JSONResponse.

    A customer-bound caller (R2-3) is FORCED to its own customer (an explicit
    `raw` is ignored -- a bound token can never reach another customer's
    memories). An UNBOUND (internal-staff) caller must supply one explicitly,
    AND that id is resolved through `_customer_in_org` exactly like every other
    customer-scoped route in this file: a customer that does not exist, or lives
    in another ORG, is a 404 (never enumerable), not a scope to write into.

    WHY THIS TAKES A SESSION (adversarial review S1/S2 -- CRITICAL). This helper
    used to `int()` the request body's customer_id and hand it straight to the
    service. The org gate was never consulted, so an authenticated caller from
    org A could POST a note into an org-B customer and have it recalled into
    org-B's translate prompts (memory planting across a tenant boundary), and
    could reach the two DESTRUCTIVE sweep verbs -- `invalidate-since` (roll back
    another tenant's learned memories) and `distill` (whose response body
    discloses another tenant's event/entry volumes). Every other customer-scoped
    route here already resolved through `_customer_in_org`; these did not,
    because the id arrived in a BODY rather than a path. The fix is to make this
    the one funnel and give it the session it needs -- so no memory route can be
    written that skips the check."""
    bound = _cust_of(ident)
    if bound is not None:
        return bound, None
    cid = _int_id(raw)
    if cid is None:
        return None, JSONResponse(
            {"ok": False, "error": "customer_id is required."}, status_code=400
        )
    if _customer_in_org(s, cid, org_id, ident) is None:
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    return cid, None


def _memory_sweep_customers(s: Session, ident, org_id, qp):
    """`all_customers=1` on a memory/suggestion READ route with no `customer_id`: the ids of
    every customer VISIBLE to the caller (org-scoped; a customer-bound key is confined to its
    own), ordered by id, so the CLI's `--remote` sweep is ONE request instead of one per
    customer (#533). None = not a sweep request (the caller falls through to
    `_memory_customer`'s single-customer funnel, unchanged)."""
    if qp.get("customer_id") not in (None, "") or str(qp.get("all_customers") or "") not in (
        "1",
        "true",
    ):
        return None
    ids = s.exec(_scoped_customers(ident, org_id).order_by(Customer.id)).all()
    return [c.id for c in ids]


def _mock_spend_block_for_doc(s, project, locale, caller_provider):
    """409 when the effective provider for a doc/pair would resolve to the keyless
    offline mock (the A/B + image spend paths). The effective provider = an explicit
    caller override, else the project's per-pair resolution — exactly what
    style_ab/context_ab and the translate pipeline resolve internally, checked here
    BEFORE the harness runs so a 'real measurement' never silently runs on the mock."""
    from app.kernel import providers

    eff = str(caller_provider or "").strip().lower()
    if not eff:
        try:
            eff = pair_providers.resolve_provider(s, project, project.source_locale, locale).lower()
        except Exception:
            return None
    if eff != "mock" and not providers.provider_is_live(eff):
        return JSONResponse(
            {
                "ok": False,
                "error": "No translation engine configured - set an API key before submitting.",
            },
            status_code=409,
        )
    return None


def _ab_chosen_locale(s: Session, doc_id: int, locale):
    """The locale an A/B run would pick (explicit-if-present-else-first), for the mock
    preflight — mirrors run_ab's own choice so the 409 evaluates the exact pair that
    would run. None when the document has no segments (the caller gets a clean 400)."""
    locs = sorted(
        {
            sg.target_locale
            for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
    )
    if locale and locale in locs:
        return locale
    if locale and locale not in locs:
        return locale  # let run_ab raise its own precise "no segments for locale" ABError
    return locs[0] if locs else None


# ============================================================================
# SPEC-full-remote-parity Wave R2 — admin & identity over /api
#
# The MOST security-sensitive wave: identity + admin management on the remote
# surface. Every route mirrors the RBAC of its web/CLI twin exactly, rings every
# foreign id to a 404 (never 403 — an id in another org/customer is
# indistinguishable from a missing one), and audits every mutation with the KEY'S
# USER as actor. Three verbs are IRREVERSIBLE and gated behind BOTH the `admin`
# scope AND the `remote_admin_enabled` switch (a deliberate strengthening beyond
# the spec's Tier-3 placement — GDPR erase, retention purge-now, apikey revoke-all):
# no irreversible remote admin verb is reachable without the master opt-out.
#
# Secrets never cross the wire: apikey issue returns the new key ONCE by design (the
# single exception) and never audits it in cleartext; reset-password SETS a password
# and returns only ok+email (never a plaintext reveal); admin-scope MINTING is
# refused over /api so a stolen admin key cannot self-replicate.
# ============================================================================
def _user_in_org(s: Session, uid, org_id):
    """Fetch a User, or None if missing OR in another org (404-worthy). Users belong
    to an org, not a customer, so there is no customer ring here — and the identity
    routes are is_admin, so the caller is unbound staff anyway."""
    from app.models import User

    u = s.get(User, uid) if uid is not None else None
    if u is None:
        return None
    if org_id is not None and getattr(u, "org_id", None) != org_id:
        return None
    return u


def _resolve_user_ref(s: Session, ref, org_id):
    """Resolve a body/param user reference (numeric id OR email), org-scoped. Returns
    the User or None (a cross-org / missing / malformed ref is uniformly None, so a
    caller can never learn another org's user exists)."""
    from app.models import User

    ref = ("" if ref is None else str(ref)).strip()
    if not ref:
        return None
    rid = _int_id(ref)
    if rid is not None:
        u = s.get(User, rid)
    else:
        u = s.exec(select(User).where(User.email == ref.lower())).first()
    if u is None:
        return None
    if org_id is not None and getattr(u, "org_id", None) != org_id:
        return None
    return u


def _r3_gate(request: Request):
    """The full R3 three-lock preamble: `admin` scope + is_admin role +
    remote_admin_enabled. Returns (ident, org_id, blocked); `blocked` is a
    ready-to-return JSONResponse (403 on scope/role, 404 on the switch) or None. Every
    R3 route - READS INCLUDED - begins with this, so a switched-off install answers 404
    on the whole surface (the kill-switch law) and the surface is non-enumerable."""
    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return None, None, blocked
    b = _admin_block(ident)
    if b:
        return None, None, b
    rb = _remote_admin_block(ident)
    if rb:
        return None, None, rb
    return ident, org_id, None


# ============================================================================
# Reviewer-authored LQA findings (SPEC-wave3-collaboration §6.5, Train C) — the /api
# twin of the LQA page's reviewer surface + the `lqa` CLI group. CORE (no feature gate,
# §5.3). Writes are REVIEWER+ (_lqa_finding_review_block — can_review, with a refusal
# that NAMES the finding action byte-identically to the web gate, #70); edit/withdraw
# additionally need own-row OR can_manage+ (enforced in the service). The reviewer gate
# runs AFTER the doc-visibility ring (visibility-before-role, #70), so a foreign/invisible
# id 404s (non-enumerable) exactly like its GET twin. Reads mirror the LQA page:
# doc-visibility only.
# ============================================================================
def _lqa_finding_dict(f) -> dict:
    return {
        "id": f.id,
        "document_id": f.document_id,
        "segment_id": f.segment_id,
        "dimension": f.dimension,
        "severity": f.severity,
        "note": f.note,
        "target_locale": f.target_locale,
        "finding_source": f.finding_source,
        "actor": f.actor,
        "vendor_id": f.vendor_id,
        "dispatch_id": f.dispatch_id,
        "created_at": str(f.created_at),
        "withdrawn_at": str(f.withdrawn_at) if f.withdrawn_at else None,
    }


def _lqa_finding_api_error(exc):
    """Map a service finding error to the canonical /api envelope (400 input / 403 rbac)."""
    from app.kernel import lqa as _lqa

    code = 403 if isinstance(exc, _lqa.HumanFindingForbidden) else 400
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=code)
