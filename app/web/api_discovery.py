"""api: discovery

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _app_version, _FAILURE_KEYS, _FAILURE_IDS,
_FAILURE_CAP, _FAILURE_COUNT_CAP, _clean_failures, vendor_return_callback, vendor_quote_callback
from app.web.api_discovery_common.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters.base import InputLimitError
from app.kernel import (
    access,
    audit,
    budgets,
    content,
    export,
    jobs,
    languages_catalog,
    locale_seed,
    pair_providers,
    project_budgets,
    providers,
    routing,
    security,
    tm_delivery,
)
from app.kernel.connectors import store as conn_store
from app.models import Document, Job, JobStatus, Project, Segment, TargetLocale
from app.release import coarse_version
from app.web import deps
from app.web.api_discovery_common import (
    _FAILURE_CAP,
    _FAILURE_COUNT_CAP,
    _FAILURE_IDS,
    _FAILURE_KEYS,
    _app_version,
    _clean_failures,
    vendor_quote_callback,
    vendor_return_callback,
)
from app.web.api_discovery_common import router as api_discovery_common_router

__all__ = [
    "_FAILURE_CAP",
    "_FAILURE_COUNT_CAP",
    "_FAILURE_IDS",
    "_FAILURE_KEYS",
    "_app_version",
    "_clean_failures",
    "vendor_quote_callback",
    "vendor_return_callback",
]

router = APIRouter()


_APP_VERSION = _app_version()


_APP_VERSION_COARSE = coarse_version(_APP_VERSION)


_UNAUTHORIZED = {"ok": False, "error": "unauthorized"}


_NOT_FOUND = {"ok": False, "error": "not found"}


_FORBIDDEN = {"ok": False, "error": "your role does not allow this action."}


# L5: evaluated ONCE at import (the read is allocation-light and the anonymous liveness path must
# not re-read a file per request). The tradeoff: an IN-PLACE upgrade without a process restart keeps
# serving the version the process started on -- a version bump is reflected only after run.py is
# restarted, which the deploy already does. `_APP_VERSION_COARSE` is what the anonymous field shows.
# `coarse_version` is the ONE coarsener shared by both keyless routes (here and app/web/health.py),
# derived from the same RELEASE source (app.release) as the full string.
# --- write-role RBAC (parity with the web /connectors routes) ----------------
# The web connector-write routes (app/web/connectors.py pull/push/sync/retry) all
# require _ai_block (can_tune_ai: admin or linguist). These /api equivalents are
# the MCP/companion-app front door and historically enforced NO role at all — any
# valid token could pull/push/sync/retry regardless of the caller's role. This
# mirrors api_cli.py's _role_block pattern: a strict no-op when auth_required()
# is off for per-user identities.
#
# Legacy shared tokens have no operational authority, including with auth off.
# Companion apps must present a per-user key with the required role and confinement.
def _ai_role_block(ident: dict):
    """Require a user identity, then can_tune_ai when the login wall is enabled."""
    if ident.get("kind") != "user":
        return JSONResponse(_FORBIDDEN, status_code=403)
    if not security.auth_required():
        return None
    if security.can_tune_ai(ident.get("user")):
        return None
    return JSONResponse(_FORBIDDEN, status_code=403)


_JIRA_BATCH_ADMIN = {
    "ok": False,
    "error": (
        "Running or scheduling a Jira batch (saved JQL, a JQL translate, or more than 10"
        " named issues) needs an admin account."
    ),
}


def _jira_batch_admin_block(ident: dict, conn_kind: str):
    """Fail-closed admin gate for the paid, org-wide Jira batch actions (#517).

    The Forge app calls with one install-wide key, so the server cannot see which Jira
    user pressed the button; the key's account must itself be an admin for a batch to
    run. Single-issue translate stays at the normal write role. No-op for other kinds
    and while the login wall is off (single-operator mode)."""
    if conn_kind != "jira" or not security.auth_required():
        return None
    if security.is_admin(ident.get("user")):
        return None
    return JSONResponse(_JIRA_BATCH_ADMIN, status_code=403)


# Config keys that turn on the scheduler for a connection (app/services/scheduler.py).
_SCHEDULE_KEYS = ("sync_every_minutes", "sync_next_at")


def _jira_schedule_admin_block(ident: dict, conn_kind: str, *, minutes=None, cfg=None):
    """Turning ON a Jira schedule starts recurring paid, org-wide saved-JQL batches, so it
    is admin only like the batch itself (#517 gate). Turning a schedule off stays open.
    minutes: the /schedule interval; cfg: a create/update config that could carry the
    scheduler keys directly."""
    if conn_kind != "jira":
        return None
    turning_on = (minutes is not None and minutes > 0) or any(
        k in (cfg or {}) for k in _SCHEDULE_KEYS
    )
    if not turning_on:
        return None
    return _jira_batch_admin_block(ident, conn_kind)


# A non-admin may name only a few Jira issues per call (the Forge button sends exactly one).
_JIRA_NAMED_KEYS_MAX = 10


def _jira_named_keys_block(ident: dict, conn_kind: str, keys) -> JSONResponse | None:
    """A caller-built list of more than _JIRA_NAMED_KEYS_MAX distinct Jira issues is a
    batch in disguise (a loop of single-issue calls, or a hand-written retry list): admin
    only, like the saved-JQL batch (#517). Applies to /jira/translate issue_keys and to
    caller-supplied retry lists; a retry read from a real job's stored failures is not
    caller-built and is not counted here."""
    if conn_kind != "jira":
        return None
    distinct = {str(k).strip() for k in (keys or []) if str(k or "").strip()}
    if len(distinct) <= _JIRA_NAMED_KEYS_MAX:
        return None
    return _jira_batch_admin_block(ident, conn_kind)


def _job_readable(ident: dict, job) -> bool:
    """Cross-user isolation for job results (they carry filenames/stats).
    Managers+ read everything; a per-user key otherwise reads only its own
    jobs; the shared connector token cannot read jobs. Auth OFF for users is
    the single-operator mode — no scoping, matching the rest of the app.

    MULTI-ORG (sacred gate): a manager who can read "everything" is still an
    org-admin of their HOME org here (the linchpin) — org isolation is layered
    ON TOP so even a manager never reads another org's job (leak vectors 5/6). A
    Job has no org_id; its org is its creator's org (jobs.job_org_id). Org
    resolution uses the identity's org_id, the per-user HOME org.
    A NULL-org job (creator org unknown) never matches a scoped caller. User org
    scoping is a no-op while the multi-org gate is OFF. Shared credentials are
    refused regardless of either gate."""
    if ident.get("kind") != "user":
        return False
    if not security.auth_required():
        return True
    if access.multi_org_active():
        job_org = jobs.job_org_id(job)
        if job_org is None or _ident_org_id(ident) != job_org:
            return False
    if ident["kind"] == "user":
        with Session(engine) as session:
            if not access.can_view_job_projects(session, ident["user"], job):
                return False
        return security.can_manage(ident["user"]) or job.created_by == ident["actor"]
    return False


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_discovery_common_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


# ---------------------------------------------------------------------------
# Document endpoints (the Claude Desktop / MCP "front door", Phase A)
#
# These require a per-user API key carrying the "docs" scope (deps._require_doc_scope):
# the shared connector token is rejected outright, and a plain "api" key is 403.
# Object-level authz reuses the project's created_by / can_manage rules. No `mcp`
# import lives anywhere in the app — these are plain HTTP routes.
# ---------------------------------------------------------------------------


def _ident_org_id(ident: dict):
    """The org an inbound API identity is scoped to — delegates to the single
    authority in deps (P4). Kept as a thin module-local alias so the many call
    sites below read tersely; deps._ident_org_id is the source of truth (reads the
    org_id api_identity resolved onto the dict). None with the gate off / an
    org-less identity — byte-identical to today."""
    return deps._ident_org_id(ident)


def _ensure_inbox_project(s: Session, ident: dict) -> Project:
    """Find (or create) the inbox project uploads land in when the caller doesn't
    name a project. Delegates to routing.ensure_department_inbox — the single inbox
    authority: one shared inbox per department, else one per actor (no department /
    auth-off), preserving the pre-Phase-B per-actor 'MCP Inbox (<actor>)' behaviour.

    When multi-org is active the inbox is stamped with the caller's org so an
    auto-provisioned inbox lands in the caller's org (leak vector 11)."""
    return routing.ensure_department_inbox(
        s, routing.department_for(ident), ident["actor"], org_id=_ident_org_id(ident)
    )


def _ensure_doc_locale_segments(s: Session, doc_id: int, locale: str) -> None:
    """Seed source segments for `locale` if the document has none for it yet.

    The web UI adds a project's target locales BEFORE upload, so ingest seeds one
    segment set per locale. The MCP front door uploads first and picks locales at
    translate time, so a brand-new target locale can have zero segments. Clone the
    source_text/order_index/inline_tags from whatever locale ingest already seeded
    (the placeholder "—" when the project had no locales, else the first one)."""
    locale_seed.seed_locale(s, doc_id, locale, strict=True)


def project_locale_spelling(s: Session, project_id, locale: str) -> str:
    """`locale` in the spelling the project already stores for that language (a project holding
    pt-BR translates "pt-br" as pt-BR), else as given. The translate doors seed, enqueue and
    translate in this spelling, so a second spelling never makes a second segment set."""
    want = languages_catalog.normalize(locale)
    for tl in s.exec(
        select(TargetLocale).where(TargetLocale.project_id == project_id).order_by(TargetLocale.id)
    ).all():
        if languages_catalog.normalize(tl.locale) == want:
            return tl.locale
    return locale


def prepare_translate_locale(s: Session, doc: Document, locale: str):
    """Register and seed a target after the caller clears document and launch gates.

    Shared by the dedicated translate endpoint and the generic jobs API. Empty
    source documents cannot produce a translation and must be refused before launch.
    """
    if s.exec(select(Segment.id).where(Segment.document_id == doc.id)).first() is None:
        return JSONResponse(
            {"ok": False, "error": "Document has no source segments to translate."},
            status_code=400,
        )
    # #617 item 4 (gate): a run parked on a human_review step holds this locale.
    from app.kernel import segment_review

    try:
        segment_review.refuse_if_awaiting_review(s, doc.id, locale)
    except segment_review.AwaitingReviewError as exc:
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": str(exc), "run_id": exc.run_id},
            status_code=exc.status,
        )
    try:
        _ensure_doc_locale_segments(s, doc.id, locale)
    except InputLimitError as exc:
        # #576: the document would pass MAX_DOCUMENT_ROWS with this language. Refused
        # before any engine call; nothing was seeded (413). #687: a bilingual file is
        # translated only into the language it declares (its refusal names 422).
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=getattr(exc, "http_status", 413)
        )
    # one row per language: a project holding pt-BR already has pt-br (catalog-normalized)
    want = languages_catalog.normalize(locale)
    target = next(
        (
            tl
            for tl in s.exec(
                select(TargetLocale).where(TargetLocale.project_id == doc.project_id)
            ).all()
            if languages_catalog.normalize(tl.locale) == want
        ),
        None,
    )
    if target is None:
        s.add(TargetLocale(project_id=doc.project_id, locale=locale))
        s.commit()
    return None


def _doc_authz(s: Session, ident: dict, doc: Document):
    """Object-level authz for a document: the caller must be able to VIEW the
    document's project. This mirrors upload (api_upload_document) and the web UI
    translate/export routes, which all gate on access.can_view_project — owner +
    ProjectMembers + (when project_scoping is off, the default) any authenticated
    user. Without this, a legitimate member/non-scoped user who can upload and
    translate/download in the UI got a spurious 404 from the API.

    Returns a JSONResponse 404 when the doc/project is missing or out of scope
    (ids must not be enumerable — 404 not 403 to match the routes' semantics),
    else (project, None).

    SPEC-key-based-projects 2.5 (Wave B fix round, A-F3): an INTERNAL key document
    answers the same 404 as a missing one. The web document routes gained
    `deps._visible_doc`, but this /api path did not, so a plain docs-scoped token
    could read a key project's segments (the TRASH document's included), apply TM
    straight into value rows and pass the translate authz — i.e. the docs scope
    reached key internals that the deliberately separate `keys` scope exists to
    gate. One test in the shared authz seam covers every route that calls it."""
    if doc is None:
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if doc.fmt == "keys":
        from app.kernel import keys as _keys

        if _keys.is_internal_key_document(doc):
            return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    project = s.get(Project, doc.project_id)
    if project is None:
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if not security.auth_required():
        return project, None
    if access.can_view_project(s, ident.get("user"), project):
        return project, None
    return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)


def _dedupe(items) -> list:
    """De-duplicate an iterable, preserving first-seen order."""
    seen: set = set()
    out: list = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _active_translate_locales(doc_id: int) -> set[str]:
    """Locales that already have a QUEUED or RUNNING translate_document job for
    this document — used to skip re-enqueuing a duplicate paid-MT run for the
    same (document_id, locale). Mirrors the connector overlap-guard pattern
    (query active jobs, filter by payload)."""
    with Session(engine) as s:
        active = s.exec(
            select(Job).where(
                Job.kind == "translate_document",
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        ).all()
    return {
        (j.payload or {}).get("locale")
        for j in active
        if (j.payload or {}).get("document_id") == doc_id and (j.payload or {}).get("locale")
    }


class _LaunchRefused(Exception):
    """A paid-MT launch refused by `translate_launch_gate`; `.response` is the ready JSON error."""

    def __init__(self, content, status_code=400):
        super().__init__(content.get("error", "") if isinstance(content, dict) else "")
        self.response = JSONResponse(content, status_code=status_code)


def translate_locales_block(targets):
    """400 for a locale code not in the language catalog (BUG-A11), else None. Shared by the
    dedicated translate door and the generic /api/jobs/run door (#370) so they cannot drift."""
    invalid = [t for t in targets if not languages_catalog.is_valid(t)]
    if invalid:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown locale code(s): "
                + ", ".join(invalid)
                + " (not in the language catalog).",
            },
            status_code=400,
        )
    return None


def translate_launch_gate(s, ident, doc, project, targets, caller_provider):
    """Every cost/engine control a paid-MT launch must clear, in order: viewer refusal (#283),
    department token budget (429), project USD cap (429, evaluating the 100% alert), then the
    routing decision, effective provider per locale and the mock-engine hard-fail (409).

    The ONE implementation behind both POST /api/documents/{id}/translate and the generic
    POST /api/jobs/run translate_document kind (#370), so the two doors cannot drift. Returns
    (None, ctx) with ctx = {dept, provider_for, project_default, pb_project_id}, or
    (JSONResponse, None) when refused."""
    try:
        return None, _translate_launch_gate(s, ident, doc, project, targets, caller_provider)
    except _LaunchRefused as exc:
        return exc.response, None


def _translate_launch_gate(s, ident, doc, project, targets, caller_provider):
    # #283: a viewer's docs key may read but never start paid MT. The same
    # service rule as the web live stream and background translate (/api
    # bypasses the web middleware's role gate, so it must be applied here).
    from app.kernel import translate as _translate

    if not _translate.can_start_mt(ident.get("user")):
        raise _LaunchRefused(_FORBIDDEN, status_code=403)

    # --- per-department budget (Phase B chunk 4) ------------------------
    # Gate BEFORE the mock-hard-fail and BEFORE enqueuing anything: if the
    # submitting actor's department has used up its monthly translation-token
    # budget, refuse the whole request with a clear 429 and enqueue no jobs.
    # A department with no budget row (or auth-off / no-department callers) is
    # unlimited, so existing callers are unaffected.
    dept = routing.department_for(ident)
    # Org-scoped budget (leak vector 12): org A's cap must never gate org B.
    allowed, _remaining, msg = budgets.check(dept, org_id=_ident_org_id(ident))
    if not allowed:
        raise _LaunchRefused({"ok": False, "error": msg, "remaining": 0}, status_code=429)

    # --- per-PROJECT USD budget (CV-2) ----------------------------------
    # Same refuse-before-enqueue posture as the department token cap, but in
    # MONEY and keyed to the document's project (org resolved from the project,
    # not the caller): at 100% of monthly_usd_limit (ENFORCED spend = AI + settled
    # vendor human), block the whole request with a 429. Unlimited when the project
    # has no budget row / limit <= 0. Human review/editing is never gated here.
    _pb_project_id = doc.project_id
    p_allowed, _p_rem, p_msg = project_budgets.project_check(_pb_project_id)
    if not p_allowed:
        # H1: the BLOCKED submission IS the 100% crossing moment — evaluate the
        # threshold alert (band=100 audit + email) BEFORE returning the refusal,
        # else the "budget EXCEEDED" alert would be unreachable dead code.
        project_budgets.evaluate_thresholds(_pb_project_id, actor=ident.get("actor", ""))
        raise _LaunchRefused({"ok": False, "error": p_msg, "remaining": 0}, status_code=429)

    # --- content routing (Phase B) --------------------------------------
    # The document already belongs to a project (chosen at upload) and is NEVER
    # reassigned here. But a routing rule's PROVIDER override must still win over
    # the project/config default (Finding 1). So we resolve for MATCHING only
    # (explicit_project_id=None, ensure_inbox=False) — passing the doc's project
    # as an explicit override would short-circuit the rule loop and discard the
    # rule's provider. The decision's project_id is ignored; only its provider is
    # used. No workflow/orchestrator switch in v1 (§9.3).
    decision = routing.resolve(
        s,
        ext=(doc.fmt or ""),
        department=routing.department_for(ident),
        actor=ident["actor"],
        explicit_project_id=None,
        ensure_inbox=False,
        org_id=_ident_org_id(ident),
    )

    # --- effective provider per (project, locale) pair ------------------
    # Resolve the effective engine EACH LOCALE would use. Provider precedence:
    #   caller-explicit > rule (decision.provider) > per-pair mapping (Phase 2)
    #   > project default > config default.
    # Phase 2 inserts the per-pair resolver at the "project default" rung: with
    # no caller override and no rule, a PER_PAIR project picks each pair's own
    # engine (a UNIFORM project resolves to project.default_provider, so this is
    # byte-identical to the pre-Phase-2 single `provider` for uniform projects).
    # Computed per LOCALE because different pairs can resolve to different
    # engines — a single pre-loop provider would wrongly pin them all.
    project_default = (project.default_provider or config.DEFAULT_PROVIDER or "").lower()

    def _provider_for(loc: str) -> str:
        if caller_provider:
            return caller_provider.lower()
        if decision.provider:
            return decision.provider.lower()
        return pair_providers.resolve_provider(s, project, project.source_locale, loc).lower()

    # #580: a provider NAME that exists nowhere (a typo) is a 400 naming it, not the
    # "no engine configured" 409 below, which tells the caller to set an API key that
    # cannot help. Only the CALLER's explicit value is judged here; stored project/rule
    # providers keep the existing path.
    if caller_provider and providers.provider_name_is_unknown(caller_provider):
        raise _LaunchRefused(
            {
                "ok": False,
                "error": f"unknown provider {caller_provider!r}: use claude, gemini, openai, "
                "local, mock, or an engine reference like engine:<id>.",
            },
            status_code=400,
        )

    # --- mock-engine hard-fail (the day-one trust killer) ---------------
    # Refuse if ANY requested locale would silently fall back to MockProvider.
    # The 409 hard-fail evaluates the FINAL effective provider per locale, so a
    # rule (or a per-pair mapping) routing to a keyless real provider still
    # 409s. A "mock" resolution is the dev escape hatch.
    for loc in targets:
        eff = _provider_for(loc)
        if eff != "mock" and not providers.provider_is_live(eff):
            raise _LaunchRefused(
                {
                    "ok": False,
                    "error": "No translation engine configured - set an API key "
                    "before submitting for translation.",
                },
                status_code=409,
            )
    return {
        "dept": dept,
        "provider_for": _provider_for,
        "project_default": project_default,
        "pb_project_id": _pb_project_id,
    }


@router.post("/api/documents/{doc_id}/translate")
async def api_translate_document(doc_id: int, request: Request):
    """Enqueue MT pre-translation, one job per target locale. Requires the docs
    scope + object-level authz. Body: {"targets": [locale, ...]} (or
    "target_locales"). CRITICAL: hard-fails BEFORE enqueuing anything if the
    resolved engine would be the MockProvider placeholder (no key configured) —
    provider="mock" is the one explicit dev escape hatch. Returns 202 with one
    job per target."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    targets = payload.get("targets") or payload.get("target_locales") or []
    if isinstance(targets, str):
        targets = [targets]
    # Reject a non-list, or a non-string ELEMENT (a nested list / int / 999), BEFORE any
    # str()-coercion (BUG-A11): the old `str(t)` turned junk into a locale string that
    # then permanently registered a bogus TargetLocale AND enqueued a paid MT job. A
    # non-string target is a 422 (type error), pre-spend.
    if not isinstance(targets, (list, tuple)) or any(not isinstance(t, str) for t in targets):
        return JSONResponse(
            {"ok": False, "error": "target locales must be strings."}, status_code=422
        )
    # De-duplicate the normalized list, preserving first-seen order, so a body
    # like {"targets": ["fr-FR", "fr-FR"]} never enqueues the same (doc, locale)
    # translate job twice (double paid MT + two workers racing the segments).
    targets = _dedupe(t.strip() for t in targets if t.strip())
    if not targets:
        return JSONResponse(
            {"ok": False, "error": 'No target locales given (body: {"targets": [...]} ).'},
            status_code=400,
        )
    # Catalog-validate BEFORE the auto-register / segment-seed / enqueue loop (BUG-A11):
    # a code not in the language catalog is refused with a clean 400 — the SAME gate
    # `add-locale` applies (is_valid accepts any catalog code even if not currently
    # enabled; only genuinely-unknown codes reject). This is the only locale-entry point
    # that used to skip the check, so a typo'd/hostile locale ("../etc/passwd",
    # "xx-NONSENSE") could pollute the project and spend real MT budget. Valid new
    # locales still auto-register (the intended "translate a new locale" flow).
    refused = translate_locales_block(targets)
    if refused:
        return refused

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        # one language, one spelling: a target the project already has is used in its spelling
        targets = _dedupe(project_locale_spelling(s, doc.project_id, t) for t in targets)
        blocked, _gate_ctx = translate_launch_gate(
            s, ident, doc, project, targets, str(payload.get("provider") or "").strip()
        )
        if blocked:
            return blocked
        dept = _gate_ctx["dept"]
        _provider_for = _gate_ctx["provider_for"]
        project_default = _gate_ctx["project_default"]
        _pb_project_id = _gate_ctx["pb_project_id"]

        # Effective provider to STAMP on each job, PER LOCALE — only when it differs
        # from the project default (so the common no-rule/no-override UNIFORM case
        # enqueues a byte-identical payload with no provider key, and the job handler
        # resolves via spec_for_pair). This is what makes a routing rule's / caller's
        # / per-pair provider actually WIN at translate time. Stamped per locale
        # because a PER_PAIR project can differ across pairs (Finding 1 generalized).
        provider_stamp_by_loc = {
            loc: (p if (p := _provider_for(loc)) != project_default else "") for loc in targets
        }

        # Both API launch doors register and seed only after the same gates.
        for loc in targets:
            try:
                blocked = await deps.offload_blocking(prepare_translate_locale, s, doc, loc)
            except deps.WorkerBusy as exc:
                return deps.busy_response(str(exc))
            if blocked:
                return blocked

        # Per-locale estimated token cost, for the in-flight budget accounting
        # (Finding 2). Heuristic: total source characters for that locale's segments
        # / 4 (a common ~4-chars-per-token rule of thumb, in+out lumped together and
        # deliberately conservative). Stamped on the job payload so budgets.spend_
        # this_month can count a QUEUED/RUNNING job's estimate before it produces
        # real token stats.
        est_by_locale = {
            loc: max(
                1,
                sum(
                    len(st or "")
                    for st in s.exec(
                        select(Segment.source_text).where(
                            Segment.document_id == doc_id, Segment.target_locale == loc
                        )
                    ).all()
                )
                // 4,
            )
            for loc in targets
        }

    # Department stamped on the job so budget accounting sums by the SUBMIT-TIME
    # department, not the actor's current (possibly later-changed) User.department
    # (Finding 4).
    dept_stamp = dept

    # Overlap guard (mirrors the connector submit_unique pattern): skip any
    # locale that already has a QUEUED/RUNNING translate_document job for this
    # document, so a quick second call — or an automation retry — can't stack a
    # duplicate paid-MT run against the same (document_id, locale).
    active = _active_translate_locales(doc_id)
    out, skipped = [], []
    budget_refused: list[str] = []
    budget_msg = ""
    for loc in targets:
        if loc in active:
            skipped.append(loc)
            continue
        # PII behavior change (2026-07-03): the API no longer FORCES redaction.
        # Inbound API submissions on the pretranslate path are scrubbed only when
        # the global pii_redaction setting is on (the blanket fallback); PII is a
        # per-step choice on the workflow path (WorkflowStep.redact_pii). The proper
        # replacement — routing an API submission to a customer workflow that decides
        # PII — is a follow-up in the SaaS/multi-org work.
        job_payload = {
            "document_id": doc_id,
            "locale": loc,
            "est_tokens": est_by_locale.get(loc, 1),
        }
        if dept_stamp:
            job_payload["department"] = dept_stamp
        if provider_stamp_by_loc.get(loc):
            job_payload["provider"] = provider_stamp_by_loc[loc]
        # #503 item 2: re-check the department budget and insert the Job under one lock, so a
        # burst of concurrent submits cannot all pass the launch-gate check before any of
        # them is counted. A refusal stops the loop: the cap is used up for every later locale.
        try:
            jid = jobs.submit_budgeted(
                "translate_document",
                job_payload,
                actor=ident["actor"],
                department=dept_stamp,
                org_id=_ident_org_id(ident),
            )
        except jobs.LiveBudgetRefused as exc:
            budget_msg = str(exc)
            budget_refused.append(loc)
            continue
        active.add(loc)  # guard within this same request too
        out.append({"locale": loc, "job_id": jid, "status_url": f"/api/jobs/{jid}"})
    audit.log(
        actor=ident["actor"],
        action="document.translate",
        target=f"doc {doc_id} -> {','.join(j['locale'] for j in out) or '(none)'}",
        ip=deps._ip(request),
        customer_id=audit.customer_for_document(doc_id),
    )
    # A submission that PASSED (and cleared the mock-fail 409 gate, L6) may have crossed
    # 80% — evaluate the threshold alert now, after the request's own validations.
    project_budgets.evaluate_thresholds(_pb_project_id, actor=ident["actor"])
    if budget_refused and not out:
        # Nothing was enqueued: the department cap was used up between the launch gate and
        # the insert (#503), the same 429 the gate gives.
        return JSONResponse({"ok": False, "error": budget_msg, "remaining": 0}, status_code=429)
    body = {"ok": True, "jobs": out, "skipped": skipped}
    if budget_refused:
        body["budget_refused"] = budget_refused
    return JSONResponse(body, status_code=202)


# --- FULL_TEXT per-paragraph view + recovery (GitHub #42) --------------------
# Thin callers of app.services.fulltext_paragraphs — the SAME seam the web editor page
# and `loc-tms doc paragraphs / revert-paragraph / set-paragraph` call, so the three
# surfaces cannot drift. RBAC: the READ (GET paragraphs) is _require_doc_scope +
# _doc_authz like api_download_document; the three WRITE routes (save / revert /
# revert-all-damaged) ADD _write_role_block (can_write, >= translator) so a viewer's
# docs key can no longer overwrite target text -- parity with their web twins, which the
# login-wall middleware already gates at can_write, and with the approve / apply-tm /api
# siblings. The refusal-status mapping is the seam's own classification via
# `deps.paragraph_status`, so this route group and documents.py's form routes cannot
# report the same event with two different codes.
@router.get("/api/documents/{doc_id}/paragraphs")
def api_document_paragraphs(doc_id: int, request: Request, locale: str | None = None):
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    if not locale or not locale.strip():
        return JSONResponse({"ok": False, "error": "locale is required"}, status_code=400)
    from app.kernel import fulltext_paragraphs as ftp

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        res = ftp.split_view(s, doc, (locale or "").strip())
    return JSONResponse(res, status_code=deps.paragraph_status(res))


@router.get("/api/documents/{doc_id}/download")
def api_download_document(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    allow_source_fallback: int = 0,
    allow_held: int = 0,
    as_fmt: str = "",
):
    """Download a translated file. Requires the docs scope + object authz. Sets
    the correct per-format MIME (fixes the DOCX-hardcoded export bug) and audits
    the download. 404 if the doc/locale is missing. A HELD locale (no committed
    translation) is a 409 whose body names `allow_held=1` as the explicit source-anyway
    override (review M1 / the #42 "name a way out" lesson)."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    locale = (locale or "").strip()
    if not locale:
        return JSONResponse({"ok": False, "error": "locale is required"}, status_code=400)
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        # #593 gate: a read-only key must not take a source-filled or held file
        # (visibility first, so an invisible document still 404s, never a role 403).
        _ovr = deps.export_override_block(
            ident.get("user"), requested=bool(allow_held or allow_source_fallback)
        )
        if _ovr is not None:
            return _ovr
        locales = {
            sg.target_locale
            for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
        if not locale or locale not in locales:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        # `as_fmt=po`: a gettext MO upload delivered as its PO source (export_as_formats).
        as_fmt = (as_fmt or "").strip().lower()
        if as_fmt and as_fmt not in export.export_as_formats(doc.fmt):
            return JSONResponse(
                {"ok": False, "error": f"as_fmt={as_fmt} is not available for a {doc.fmt} file"},
                status_code=400,
            )
        # chip task_9c665111: a HELD locale (no committed translation -> pure source)
        # is refused with a 409, never silently downloaded as source (FINDING 8). A
        # delivery guard; checked before the build. Mirrors the web + CLI refusal.
        # M1: `allow_held=1` is the explicit override; the 409 body names it.
        # Batch 15 T2 (§9.1/LAW-TM-14): the TM-only empty gate COMPANION, consulted before
        # the classic held check and NOT bypassable by `allow_held`. HOLD -> 409 naming the
        # audited approval; RELEASE_EMPTIES -> deliver empties; PASS -> classic logic.
        # ISSUE #95 (gate C round 1, H1): the QUALITY-GATE hold COMPANION, consulted
        # first and NOT bypassable by allow_held -- these segments were produced by a
        # run whose correction loop ended with its gate still FAILING, so they are
        # unreviewed failing text, not a missing translation.
        _qgate = export.gate_held_reason(s, doc, locale)
        if _qgate is not None:
            return JSONResponse(
                {
                    "ok": False,
                    "gate_held": True,
                    "error": _qgate,
                    "locale": locale,
                    "review_url": f"/documents/{doc_id}/editor?locale={locale}",
                },
                status_code=409,
            )
        _gate = tm_delivery.empty_gate_decision(s, doc=doc, locale=locale)
        if _gate == tm_delivery.HOLD:
            empties = tm_delivery.doc_empties(s, doc, locale)
            return JSONResponse(
                {
                    "ok": False,
                    "empty_held": True,
                    # #105: one seam builds the diagnosis (naming an in-flight run when
                    # there is one); this route appends its own way-out.
                    "error": tm_delivery.held_message(
                        s,
                        doc=doc,
                        locale=locale,
                        count=len(empties),
                        unit="segment",
                        way_out=(
                            "The approval is not bypassable with allow_held: "
                            f"POST /api/documents/{doc_id}/deliver-approve?locale={locale}."
                        ),
                    ),
                    "locale": locale,
                    "empties": empties,
                    "empties_url": f"/api/documents/{doc_id}/empties?locale={locale}",
                    "approve_url": f"/api/documents/{doc_id}/deliver-approve?locale={locale}",
                },
                status_code=409,
            )
        if _gate != tm_delivery.RELEASE_EMPTIES and not allow_held:
            held = export.held_locale_reason(s, doc, locale)
            if held is not None:
                return JSONResponse(
                    {
                        "ok": False,
                        "held": True,
                        "error": held,
                        "locale": locale,
                        "export_anyway_url": (
                            f"/api/documents/{doc_id}/download?locale={locale}&allow_held=1"
                        ),
                    },
                    status_code=409,
                )
        # AI Tagging 1.6: a FULL_TEXT paragraph/token desync quarantines the WHOLE
        # export — surface a clean 409, never ship a corrupt file (nor a raw 500).
        # AI Tagging 1.7 (#41): the substitution report rides ALONGSIDE the bytes (a
        # JSON body would corrupt the download, and the remote CLI arm reads
        # resp.content as file bytes) — same header the web route emits.
        # GitHub #42 (C-3): `allow_source_fallback=1` is the EXPLICIT per-export override
        # for a FULL_TEXT guard-B desync; bounded by export_source_fallback_max_share,
        # guard A untouched.
        rep: list = []
        try:
            data = export.build_translated_file(
                s,
                doc,
                locale,
                report=rep,
                allow_source_fallback=bool(allow_source_fallback),
                as_fmt=as_fmt or None,
            )
        except export.TagExportQuarantine as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "quarantined": True,
                    "error": str(exc),
                    "kind": exc.kind,
                    "paragraphs": exc.paragraphs,
                },
                status_code=409,
            )
        except export.ExportMissingBlob as exc:
            # Issue #54 L3: the seam now raises a TYPED missing-blob error (was a raw
            # FileNotFoundError). Same clean 409 as the generic guard below -- an honest
            # "original file missing", never a 500 traceback.
            return JSONResponse(
                {"ok": False, "missing_blob": True, "error": str(exc)}, status_code=409
            )
        except export.ExportUnreadableBlob as exc:
            # F4: a PRESENT-but-corrupt/undecryptable blob is a distinct honest 4xx.
            return JSONResponse(
                {"ok": False, "unreadable_blob": True, "error": str(exc)}, status_code=409
            )
        except (FileNotFoundError, OSError):
            # BUG-A6-1: the Document row's on-disk blob is missing/unreadable (a wiped
            # volume, a manually-deleted upload). A clean 409 beats a raw 500 traceback.
            return JSONResponse(
                {
                    "ok": False,
                    "error": "document content unavailable "
                    "(the stored file is missing or unreadable).",
                },
                status_code=409,
            )
        fmt = as_fmt or (doc.fmt or "").lower()
        # Gate MED-2: not a filesystem sink (this name only reaches a header), but it is
        # composed from the STORED filename like the CLI's export default, so it goes through
        # the SAME helper -- one rule for every `<stem>.<locale>.<fmt>` in the product.
        fname = content.safe_output_name(
            doc.filename,
            locale,
            deps.export_ext_for(fmt) or "bin",
            fallback=f"doc{doc_id}",
            neutralise=False,
        )

    # The detail names the KIND (routine #41 substitution vs an explicit
    # allow_source_fallback=1 force-export) and the UNIT (segment order_index vs
    # paragraph index) from the seam's own record, not from this route's query param.
    audit.log(
        actor=ident["actor"],
        action="document.download",
        target=f"doc {doc_id} {locale}",
        detail=export.substitution_detail(rep),
        ip=deps._ip(request),
    )
    # RFC 6266 (content_disposition_attachment): a NON-ASCII filename (CJK/emoji -- rename
    # accepts them; only `"`/CRLF are rejected) cannot latin-1-encode into a bare
    # `filename="..."` on the ASGI wire -> a 500. Emit an ASCII `filename=` fallback plus a
    # `filename*=UTF-8''<percent-encoded>` ext-value. Still neutralises `"`/`\`/control chars
    # (a row renamed before the F3 guard, or a hostile `locale`/`fmt` piece).
    headers = {"Content-Disposition": content.content_disposition_attachment(fname)}
    if rep:
        headers["X-Export-Substituted"] = str(len(rep))
        # The remote CLI arm reads resp.content as FILE BYTES, so the only channel it
        # has is the headers — and a bare count made it print "%d segment(s)" for what
        # are PARAGRAPHS on the FULL_TEXT path. ASCII by construction
        # (`substitution_detail` emits only kind/unit/index), which HTTP headers require.
        headers["X-Export-Substituted-Detail"] = export.substitution_detail(rep)
    return Response(
        content=data,
        media_type=deps.mime_for_fmt(fmt),
        headers=headers,
    )


# --- discovery -------------------------------------------------------------
@router.get("/api/release")
def api_release(request: Request):
    """Read build identity and dependency inventory; docs-scoped user keys only."""
    _ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    from app.release import release_manifest

    return JSONResponse(release_manifest())


_CONNECTOR_SECRET_FIELDS = (
    "secret",
    "secrets",
    "api_key",
    "api_token",
    "token",
    "password",
    "auth_token",
    "signing_secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "bearer",
    "pat",
    "credentials",
    "service_account_json",
    "private_key",
)


# ============================================================================
# Wave R2 — connector CRUD (NON-SECRET). create/update/test/browse/schedule are
# can_tune_ai; delete + prune-config are is_admin.
#
# SCOPE CARVE-OUT (W-MED, 2026-08-18 — a deliberate divergence, not a bug): the whole
# connector family uses the BASE `api` scope, NOT the `docs` scope. Unlike the document
# front-door routes (POST /api/documents, .../download — deps._require_doc_scope), these
# routes never call _require_doc_scope; access is gated purely by ROLE via _conn_role_block
# (can_tune_ai / is_admin) plus the org/customer ring. So ANY valid per-user key of the
# right ROLE does full connector CRUD — an `api`-only key included. This is the connector
# family's api-scope + role model (see _conn_role_block below), and it is intentional
# (per-install operators provision connectors with plain operator keys); do NOT read the
# `docs`-scope requirement of the document routes onto this family. Legacy shared
# connector tokens are refused even when bound to an org. Every credential/secret field
# is REFUSED with a
# clean 400 (secrets ride the R3 admin surface); the actual create/update WITH creds is
# Wave R3. Connections are customer-scoped and funnel through _conn_in_org / store.create's
# confinement so a foreign id is a 404.
# ============================================================================
#: Body keys that carry a SECRET. R2's connector routes are non-secret; any of these
#: is a clean 400 naming that secrets ride the admin surface (R3).
def _find_secret_fields(node, _seen=None) -> list:
    """Every secret-named key anywhere in a nested body -- top level OR under `config`
    or any nested dict/list. A shallow top-level scan let a credential ride in as
    `{"config": {"api_key": "..."}}` and be stored cleartext (adversarial review,
    MEDIUM); this recursion closes that. Returns the sorted distinct names found."""
    found = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in _CONNECTOR_SECRET_FIELDS:
                    found.add(k)
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return sorted(found)


def _conn_row(c) -> dict:
    secret = conn_store.dec_secret(c.secret_enc)
    return {
        "id": c.id,
        "kind": c.kind,
        "name": c.name,
        "status": c.status,
        "auth": conn_store.auth_mode(secret) if secret else "none",
        "project_id": c.project_id,
        "customer_id": getattr(c, "customer_id", None),
        "content_type_id": getattr(c, "content_type_id", None),
        # connected projects: the project whose language list this connection follows
        "languages_project_id": conn_store.languages_project_id(c.id),
    }
