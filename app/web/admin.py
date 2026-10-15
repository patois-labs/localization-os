"""Admin pages: users, security, audit, settings, and Setup AI.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports PROMPT_TAP_PAGE_ROWS, _parse_int_qp, _tap_query,
_tap_filter_detail, _looks_like_quota, _local_probe_key, _parse_custom_instructions,
_DEEPL_EDIT_CLEARABLE, _LARA_EDIT_CLEARABLE, _engine_clear_from_form, _org_context_card,
customer_service_list, _seg_redirect, AUDIT_PAGE_SIZE, audit_page, _engine_cfg_from_form,
_owned_profile_or_redirect from app.web.admin_common, app.web.admin_part2, app.web.admin_part3.

Documented in: docs/architecture/configuration.md
"""

import json
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import branding, config, features
from app.db import get_session
from app.kernel import (
    access,
    apikeys,
    audit,
    cloud_keys,
    engines,
    features_admin,
    gdpr,
    jev_inspector,
    languages_catalog,
    ledger_controls,
    mailer,
    mfa,
    model_warmup,
    prompt_tap,
    scim,
    security,
    settings,
    srx,
    tap_runs,
    tap_view,
)
from app.kernel import (
    segmentation_profiles as segprof,
)
from app.kernel import spend as _spend
from app.kernel.providers import (
    ProviderSpec,
    cloud_list_models,
    local_complete,
    local_list_models,
    local_list_models_result,
    provider_is_live,
    resolve_default_model,
    run_completion,
)
from app.models import Role, User
from app.web import one_time_stash
from app.web._forms import EnvelopeGuardForm
from app.web.admin_common import (
    _DEEPL_EDIT_CLEARABLE,
    _LARA_EDIT_CLEARABLE,
    PROMPT_TAP_PAGE_ROWS,
    _engine_clear_from_form,
    _local_probe_key,
    _looks_like_quota,
    _org_context_card,
    _parse_custom_instructions,
    _parse_int_qp,
    _tap_filter_detail,
    _tap_query,
    customer_service_list,
    download_audit_due,
    is_prefetch_request,
)
from app.web.admin_jev import can_inspect as _can_inspect_judges
from app.web.admin_jev import router as admin_jev_router
from app.web.admin_part2 import AUDIT_PAGE_SIZE, _seg_redirect, audit_page
from app.web.admin_part2 import router as admin_part2_router
from app.web.admin_part3 import _engine_cfg_from_form, _owned_profile_or_redirect
from app.web.deps import (
    _actor_email,
    _admin_block,
    _ai_block,
    _customer_block,
    _error_page,
    _ip,
    _my_customer_id,
    _my_org_id,
    _org_block,
    read_capped_upload,
    templates,
)
from app.web.form_errors import redirect_with_error
from app.web.streaming import StreamingResponse

__all__ = [
    "AUDIT_PAGE_SIZE",
    "PROMPT_TAP_PAGE_ROWS",
    "_DEEPL_EDIT_CLEARABLE",
    "_LARA_EDIT_CLEARABLE",
    "_engine_cfg_from_form",
    "_engine_clear_from_form",
    "_local_probe_key",
    "_looks_like_quota",
    "_my_customer_id",
    "_org_block",
    "_org_context_card",
    "_owned_profile_or_redirect",
    "_parse_custom_instructions",
    "_parse_int_qp",
    "_seg_redirect",
    "_tap_filter_detail",
    "_tap_query",
    "audit_page",
    "customer_service_list",
    "quote_plus",
]

router = APIRouter()


# --- Setup AI ---
@router.get("/setup-ai")
def setup_ai(request: Request):
    me, blocked = _ai_block(request)
    if blocked:
        return blocked
    # 2026-08-19: the page stays linguist-accessible (can_tune_ai), but the
    # install-wide AI SPEND meter (spent / cap / pct_used) is MANAGER+ only — a linguist
    # tunes AI without reading whole-install spend, the same asymmetry the cost report
    # already enforces. can_manage on the RESOLVED user (SYSTEM admin under auth-off ->
    # True; a real linguist -> False). Non-managers get a neutral stub card, no figures.
    can_see_spend = security.can_manage(me)
    cloud = [p for p in config.PROVIDERS if p != "local"]
    # Engine registry (P3): the profile cards + the Add-engine form. Scoped to the
    # caller's org when multi-org is on (no-op single-tenant). Include an
    # engine:<id> option in the default-provider picker so a project can default to
    # a registered engine.
    profiles = engines.list_profiles(org_id=_my_org_id(request))
    engine_rows = [engines.engine_row(p) for p in profiles]
    # Jev Train C: the judge Inspect buttons and the TYPESAFE_API_KEY import offer are
    # admin-only (the routes behind them are _admin_block), so a linguist sees neither.
    can_inspect_judges = _can_inspect_judges(me)
    # No auto-ping on load (keeps the page fast and avoids hitting the LAN server
    # every render) — the Fetch / Test buttons probe it on demand.
    return templates.TemplateResponse(
        request,
        "setup_ai.html",
        {
            "request": request,
            "providers": config.PROVIDERS,
            "cloud_providers": cloud,
            "live": {p: provider_is_live(p) for p in config.PROVIDERS},
            "engines": engine_rows,
            "engine_kinds": engines.KINDS,
            "engine_kind_labels": engines.KIND_LABELS,
            "engine_judge_kinds": sorted(engines.JUDGE_KINDS),
            "can_inspect_judges": can_inspect_judges,
            "jev_env_import": can_inspect_judges
            and jev_inspector.env_import_available(_my_org_id(request)),
            "engine_warning": request.query_params.get("engine_warning"),
            "engine_live": {
                r["provider_name"]: provider_is_live(r["provider_name"]) for r in engine_rows
            },
            "engine_saved": request.query_params.get("engine_saved"),
            "engine_error": request.query_params.get("engine_error"),
            "engine_test": request.query_params.get("engine_test"),
            "models": config.DEFAULT_MODELS,
            "default_provider": config.DEFAULT_PROVIDER,
            "env_keys": {
                "claude": "ANTHROPIC_API_KEY",
                "gemini": "GEMINI_API_KEY",
                "openai": "OPENAI_API_KEY",
            },
            # In-app cloud key management: per-provider {source, last4, stored, has_env}
            # — the masked fingerprint + source badge + Remove-availability the cloud
            # tiles render. NEVER carries the key itself (last4 only).
            "cloud_keys": cloud_keys.all_status(),
            "local": {
                "base_url": engines.display_base_url(config.LOCAL_BASE_URL),
                "key_status": settings.local_key_status(),
                "model": config.DEFAULT_MODELS.get("local", ""),
                # #342: the pinned sampling params the local card's form pre-fills.
                "temperature": settings.get("local_temperature"),
                "judge_temperature": settings.get("local_judge_temperature"),
                "seed": settings.get("local_seed") or "",
                # #341: model warm-up checkbox state.
                "model_warmup": model_warmup.enabled(),
            },
            "local_reachable": False,
            "local_models": [],
            "saved": request.query_params.get("saved"),
            # spend-cap meter (viz quick-win): meter_state() gives the render-ready
            # figure + fill/tick/pct-used percentages the DESIGN.md §5 meter needs,
            # on top of the same cap/spent/tripped fields this card always rendered.
            "spend": _spend.meter_state() if can_see_spend else None,
            "can_see_spend": can_see_spend,
            "assist_limits": {
                "minute": settings.get("assist_calls_per_minute"),
                "day": settings.get("assist_calls_per_day"),
                "editable": security.is_admin(me) and settings.get("multi_org_enabled") != "1",
            },
            "spend_error": request.query_params.get("spend_error"),
            # a11y T7: the engine and spend-cap refusals render in the page error summary
            # (base.html reads err; ?key_error= is the /setup-ai page alias).
            "err": request.query_params.get("engine_error")
            or request.query_params.get("spend_error")
            or request.query_params.get("allowance_error"),
            "mock_egress": {"allowed": settings.get("allow_mock_egress") == "1"},
            # TM matching / locks (PROPOSAL-tm-matching §6). GLOBAL interim policy, mirrored
            # from the CLI `config` tm group; the TM_LOCKS_ENABLED kill switch is ENV-ONLY
            # and is deliberately NOT surfaced here.
            "tm_matching": {
                "lock_ice": settings.get("tm_lock_ice") != "0",
                "hundreds_enter_lqa": settings.get("tm_hundreds_enter_lqa") != "0",
                "context_floor": settings.get("tm_context_floor") or "95",
                # Batch 15 T2: default ON (only an explicit "0" turns it off).
                "empty_delivery_gate": settings.get("tm_empty_delivery_gate") != "0",
            },
        },
    )


@router.post("/setup-ai")
def setup_ai_save(
    request: Request,
    default_provider: str = Form(...),
    model_claude: str = Form(""),
    model_gemini: str = Form(""),
    model_openai: str = Form(""),
):
    me, blocked = _ai_block(request)
    if blocked:
        return blocked
    # #561 gate: switching the INSTALL default to the offline mock is an admin decision
    # (it turns every translation into placeholder text for every user). A linguist may
    # still save this form while mock is already the default; they just cannot turn it on.
    if (
        (default_provider or "").strip().lower() == "mock"
        and (config.DEFAULT_PROVIDER or "").strip().lower() != "mock"
        and security.auth_required()
        and not security.is_admin(me)
    ):
        return _error_page(
            request, 403, "Only an administrator can make the offline mock the default engine."
        )
    # FIX 2a: canonicalize an engine:<id> ref to its exact "engine:<id>" shape
    # before it is persisted, so this write seam never PLANTS a non-canonical-
    # but-live ref (e.g. "engine:05") that engines.reference_count would then
    # have to reader-side match by parsed id (FIX 2b covers that regardless —
    # this is defense in depth at the one provider-string write seam in this
    # file). Non-engine values (built-in provider names) pass through unchanged.
    default_provider = engines.canonicalize_ref(default_provider)
    try:
        settings.validate_value("default_provider", default_provider)
    except settings.InvalidSettingValue as exc:
        return _error_page(request, 400, str(exc))
    settings.set_many(
        {
            "default_provider": default_provider,
            "model_claude": model_claude,
            "model_gemini": model_gemini,
            "model_openai": model_openai,
        }
    )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


def _ledger_web_gate(request):
    # The WEB ceremony's posture is the one every other destructive web admin control uses
    # (retention, segmentation): an admin SESSION + CSRF (global middleware) + the typed
    # phrase IS the ceremony. It does NOT consult `remote_admin_enabled` -- that flag is the
    # kill switch for the REMOTE /api door (kept on the /api twin via _r3_gate), not the
    # local session door; requiring it here made the primary control unreachable on a default
    # install and answered submits with a bare 404 (gate round-1 MED-2).
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # 2026-09-20: this used to require a super-admin under an active gate. That account no
    # longer exists, and the ONE thing that must not happen is this quietly becoming "any
    # org admin may drive install-wide controls" -- a privilege WIDENING hidden inside a
    # deletion. Install-wide power now requires host access, exactly like org lifecycle:
    # under an active gate the web ceremony refuses and names the CLI twin.
    #
    # STRICT NO-OP for every install today: multi_org_active() is False with the gate off,
    # so this branch is unreachable and the ceremony behaves byte-identically.
    if access.multi_org_active():
        return _error_page(
            request,
            403,
            "Install-wide Ledger controls are a host-operator action under multi-org. "
            "Run them from the CLI on the host: `ledger --help`.",
        )
    return None


def _ledger_controls_available(request) -> bool:
    """True when the caller may drive the web ceremony (so the card renders only then)."""
    return _ledger_web_gate(request) is None


@router.post("/prompt-tap/envelopes/{state}")
def ledger_envelopes(request: Request, state: str, confirm: str = Form(""), reason: str = Form("")):
    from app.kernel import ledger_controls

    blocked = _ledger_web_gate(request)
    if blocked:
        return blocked
    if state not in ("on", "off"):
        return _error_page(request, 404, "Not found.")
    try:
        ledger_controls.set_envelopes(
            state == "on",
            confirm=confirm,
            reason=reason,
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except ValueError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse("/prompt-tap", status_code=303)


@router.post("/prompt-tap/purge")
def ledger_purge(
    request: Request, before: str = Form(""), confirm: str = Form(""), apply: str = Form("")
):
    from app.kernel import ledger_controls

    blocked = _ledger_web_gate(request)
    if blocked:
        return blocked
    try:
        result = ledger_controls.purge(
            before=before,
            confirm=confirm,
            apply=apply == "1",
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except ValueError as exc:
        return _error_page(request, 400, str(exc))
    # LOW-1: answer the BROWSER form with the normal page flow (redirect + flash), not a bare
    # JSON document with no chrome. The /api twin keeps JSON.
    # gate round-2 LOW-3: carry the confirmation in a SIGNED, ONE-SHOT session value (the
    # Starlette SessionMiddleware cookie is itsdangerous-signed), not on the query string -- a
    # query string let anyone forge a "purge applied: N actions" banner with a crafted link to
    # an audited destructive-action confirmation. The GET pops it (one render only).
    request.session["ledger_purge_flash"] = {
        "applied": bool(result["applied"]),
        "cutoff": result["cutoff"],
        "actions": int(result["actions"]),
        "runs": int(result["runs"]),
        "raw_files": int(result["raw_files"]),
    }
    return RedirectResponse("/prompt-tap", status_code=303)


@router.get("/prompt-tap", summary="Ledger view", operation_id="ledger_view")
def prompt_tap_view(request: Request):
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    provider = request.query_params.get("provider") or None
    workflow_id = _parse_int_qp(request, "workflow_id")
    workflow_run_id = _parse_int_qp(request, "workflow_run_id")
    document_id = _parse_int_qp(request, "document_id")
    # ORG SCOPING (N3/F3/F4): the FLAT "All calls" section walks the legacy file AND every
    # per-action file, so under ACTIVE multi-org it would render every org's raw content to
    # any org-admin. Confine it to the caller's org, hiding unattributable legacy records
    # (fail closed). Single-tenant / gate off is byte-identical.
    _org_id = _my_org_id(request)
    _scoped = access.multi_org_active()
    # F4: an org-NULL caller under ACTIVE multi-org fails CLOSED everywhere on this page --
    # both the flat section AND the grouped section (list_actions(org_id=None) would mean
    # "unscoped", leaking every org). Not reachable via the web today (login 401s such
    # users), but the code must match the fail-closed claim.
    _fail_closed = _scoped and _org_id is None
    legacy_total = prompt_tap.count_legacy_records()
    if _scoped:
        # F3: the org filter is applied BEFORE the page cap (over-fetch with limit=0) so a
        # loud sibling org's volume cannot consume the cap and blank a quiet org's page --
        # and all counts derive from the PRE-CAP filtered set.
        if _fail_closed:
            records, matching, total = [], 0, 0
        else:
            all_recs, _m, _s = prompt_tap.page_snapshot(
                limit=0,
                provider=provider,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                document_id=document_id,
            )
            filtered = tap_view.filter_records_for_org(all_recs, _org_id)
            matching = total = len(filtered)
            records = filtered[:PROMPT_TAP_PAGE_ROWS]
        legacy_total = 0  # legacy is hidden under multi-org (fail closed)
    else:
        # ONE pass for EVERY number this page prints — rows, matching, stored. Splitting
        # them across passes is what let a clear() land in between and print "0 records
        # stored" above five rendered rows.
        records, matching, total = prompt_tap.page_snapshot(
            limit=PROMPT_TAP_PAGE_ROWS,
            provider=provider,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            document_id=document_id,
        )
    # The RUN-ENVELOPE grouped view (proposal §9): translate actions newest-first, each
    # with its language-pair runs and their nested calls -- the primary way to read the
    # tap now (a run's calls organized under the click they belong to, not a flat stream).
    a_project = _parse_int_qp(request, "action_project")
    a_document = _parse_int_qp(request, "action_document")
    a_batch = _parse_int_qp(request, "action_batch")
    a_status = request.query_params.get("action_status") or None
    a_since = request.query_params.get("action_since") or None
    a_until = request.query_params.get("action_until") or None
    a_failures = request.query_params.get("action_failures") in ("1", "on", "true")
    # ORG SCOPING (H2): the grouped-actions section is confined to the caller's org
    # whenever multi-org is active (TapAction.org_id now exists), because _admin_block is
    # NOT a tenant boundary -- an org-admin passes it. _my_org_id() is None (no scoping)
    # single-tenant / gate off, so this is byte-identical there. (The flat "All calls"
    # section's content-visibility posture is a separate pre-existing policy call, left
    # unchanged.)
    # Fail closed for an org-NULL caller under active multi-org (F4): an unscoped
    # list_actions(org_id=None) would leak every org's grouped actions.
    actions = (
        []
        if _fail_closed
        else tap_view.list_actions(
            project_id=a_project,
            document_id=a_document,
            batch_id=a_batch,
            status=a_status,
            since=a_since,
            until=a_until,
            has_failures=a_failures,
            org_id=_org_id,
            # The provider filter (e.g. a judge Inspect page's engine:<id> link) confines
            # the grouped actions and their calls too, not only the flat list (C5).
            provider=provider,
        )
    )
    return templates.TemplateResponse(
        request,
        "prompt_tap.html",
        {
            "request": request,
            "records": records,
            "enabled": prompt_tap.enabled(),
            "ledger_status": ledger_controls.status(
                org_id=_org_id, scoped=access.multi_org_active(), use_cache=True
            ),
            "ledger_controls_available": _ledger_controls_available(request),
            # gate round-2 LOW-3: the purge confirmation comes from a SIGNED, one-shot session
            # value (popped here so a refresh does not re-show it), never a forgeable query param.
            "purge_flash": request.session.pop("ledger_purge_flash", None),
            "total": total,
            "legacy_total": legacy_total,
            "run_capture_total": total - legacy_total,
            "response_cap": prompt_tap.RESPONSE_CAP,
            "actions": actions,
            "calls_cap": tap_view.CALLS_PER_RUN_CAP,
            "action_filters": {
                "project": a_project,
                "document": a_document,
                "batch": a_batch,
                "status": a_status or "",
                "since": a_since or "",
                "until": a_until or "",
                "failures": a_failures,
            },
            # The page shows at most this many rows and has no pagination, so a download
            # of the same filter can legitimately contain MORE rows. The copy must say so
            # (it used to claim "exactly what you see below").
            "page_rows": PROMPT_TAP_PAGE_ROWS,
            "filters": {
                "provider": provider or "",
                "workflow_id": workflow_id,
                "workflow_run_id": workflow_run_id,
                "document_id": document_id,
            },
            # Per-row download addresses: content keys, NOT row indexes (a capture
            # appended between render and click would re-point an index).
            "keys": [prompt_tap.record_key(r) for r in records],
            # The whole matching set, so the download control can say how many it will
            # hand over instead of implying the page's 200 rows are all there is.
            "matching": matching,
            "download_limit": prompt_tap.EXPORT_DEFAULT_LIMIT,
            "query": _tap_query(provider, workflow_id, workflow_run_id, document_id),
            "saved": request.query_params.get("saved"),
        },
    )


@router.get("/prompt-tap/download", summary="Ledger download", operation_id="ledger_download")
def prompt_tap_download(request: Request):
    """Download captured records for offline analysis — the maintainer's queue #1.

    ADMIN-ONLY, the same predicate as the viewer (`_admin_block`): the tap is a
    raw-content firehose across all customers, and a file on disk is the same content
    the page already renders in full, so this route must never be reachable by anyone
    who cannot read the page. Exposure is UNCHANGED by construction — it serves the
    stored record verbatim, and the capture contract keeps keys/secrets out of it.

    `?record=<key>` downloads ONE record pretty-printed; otherwise the current filter
    selection is streamed as newline-delimited JSON (`format=json` wraps it in a
    manifest envelope instead). An unrecognized `format` falls back to jsonl rather
    than erroring a download; a MALFORMED `?record=` is refused outright (a blank one
    used to mean "no key given", i.e. the whole store).

    MULTI-ORG (N3). `_admin_block` is not a tenant boundary — an org-admin passes it.
    The BATCH download (no `?record`) cannot be scoped per-item and stays REFUSED once
    `multi_org_active()` (fail closed, the repo law). A PER-RECORD download IS scopable now
    that per-action files carry `meta.action_id` -> TapAction.org_id: under active multi-org
    a record is served ONLY if its action belongs to the caller's org; a legacy /
    unattributable / cross-org record is refused (403, uniform with a missing record so it
    is not an existence oracle). Single-tenant (every install today) is byte-identical."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    if is_prefetch_request(request.headers):
        # #617: a browser prefetch is not a download. Deliver nothing (so nothing needs an audit
        # row, and a client-set header can never pull the ledger out unaudited).
        return Response(status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "60"})
    _org_id = _my_org_id(request)  # None single-tenant / gate off
    fmt = (request.query_params.get("format") or "jsonl").lower()
    if fmt not in prompt_tap.EXPORT_FORMATS:
        fmt = "jsonl"
    raw_key = request.query_params.get("record")
    if raw_key is not None and not prompt_tap.is_record_key(raw_key.strip()):
        # Present but malformed/blank: refuse. Falling through would serve the WHOLE
        # STORE to someone who asked for one record.
        return _error_page(request, 404, "No such capture (it may have rotated out).")
    key = raw_key.strip() if raw_key is not None else None
    if access.multi_org_active():
        # ACTIVE multi-org. F4: an org-NULL caller fails CLOSED (never the whole store) --
        # `_org_id is not None` alone would fall THROUGH to serving everything, contradicting
        # the fail-closed claim (unreachable via the web today, but the code must match).
        if _org_id is None:
            return _error_page(request, 403, "This download is not available to your organization.")
        # The batch (whole-selection) download cannot be per-item scoped -> refuse. A
        # per-record download is scoped to the caller's org.
        if not key:
            return _error_page(
                request,
                403,
                "The prompt-tap batch download is disabled "
                "under multi-org mode; download individual records instead.",
            )
        if not tap_view.record_visible_to_org(prompt_tap.find_record(key), _org_id):
            # cross-org / legacy / missing all look identical (no existence oracle).
            return _error_page(request, 403, "This capture is not available to your organization.")
    bundle = prompt_tap.export_bundle(
        fmt=fmt,
        key=key,
        provider=request.query_params.get("provider") or None,
        workflow_id=_parse_int_qp(request, "workflow_id"),
        workflow_run_id=_parse_int_qp(request, "workflow_run_id"),
        document_id=_parse_int_qp(request, "document_id"),
    )
    if key and not bundle["count"]:
        # The key names no stored record — rotation may have dropped it since the
        # page was rendered. Say so rather than serving an empty file.
        return _error_page(request, 404, "No such capture (it may have rotated out).")
    # Audit what ACTUALLY left the box, from inside the stream: logging up front
    # recorded a completed download for a client that disconnected before the first
    # byte (the same defect already fixed on the CLI). The `finally` runs on normal
    # end AND on GeneratorExit when the response closes early, so a partial transfer
    # — the case that matters — is still recorded, with its real record count.
    actor = _actor_email(request) or "system"

    def _audited_stream(lines, meta):
        sent = 0
        finished = False
        try:
            for chunk in lines:
                sent += 1
                yield chunk
            finished = True
        finally:
            # #617: one row per actor and selection per minute. The target is the selection
            # (format, record key, filters), so a different selection always gets its own row.
            # A PARTIAL transfer (client dropped early) always writes its row and never starts
            # the one-minute window, so a full retry is still recorded.
            # `finished` is True only when the generator ran to its end (json chunks are not one
            # per record, so a count comparison cannot tell a cut-off json download).
            complete = finished
            target = "%s|%s|%s" % (meta["fmt"], meta["key"], sorted(meta["filters"].items()))  # noqa: UP031
            if download_audit_due(actor, target, complete=complete):
                audit.log(
                    actor=actor,
                    action="prompt_tap.download",
                    detail="%s: %d of %d record%s delivered%s%s"  # noqa: UP031
                    % (
                        meta["fmt"],
                        sent if meta["fmt"] == "jsonl" else meta["count"],
                        meta["total"],
                        "" if meta["total"] == 1 else "s",
                        " (record {})".format(meta["key"]) if meta["key"] else "",
                        _tap_filter_detail(meta["filters"], key=meta["key"]),
                    ),
                )

    return StreamingResponse(
        _audited_stream(bundle["lines"], bundle),
        media_type=bundle["media_type"],
        headers={
            "Content-Disposition": 'attachment; filename="{}"'.format(bundle["filename"]),
            # Machine-readable honesty for a jsonl stream, which carries no
            # manifest of its own (the filename says it too).
            "X-Prompt-Tap-Count": str(bundle["count"]),
            "X-Prompt-Tap-Total": str(bundle["total"]),
            "X-Prompt-Tap-Truncated": "1" if bundle["truncated"] else "0",
        },
    )


@router.post("/prompt-tap/toggle", summary="Ledger toggle", operation_id="ledger_toggle")
def prompt_tap_toggle(request: Request, enabled: str = Form("")):
    # #538: capture is install-wide (one switch, one shared file), so under multi-org it is
    # a host-operator action exactly like the envelope and purge controls.
    blocked = _ledger_web_gate(request)
    if blocked:
        return blocked
    prompt_tap.set_enabled(enabled == "1", actor=_actor_email(request))
    return RedirectResponse("/prompt-tap?saved=1", status_code=303)


@router.post("/prompt-tap/clear", summary="Ledger clear", operation_id="ledger_clear")
def prompt_tap_clear(request: Request):
    # #538: clear truncates the one shared legacy capture file (no org filter).
    blocked = _ledger_web_gate(request)
    if blocked:
        return blocked
    n = prompt_tap.clear()
    audit.log(
        actor=_actor_email(request) or "system",
        action="prompt_tap.clear",
        detail=f"cleared {n} records",
    )
    return RedirectResponse("/prompt-tap?saved=1", status_code=303)


# --- ROADMAP #6: the RUN-first Ledger lens (web) ------------------------------
@router.get("/prompt-tap/runs", summary="Ledger runs", operation_id="ledger_runs")
def prompt_tap_runs(request: Request):
    """List translation RUNS newest-first (the run-first lens: one row per run, grouped from
    each call's own run id). Admin-only (D8), org-scoped whenever multi-org is active -- an
    org-NULL caller under active multi-org fails CLOSED (mirrors the grouped-actions section)."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    _org_id = _my_org_id(request)
    _fail_closed = access.multi_org_active() and _org_id is None
    since = request.query_params.get("since") or None
    until = request.query_params.get("until") or None
    failures = request.query_params.get("failures") in ("1", "on", "true")
    runs = (
        []
        if _fail_closed
        else tap_runs.list_runs(
            limit=PROMPT_TAP_PAGE_ROWS,
            since=since,
            until=until,
            has_failures=failures,
            org_id=_org_id,
        )
    )
    return templates.TemplateResponse(
        request,
        "prompt_tap_runs.html",
        {
            "request": request,
            "enabled": prompt_tap.enabled(),
            "ledger_status": ledger_controls.status(
                org_id=_org_id, scoped=access.multi_org_active(), use_cache=True
            ),
            "runs": runs,
            "run_filters": {"since": since or "", "until": until or "", "failures": failures},
        },
    )


@router.get(
    "/prompt-tap/runs/{run_id}", summary="Ledger run detail", operation_id="ledger_run_detail"
)
def prompt_tap_run_detail(run_id: str, request: Request):
    """One run's calls grouped by step then unit. Admin-only + org-confined. `?bodies=1`
    shows the full prompt/response payloads -- operator-gated exactly like the flat 'All
    calls' section already renders bodies to an admin on this host-served page; without it
    the view is the bodies-withheld digest (lengths + sha256 + head). A foreign/unknown run
    is a non-enumerable 404."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    _org_id = _my_org_id(request)
    if access.multi_org_active() and _org_id is None:
        return _error_page(request, 404, "Not found.")  # fail closed for an org-NULL caller
    bodies = request.query_params.get("bodies") in ("1", "on", "true")
    detail = tap_runs.run_detail(
        (run_id or "").strip(),
        bodies=bodies,
        include_keys=True,
        org_id=_org_id,
    )
    if detail is None:
        return _error_page(request, 404, "Not found.")
    return templates.TemplateResponse(
        request,
        "prompt_tap_run.html",
        {
            "request": request,
            "enabled": prompt_tap.enabled(),
            "ledger_status": ledger_controls.status(
                org_id=_org_id, scoped=access.multi_org_active(), use_cache=True
            ),
            "run": detail,
            "bodies": bodies,
            "response_cap": prompt_tap.RESPONSE_CAP,
        },
    )


# --- in-app cloud API keys (Claude/Gemini/OpenAI) ---------------------------
@router.post("/setup-ai/keys/{provider}")
def setup_ai_key_set(provider: str, request: Request, api_key: str = Form("")):
    """Set / replace a cloud provider's API key (stored Fernet-encrypted, overrides
    .env, live immediately). ADMIN-ONLY (_admin_block, stricter than the rest of the
    Setup AI cards): pasting a key is what ENABLES real spend on a keyless install,
    the same class of act as raising the spend cap — which a linguist must not do.
    The key is trimmed of whitespace/surrounding quotes and NEVER echoed — the audit
    records the last4 only. An empty submit is refused (never blanks the stored key).

    The full key never appears in any response: on success we 303-redirect (no body),
    and the tile input always renders empty."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    if not cloud_keys.is_cloud_provider(provider):
        return RedirectResponse(
            "/setup-ai?key_error=" + quote("Unknown cloud provider."), status_code=303
        )
    try:
        cloud_keys.set_key(provider, api_key, actor=getattr(me, "email", "system"), ip=_ip(request))
    except ValueError:
        return RedirectResponse(
            "/setup-ai?key_error=" + quote("Enter a key to save."), status_code=303
        )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


@router.post("/setup-ai/keys/{provider}/delete")
def setup_ai_key_delete(provider: str, request: Request):
    """Remove an app-stored cloud key (falls back to the .env key if present, else
    keyless). Gated like the setter (admin-only); takes effect immediately."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    if not cloud_keys.is_cloud_provider(provider):
        return RedirectResponse(
            "/setup-ai?key_error=" + quote("Unknown cloud provider."), status_code=303
        )
    cloud_keys.remove_key(provider, actor=getattr(me, "email", "system"), ip=_ip(request))
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


@router.post("/setup-ai/spend")
def setup_ai_spend_save(request: Request, spend_cap_usd: str = Form("0")):
    """Save the global spend-cap kill switch. A non-negative float ("0" = off).
    Its own audited action (a safety-posture change), separate from the defaults
    form. spend_period stays "month" (fixed) — not exposed as a form field.

    ADMIN-GATED (_admin_block, not _ai_block like the rest of the Setup AI cards) —
    the mock-egress-switch precedent, and the write half of the maintainer's 2026-08-19
    spend-disclosure decision. The GET above now hides the meter + this form from a
    non-manager (can_see_spend), so of the roles that reach /setup-ai at all only an
    ADMIN can see the cap; leaving the SAVE on _ai_block would have let a linguist who
    can no longer READ the figure still SET the install-wide ceiling by direct POST
    (probe-proven: cap 137.5 -> 999999). Raising/clearing the cap is the same class of
    act as pasting a cloud key or disarming the mock-egress guard — both already
    admin-only here — so the write rung matches the read rung. Refused attempts write
    NO audit row (house convention: the audit below is reached only past the gate).
    The trusted local operator is unaffected (`config set spend_cap_usd`), and
    _admin_block is a no-op while the login wall is off, so single-tenant behaviour is
    byte-identical.

    task_cca6bb89 (huge-value hardening): an absurdly large/long numeric string
    (e.g. a stray extra zero, or a 300-digit typo) doesn't raise from float() --
    it silently saturates to `inf`, which used to be accepted and stored verbatim
    as a nonsensical "$inf" cap with no error. That specific case (parses fine,
    but is non-finite or past the sane MAX_SPEND_CAP_USD ceiling) now gets a
    clean redirect-banner rejection instead. Since a11y T11, negative and
    non-numeric input is refused the same way (it used to be coerced to 0/off);
    verify_spend_cap.py covers every door."""
    from urllib.parse import quote

    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # T11 (HECVAT APPL-09): ONE parser for every door (settings.spend_cap_value, also the
    # registered validator behind `config set` and /api/admin/config). A negative or
    # non-numeric value is refused with the page's error summary (it used to be saved as
    # 0, silently turning the cap off); blank still means no cap. Nothing is written.
    try:
        value = settings.spend_cap_value(spend_cap_usd)
    except settings.InvalidSettingValue as exc:
        return RedirectResponse("/setup-ai?spend_error=" + quote(str(exc)), status_code=303)
    settings.set_many({"spend_cap_usd": value, "spend_period": "month"})
    audit.log(
        actor=getattr(me, "email", "system"),
        action="spend.cap_set",
        detail=f"spend_cap_usd={value}",
        ip=_ip(request),
    )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


@router.post("/setup-ai/assist-limits")
def setup_ai_assist_limits_save(
    request: Request,
    assist_calls_per_minute: str = Form("6"),
    assist_calls_per_day: str = Form("60"),
):
    """Admin-only allowance settings; the shared settings guard protects tenant doors."""
    from urllib.parse import quote

    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        settings.set_many(
            {
                "assist_calls_per_minute": assist_calls_per_minute,
                "assist_calls_per_day": assist_calls_per_day,
            }
        )
    except settings.InvalidSettingValue as exc:
        return RedirectResponse("/setup-ai?allowance_error=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=getattr(me, "email", "system"),
        action="assist.allowance_settings",
        detail="Updated Ask and Draft call limits",
        ip=_ip(request),
    )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


@router.post("/setup-ai/mock-egress")
def setup_ai_mock_egress_save(request: Request, allow_mock_egress: str = Form("")):
    """Save the mock-egress guard override (default OFF = guard ON). An
    unchecked-checkbox form field (absent => off), same convention as
    retention_enabled. Its own audited action (a safety-posture change): with it ON,
    offline/Mock MT output CAN be written to real external systems by the connector
    flows — the opposite of the default-safe posture — so it is logged separately.

    ADMIN-GATED (not _ai_block like the rest of the Setup AI cards): this is a
    "disable the safety guard" switch, so it deserves the admin rung — a linguist
    (who CAN tune AI) must not be able to disarm it, and _ai_block's can_tune_ai is a
    no-op when security_auth_required=0 (the default), which would otherwise let any
    unauthenticated caller POST this and turn the guard off on a fresh install. The
    READ/display of the setting on the page stays under _ai_block; only this SAVE is
    admin-only."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    on = "1" if allow_mock_egress == "1" else "0"
    settings.set_many({"allow_mock_egress": on})
    audit.log(
        actor=getattr(me, "email", "system"),
        action="connectors.allow_mock_egress",
        detail=f"allow_mock_egress={on}",
        ip=_ip(request),
    )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


# --- TM matching / locks policy (PROPOSAL-tm-matching §6, GLOBAL interim) ---
@router.post("/setup-ai/tm")
def setup_ai_tm_save(
    request: Request,
    tm_lock_ice: str = Form(""),
    tm_hundreds_enter_lqa: str = Form(""),
    tm_context_floor: str = Form("95"),
    tm_empty_delivery_gate: str = Form(""),
):
    """Save the TM-matching policy card (the three §6 controls, GLOBAL interim, + the
    Batch 15 TM-only delivery empty gate). The booleans are unchecked-checkbox fields
    (absent => off), same convention as the semantic card. The kill switch
    (TM_LOCKS_ENABLED) is ENV-ONLY and is not here."""
    me, blocked = _ai_block(request)
    if blocked:
        return blocked
    try:
        floor = int(tm_context_floor or "95")
    except (TypeError, ValueError):
        floor = 95
    floor = max(50, min(99, floor))
    lock_ice = "1" if tm_lock_ice == "1" else "0"
    hundreds = "1" if tm_hundreds_enter_lqa == "1" else "0"
    # Batch 15 T2: the empty-delivery gate is an unchecked-checkbox boolean like the others.
    empty_gate = "1" if tm_empty_delivery_gate == "1" else "0"
    settings.set_many(
        {
            "tm_lock_ice": lock_ice,
            "tm_hundreds_enter_lqa": hundreds,
            "tm_context_floor": str(floor),
            "tm_empty_delivery_gate": empty_gate,
        }
    )
    audit.log(
        actor=getattr(me, "email", "system"),
        action="tm_locks.settings",
        detail=(
            f"lock_ice={lock_ice} hundreds_enter_lqa={hundreds} floor={floor} "
            f"empty_delivery_gate={empty_gate}"
        ),
        ip=_ip(request),
    )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


@router.post("/setup-ai/test")
def setup_ai_test(request: Request, provider: str = Form(...)):
    """Two-stage cloud connectivity Test.

    STAGE 1 (free): cloud_list_models(provider) — a no-token metadata call that only
    succeeds with a VALID key. STAGE 2: a one-shot real completion. Splitting them
    lets the UI distinguish "key is invalid/missing" (stage 1 empty) from "key is
    valid but the completion failed" (stage 2 error) — and, when the stage-2 error
    carries a quota marker, say plainly the account is simply out of credit.

    A KEYLESS install (provider not live) skips both stages and runs the deterministic
    OFFLINE MOCK, preserving the historical shape ({text, live: False}) so the tile
    still demonstrates the mock translating."""
    blocked = _ai_block(request)[1]
    if blocked:
        return blocked
    live = provider_is_live(provider)
    spec = ProviderSpec(provider=provider, model=config.DEFAULT_MODELS.get(provider))
    _prompt = "Translate to French: Hello world"
    _sys = "Output only the translation."
    _payload = {"text": "Hello world", "target": "fr-FR", "glossary": []}
    # Keyless / offline: the mock. Keep the legacy {text, live} shape (verify_ui).
    if not live:
        res = run_completion(spec, _prompt, system=_sys, task="translate", payload=_payload)
        return JSONResponse(
            {
                "provider": provider,
                "live": False,
                "ok": True,
                "models_visible": 0,
                "used": res["provider_used"],
                "text": res["text"],
                "message": f"mock -> {res['text']}",
            }
        )
    # STAGE 1 — free model-list probe. Empty => the key is missing or invalid.
    models = cloud_list_models(provider)
    if not models:
        return JSONResponse(
            {
                "provider": provider,
                "live": True,
                "ok": False,
                "stage": "list",
                "models_visible": 0,
                "used": None,
                "text": "",
                "message": "API key missing or invalid - could not list models.",
            }
        )
    # STAGE 2 — one real completion. Success reports as before (now with model count).
    res = run_completion(spec, _prompt, system=_sys, task="translate", payload=_payload)
    if not res["error"] and res["provider_used"]:
        return JSONResponse(
            {
                "provider": provider,
                "live": True,
                "ok": True,
                "stage": "complete",
                "models_visible": len(models),
                "used": res["provider_used"],
                "text": res["text"],
                "message": f"Connection OK ({len(models)} models visible) - {res['text']}",
            }
        )
    # Stage 1 passed but the completion failed: the KEY IS VALID — say so, and flag a
    # quota/no-credit cause distinctly when the error text carries the marker.
    err = res["error"] or "unknown error"
    quota = _looks_like_quota(err)
    detail = "account has no credit / quota exhausted" if quota else err
    return JSONResponse(
        {
            "provider": provider,
            "live": True,
            "ok": False,
            "stage": "complete",
            "key_valid": True,
            "quota": quota,
            "models_visible": len(models),
            "used": None,
            "text": "",
            "error": err,
            "message": (
                f"Connection OK (key valid, {len(models)} models visible) "
                f"- completion failed: {detail}"
            ),
        }
    )


# --- local / self-hosted engine (LM Studio, Ollama, vLLM) ---
@router.post("/setup-ai/local")
def setup_ai_local_save(
    request: Request,
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_key: bool = Form(False),
    local_model: str = Form(""),
    # #342: pinned sampling for every local-engine call (temperature always sent,
    # seed only when non-blank). A blank TEMPERATURE field keeps the saved value
    # (a form without the fields still saves); a blank SEED is a valid value in
    # its own right (sends no seed) but a post from a form WITHOUT the sampling fields
    # (no `local_sampling_form` marker; FastAPI reads a blank optional field as absent, so
    # the marker is what tells "blank" from "omitted") keeps the saved one. Non-blank
    # invalid values are refused at the door with a clear message.
    local_temperature: str = Form(""),
    local_judge_temperature: str = Form(""),
    local_seed: str = Form(""),
    local_sampling_form: str = Form(""),
    # #341: model warm-up checkbox. An unchecked box posts nothing, so (like the seed) the
    # `local_sampling_form` marker is what makes "absent" mean off; without the marker an
    # old client leaves the saved value alone.
    local_model_warmup: str = Form(""),
):
    # #375: even a "local" URL can register an outside processor.
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    # #342 review: a post that OMITS the sampling fields (an older client, a script) keeps
    # the saved seed; only the real card form (marker present) lets a blank seed clear it.
    sampling = {"local_seed": local_seed.strip()} if local_sampling_form else {}
    if local_sampling_form:
        sampling["local_model_warmup"] = "1" if local_model_warmup.strip() else "0"
    try:
        settings.validate_value("local_base_url", base_url.strip())
        if "local_seed" in sampling:
            settings.validate_value("local_seed", sampling["local_seed"])
        temp = local_temperature.strip()
        if temp:
            settings.validate_value("local_temperature", temp)
            sampling["local_temperature"] = temp
        judge_temp = local_judge_temperature.strip()
        if judge_temp:
            settings.validate_value("local_judge_temperature", judge_temp)
            sampling["local_judge_temperature"] = judge_temp
    except settings.InvalidSettingValue as exc:
        return RedirectResponse("/setup-ai?key_error=" + quote(str(exc)), status_code=303)
    settings.set_many(
        {
            "local_base_url": base_url.strip(),
            **(
                {"local_api_key": ""}
                if clear_key
                else {"local_api_key": api_key.strip()}
                if api_key.strip()
                else {}
            ),
            "local_model": local_model.strip(),
            **sampling,
        },
        # #218: set_many clears the saved key when the URL moves to another origin
        # (unless this form supplied one) and audits the move under this actor.
        actor=getattr(me, "email", "") or "setup-ai",
    )
    return RedirectResponse("/setup-ai?saved=1", status_code=303)


@router.post("/setup-ai/local/models")
def setup_ai_local_models(
    request: Request, base_url: str = Form(""), api_key: str = Form(""), quick: str = Form("")
):
    """Fetch models loaded on the (possibly unsaved) local server. ``quick=1`` is the
    automatic on-load probe (short connect budget, #564 m11); the Fetch button omits it."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    return JSONResponse(
        local_list_models_result(
            base_url, _local_probe_key(base_url, api_key), quick=quick.strip() == "1"
        )
    )


@router.post("/setup-ai/cloud/models")
def setup_ai_cloud_models(request: Request, provider: str = Form(...), api_key: str = Form("")):
    """List the models a cloud provider's key (configured, or an unsaved override
    supplied here) can access — powers the Setup AI cloud-model dropdowns. Free
    metadata call; [] on any error (never 500). Gated exactly like the local route.

    SECURITY: NO base_url override is accepted here (unlike the local route). The
    configured cloud key is only ever sent to the provider's official host — a
    caller-supplied host could exfiltrate a real-money key. cloud_list_models also
    ignores base_url defensively, so this is belt-and-suspenders."""
    # Linguists may list models with configured credentials, but cannot supply
    # an independent provider key through this probe.
    blocked = (_admin_block(request) if api_key.strip() else _ai_block(request))[1]
    if blocked:
        return blocked
    return JSONResponse({"models": cloud_list_models(provider, api_key or None)})


@router.post("/setup-ai/local/test")
def setup_ai_local_test(
    request: Request,
    base_url: str = Form(""),
    api_key: str = Form(""),
    local_model: str = Form(""),
):
    """Blank-model handling: a blank field must never reach local_complete's raw
    literal-"local-model" fallback (LM Studio then rejects it with a confusing
    model_not_found error). But blank does NOT always mean unresolvable —
    providers.resolve_default_model is consulted first (consistent with every
    other completion seam), and when it can't resolve one (which is always true
    for a "local" ref by design: an engine's OWN model is the only correct
    default, never an arbitrary built-in one — see its docstring), a REACHABLE
    server that reports loaded models still gets tested against its first listed
    model instead of being blocked outright. Only the truly-unresolvable case
    (unreachable server / no loaded models) short-circuits with the friendly
    message — the real connectivity test is never weakened."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    if not base_url.strip():
        return JSONResponse(
            {
                "ok": False,
                "error": "Local engine is not configured - enter the server base URL first.",
            }
        )
    try:
        settings.validate_value("local_base_url", base_url)
    except settings.InvalidSettingValue as exc:
        return JSONResponse({"ok": False, "error": str(exc), "blocked": True})
    api_key = _local_probe_key(base_url, api_key)
    model = (local_model or "").strip()
    if not model:
        model = resolve_default_model("local") or ""
        if not model:
            listed = local_list_models(base_url, api_key)
            model = listed[0] if listed else ""
        if not model:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "No model selected — pick one from the model list "
                    "above (or load one in LM Studio) and save first.",
                }
            )
    try:
        text = local_complete(
            base_url,
            api_key,
            model,
            "Translate to French, output only the translation: Hello world",
            system="You are a translator. Output only the translation.",
        )
        return JSONResponse({"ok": True, "text": text})
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        if api_key:
            detail = detail.replace(api_key, "[redacted]")
        return JSONResponse({"ok": False, "error": detail})


@router.post("/setup-ai/engines")
def setup_ai_engine_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    formality: str = Form(""),
    region: str = Form(""),
    model_type: str = Form(""),
    context: str = Form(""),
    custom_instructions: str = Form(""),
    access_key_secret: str = Form(""),
    style: str = Form(""),
    instructions: str = Form(""),
    adapt_to: str = Form(""),
    glossaries: str = Form(""),
    confidence_floor: str = Form(""),
    meaning_floor: str = Form(""),
    timeout_s: str = Form(""),
    max_parallel_jobs: str = Form(""),
):
    """Create an EngineProfile from the 'Add engine' form. The API key is stored
    Fernet-encrypted (engines.create_profile). Own audited action. A quality-judge
    warning (engines.profile_warnings: a networked jev_local with no key) rides back
    on the redirect as a banner."""
    # #375: registering providers/credentials is an administrator action.
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    try:
        prof = engines.create_profile(
            name,
            kind,
            base_url=base_url,
            api_key=api_key,
            model=model,
            cfg=_engine_cfg_from_form(
                kind,
                formality,
                region,
                model_type,
                context,
                custom_instructions,
                access_key_secret=access_key_secret,
                style=style,
                instructions=instructions,
                adapt_to=adapt_to,
                glossaries=glossaries,
                confidence_floor=confidence_floor,
                meaning_floor=meaning_floor,
                timeout_s=timeout_s,
                max_parallel_jobs=max_parallel_jobs,
            ),
            actor=getattr(me, "email", "system"),
            org_id=_my_org_id(request),
        )
    except ValueError as e:
        return RedirectResponse("/setup-ai?engine_error=" + quote(str(e)), status_code=303)
    audit.log(
        actor=getattr(me, "email", "system"),
        action="engine.create",
        target=f"engine:{prof.id}",
        detail=f"kind={prof.kind} name={prof.name}",
        ip=_ip(request),
    )
    warning = " ".join(engines.profile_warnings(prof))
    suffix = ("&engine_warning=" + quote(warning)) if warning else ""
    return RedirectResponse("/setup-ai?engine_saved=1" + suffix, status_code=303)


@router.post("/setup-ai/engines/{eid}")
def setup_ai_engine_update(
    eid: int,
    request: Request,
    name: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: str = Form(""),
    clear_access_key_secret: str = Form(""),
    formality: str = Form(""),
    region: str = Form(""),
    enabled: str = Form(""),
    model_type: str = Form(""),
    context: str = Form(""),
    custom_instructions: str = Form(""),
    access_key_secret: str = Form(""),
    style: str = Form(""),
    instructions: str = Form(""),
    adapt_to: str = Form(""),
    glossaries: str = Form(""),
    confidence_floor: str = Form(""),
    meaning_floor: str = Form(""),
    timeout_s: str = Form(""),
    max_parallel_jobs: str = Form(""),
):
    """Update an EngineProfile (org-scoped). A blank api_key never blanks a stored
    key (masked pattern); enabled is an unchecked-checkbox field (absent => off)."""
    me, blocked = _ai_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    prof = engines.get_profile(eid)
    org = _my_org_id(request)
    if prof is None or (org is not None and prof.org_id != org):
        return RedirectResponse(
            "/setup-ai?engine_error=" + quote("No such engine."), status_code=303
        )
    # Check ownership before the administrator gate. Existing profiles can be
    # enabled or repointed, so updates require the same approval as creation.
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        engines.update_profile(
            eid,
            name=name or None,
            base_url=base_url,
            model=model,
            api_key=api_key or None,
            clear_api_key=(clear_api_key == "1"),
            clear_access_key_secret=(clear_access_key_secret == "1"),  # noqa: S105 -- form checkbox flag
            cfg=_engine_cfg_from_form(
                prof.kind,
                formality,
                region,
                model_type,
                context,
                custom_instructions,
                access_key_secret=access_key_secret,
                style=style,
                instructions=instructions,
                adapt_to=adapt_to,
                glossaries=glossaries,
                confidence_floor=confidence_floor,
                meaning_floor=meaning_floor,
                timeout_s=timeout_s,
                max_parallel_jobs=max_parallel_jobs,
            )
            or None,
            clear=_engine_clear_from_form(
                prof.kind,
                model_type,
                context,
                custom_instructions,
                style=style,
                instructions=instructions,
                adapt_to=adapt_to,
                glossaries=glossaries,
                confidence_floor=confidence_floor,
                meaning_floor=meaning_floor,
                timeout_s=timeout_s,
                max_parallel_jobs=max_parallel_jobs,
            )
            or None,
            enabled=(enabled == "1"),
            actor=getattr(me, "email", "system"),
        )
    except ValueError as e:
        return RedirectResponse("/setup-ai?engine_error=" + quote(str(e)), status_code=303)
    audit.log(
        actor=getattr(me, "email", "system"),
        action="engine.update",
        target=f"engine:{eid}",
        detail=("API key removed; " if clear_api_key == "1" else "")
        + ("access key secret removed" if clear_access_key_secret == "1" else ""),  # noqa: S105 -- form checkbox flag
        ip=_ip(request),
    )
    after = engines.get_profile(eid)
    notes = engines.profile_warnings(after)
    if engines.credentials_dropped(
        prof,
        after,
        clear_key=(clear_api_key == "1"),
        clear_secret=(clear_access_key_secret == "1"),  # noqa: S105 -- form checkbox flag
    ):
        notes = [engines.DESTINATION_CLEARED_NOTE, *notes]
    warning = " ".join(notes)
    suffix = ("&engine_warning=" + quote(warning)) if warning else ""
    return RedirectResponse("/setup-ai?engine_saved=1" + suffix, status_code=303)


@router.post("/setup-ai/engines/{eid}/delete")
def setup_ai_engine_delete(eid: int, request: Request):
    # The maintainer's locked RBAC decision: every NEW/changed DELETE is strictly admin
    # (rename keeps the family gate; delete tightens to _admin_block). Engine
    # delete now also FAILS CLOSED in the service when the engine is still
    # referenced as engine:<id> — surfaced here as a clean banner, never a 500.
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    prof = engines.get_profile(eid)
    org = _my_org_id(request)
    if prof is not None and (org is None or prof.org_id == org):
        try:
            engines.delete_profile(eid)
        except engines.EngineInUseError as exc:
            return RedirectResponse("/setup-ai?engine_error=" + quote(str(exc)), status_code=303)
        audit.log(
            actor=getattr(me, "email", "system"),
            action="engine.delete",
            target=f"engine:{eid}",
            detail=f"kind={prof.kind}",
            ip=_ip(request),
        )
    return RedirectResponse("/setup-ai?engine_saved=1", status_code=303)


@router.post("/setup-ai/engines/{eid}/test")
def setup_ai_engine_test(eid: int, request: Request):
    """Per-row Test connection: the lightweight per-kind probe (JSON for the button).
    Never translates real content; live network only (no MockTransport in the web)."""
    blocked = _ai_block(request)[1]
    if blocked:
        return blocked
    prof = engines.get_profile(eid)
    org = _my_org_id(request)
    if prof is None or (org is not None and prof.org_id != org):
        return JSONResponse({"ok": False, "error": "No such engine."}, status_code=404)
    ok, msg = engines.test_profile(eid)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/setup-ai/engines/{eid}/models")
def setup_ai_engine_models(
    eid: int, request: Request, base_url: str = Form(""), api_key: str = Form("")
):
    """#346: Fetch models for an EXISTING engine. A blank key on the saved origin uses
    the engine's stored key (server-side; never sent to the browser or another origin)."""
    blocked = _ai_block(request)[1]
    if blocked:
        return blocked
    prof = engines.get_profile(eid)
    org = _my_org_id(request)
    if prof is None or (org is not None and prof.org_id != org):
        return JSONResponse({"ok": False, "error": "No such engine."}, status_code=404)
    # Unsaved destinations and credentials require administrator approval too.
    if base_url.strip() or api_key.strip():
        blocked = _admin_block(request)[1]
        if blocked:
            return blocked
    return JSONResponse(engines.list_models(eid, base_url, api_key))


# --- Settings ---
@router.get("/settings")
def settings_page(request: Request):
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    custom_srx = srx.custom_srx_path()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "values": settings.all_values(),
            "providers": config.PROVIDERS,
            "db_path": str(config.DB_PATH),
            "saved": request.query_params.get("saved"),
            "custom_srx_present": custom_srx.exists(),
            # #424: under multi-org the install-wide SRX ruleset is host-only, so the form
            # is not offered (the POST answers 403). Always False with the gate off.
            "srx_host_only": bool(access.install_wide_security_refused(_SRX_KEYS)),
            "srx_msg": request.query_params.get("srx_msg"),
            "srx_error": request.query_params.get("srx_error"),
            # Segmentation profiles (P6, item 8): the manageable named-ruleset list
            # (org-scoped under the multi-org gate; None = single-tenant = all).
            "seg_profiles": [
                {"row": p, "use": segprof.in_use_count(p.id)}
                for p in segprof.list_profiles(org_id=_my_org_id(request))
            ],
            "seg_customers": customer_service_list(),
            # SPEC-context-metadata phase 1: the org-default context card (all-NULL scope).
            "org_context_card": _org_context_card(request),
            # Central feature-flag admin: one card row per toggleable feature.
            "feature_cards": features.ui_cards(),
        },
    )


@router.post("/settings/context-default")
def settings_context_default_save(
    request: Request,
    domain: str = Form(""),
    content_kind: str = Form(""),
    audience: str = Form(""),
    formality: str = Form(""),
    tone: str = Form(""),
    notes: str = Form(""),
    char_limit: str = Form(""),
):
    """Save the org-default context card (SPEC-context-metadata phase 1). Admin-only,
    matching the rest of /settings."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    from urllib.parse import quote

    from sqlmodel import Session

    from app.db import engine as _engine
    from app.kernel import context_pkg

    raw = {
        k: v
        for k, v in (
            ("domain", domain),
            ("content_kind", content_kind),
            ("audience", audience),
            ("formality", formality),
            ("notes", notes),
        )
        if (v or "").strip()
    }
    if (tone or "").strip():
        raw["tone"] = [t.strip() for t in tone.split(",") if t.strip()]
    if (char_limit or "").strip():
        raw["char_limit"] = char_limit
    with Session(_engine) as s:
        try:
            context_pkg.upsert_profile(
                s, org_id=_my_org_id(request), context_raw=raw, actor=_actor_email(request)
            )
        except context_pkg.ContextError as exc:
            return RedirectResponse("/settings?srx_error=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings")
def settings_save(
    request: Request,
    org_name: str = Form(...),
    default_source_locale: str = Form("en-US"),
    default_provider: str = Form("claude"),
    brand_name: str = Form(""),
    brand_llmemory: str = Form(""),
    brand_ledger: str = Form(""),
    glossary_prompt_token_cap: str = Form("1500"),
    prompt_tap_envelopes_enabled: EnvelopeGuardForm = None,
):
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    # None is absent; even a supplied empty value must use the guarded ceremony.
    # The async form dependency only reads the cached form; this handler and its
    # blocking settings writes remain synchronous and run in the anyio pool.
    if prompt_tap_envelopes_enabled is not None:
        return _error_page(request, 400, "Use the guarded ledger envelopes on/off ceremony.")
    try:
        settings.validate_value("default_provider", default_provider)
    except settings.InvalidSettingValue as exc:
        return _error_page(request, 400, str(exc))
    # Terminology T3: the install-wide default token ceiling for a step's rendered
    # concept-glossary block (LAW-TERM-13). Clamp junk/negative to the registry default
    # (a step's own glossary_token_cap=0 then inherits this value).
    try:
        _gcap = max(0, int(str(glossary_prompt_token_cap).strip() or "1500"))
    except (TypeError, ValueError):
        _gcap = int(settings.DEFAULTS["glossary_prompt_token_cap"])
    settings.set_many(
        {
            "glossary_prompt_token_cap": str(_gcap),
            # Blank = "not configured": app.branding.org_name() then shows the
            # product brand as the placeholder, so nothing hardcodes a name here.
            "org_name": org_name.strip(),
            "default_source_locale": default_source_locale.strip() or "en-US",
            "default_provider": default_provider,
            # Each blank falls back to its registry default at read time (the
            # accessor also caps length), so an empty submit restores the default.
            "brand_name": brand_name.strip(),
            "brand_llmemory": brand_llmemory.strip(),
            "brand_ledger": brand_ledger.strip(),
        }
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/features/{slug}")
def settings_feature_toggle(request: Request, slug: str, enabled: str = Form("")):
    """Toggle one feature flag from the Settings > Features card. Admin-only (its own
    audited action, like retention). A THIN CALLER of app.kernel.features_admin, the seam
    the CLI and /api/admin/features write through too, so the three surfaces cannot drift.
    The write rides settings.set_many -- audited AND version-bumped, so a running server
    picks the flip up within ~2s with no restart (the K3 cache-version property).
    Semantics + the disable confirm live in the card.

    The refusal ORDER is load-bearing and unchanged: unknown slug 404s, a door-moved
    feature (SCIM/SAML, whose enablement lives on /settings/enterprise behind the
    operator step-up) REDIRECTS -- both BEFORE the "0"/"1" form check, so a bad `enabled`
    on a door-moved slug still redirects rather than 400ing. resolve() answers the first
    two; the form-shape check stays here because "0"/"1" is a FORM spelling, not the
    seam's (which takes a real bool)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        features_admin.resolve(slug)
    except features_admin.FeatureNotFound:
        return _error_page(request, 404, "Not found.")
    except features_admin.FeatureDoorMoved as exc:
        return RedirectResponse(exc.door, status_code=303)
    if enabled not in ("0", "1"):
        return _error_page(request, 400, "A feature flag is 0 or 1.")
    # A same-state resubmit is a no-op inside the seam (no write, no audit row): an
    # unchanged toggle is not a real "feature.toggle" event worth a forensics row.
    # `remote=False` -- this is a LOCAL, session-authed write, so the install-locked
    # feature keys (data_export, databricks) stay flippable here exactly as before.
    features_admin.set_feature_enabled(
        slug,
        enabled == "1",
        actor=getattr(me, "email", "system"),
        ip=_ip(request),
        remote=False,
    )
    # The Features card's in-place switch (settings.html) posts the SAME form via fetch
    # with this header and patches the card without a reload; it gets the outcome as
    # JSON. The plain form post (no JS) keeps the redirect below, byte-for-byte.
    if request.headers.get("x-requested-with", "").lower() == "fetch":
        return JSONResponse({"ok": True, "slug": slug, "enabled": enabled == "1"})
    return RedirectResponse("/settings?saved=1#features", status_code=303)


@router.post("/settings/retention")
def settings_retention_save(
    request: Request, retention_enabled: str = Form(""), retention_days: str = Form("0")
):
    """Save the data-retention policy. Separate from settings_save (destructive
    knob, its own audited action). retention_enabled is an unchecked-checkbox form
    field, so absent => off."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # #281: retention.purge has no org filter, so under multi-org the policy is a
    # host-operator act (an org admin could otherwise arm a purge of every org's data).
    if access.install_wide_security_refused(("retention_enabled", "retention_days")):
        return _error_page(request, 403, access.INSTALL_WIDE_REFUSAL)
    try:
        days = max(0, int(retention_days or "0"))
    except (TypeError, ValueError):
        days = 0
    on = "1" if retention_enabled == "1" else "0"
    try:
        settings.set_many({"retention_enabled": on, "retention_days": str(days)})
    except settings.InvalidSettingValue as exc:  # GitHub #502: a window past the datetime range
        return _error_page(request, 400, str(exc))
    audit.log(
        actor=getattr(me, "email", "system"),
        action="retention.policy",
        detail=f"enabled={on} days={days}",
        ip=_ip(request),
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


# Bound a custom SRX upload well under the general 100MB cap — rulesets are tiny
# (a few KB); anything larger is almost certainly not a real .srx.
_MAX_SRX_BYTES = 1 * 1024 * 1024

#: #424: the install-wide segmentation settings (both host-only under multi-org).
_SRX_KEYS = ("srx_enabled", "srx_file")


@router.post("/settings/segmentation")
async def settings_segmentation_save(
    request: Request,
    srx_enabled: str = Form(""),
    srx_use_custom: str = Form(""),
    srx_file: UploadFile | None = File(None),
):
    """Save the segmentation policy: toggle SRX on/off and optionally upload a
    custom .srx ruleset. A new upload is BOUNDED (1MB) and PARSE-VALIDATED before
    it is written into the data dir — an invalid XML / unsupported-ICU / bad-regex
    file is rejected with a clear message and nothing is persisted. Selecting
    'use default' clears the custom pointer (the file on disk is left, harmless).
    """
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # #424: the custom ruleset is ONE file every org ingests with, so under multi-org an org
    # admin may not replace it or switch SRX for every org (host-operator only, the #281
    # pattern). Checked BEFORE the upload is read or any file is written. A strict no-op
    # with the multi-org gate off.
    if access.install_wide_security_refused(_SRX_KEYS):
        return _error_page(request, 403, access.INSTALL_WIDE_REFUSAL)

    updates: dict[str, str] = {"srx_enabled": "1" if srx_enabled == "1" else "0"}

    # Handle an uploaded custom ruleset first (so a bad file blocks the whole save).
    uploaded_name = getattr(srx_file, "filename", "") or ""
    if uploaded_name:
        # #275: bounded read, aborted past the cap (it used to read the whole upload first).
        raw = await read_capped_upload(srx_file, _MAX_SRX_BYTES)
        if raw is None:
            return RedirectResponse(
                "/settings?srx_error=SRX+file+too+large+(max+1MB).", status_code=303
            )
        ok, msg = srx.validate(raw)
        if not ok:
            from urllib.parse import quote

            return RedirectResponse(
                "/settings?srx_error=" + quote("Rejected: " + msg), status_code=303
            )
        srx.write_custom_ruleset(raw)
        updates["srx_file"] = "segmentation.srx"
        audit.log(
            actor=getattr(me, "email", "system"),
            action="segmentation.upload",
            detail=uploaded_name,
            ip=_ip(request),
        )
    elif srx_use_custom != "1":
        # Explicitly asked to use the shipped default: clear the custom pointer.
        updates["srx_file"] = ""

    settings.set_many(updates)
    audit.log(
        actor=getattr(me, "email", "system"),
        action="segmentation.policy",
        detail=f"enabled={updates['srx_enabled']} file={settings.get('srx_file') or '(default)'}",
        ip=_ip(request),
    )
    from urllib.parse import quote

    return RedirectResponse(
        "/settings?srx_msg=" + quote("Segmentation settings saved."), status_code=303
    )


@router.post("/settings/segprofiles")
async def segprofile_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    customer_id: str = Form(""),
    is_system_default: str = Form(""),
    srx_upload: UploadFile | None = File(None),
):
    """Create a segmentation profile: named, optionally customer-scoped, backed by
    an uploaded .srx (bounded + parse-validated) or — with no file — the built-in
    default rules. Admin-gated like every /settings mutation."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # #586: a customer-bound admin cannot create a profile under another customer; that id
    # is "no such customer", the same answer as a missing one.
    _bound = _my_customer_id(request)
    _asked = (customer_id or "").strip()
    if _bound is not None and _asked and _asked != str(_bound):
        return _seg_redirect(err=f"no such customer: {_asked}")
    blob: str | None = None
    uploaded_name = getattr(srx_upload, "filename", "") or ""
    if uploaded_name:
        # #275: bounded read (this upload used to have no cap before validate_ruleset).
        raw = await read_capped_upload(srx_upload, segprof.MAX_SRX_BYTES)
        if raw is None:
            return _seg_redirect(err="Rejected: SRX ruleset too large (max 1MB).")
        ok, msg = segprof.validate_ruleset(raw)
        if not ok:
            return _seg_redirect(err="Rejected: " + msg)
        blob = raw.decode("utf-8", errors="replace")
    try:
        prof = segprof.create_profile(
            (name or "").strip(),
            srx_blob=blob,
            customer_id=customer_id or None,
            description=description,
            is_system_default=(is_system_default == "1"),
            actor=getattr(me, "email", "system"),
            org_id=_my_org_id(request),
        )
    except segprof.SegProfileError as exc:
        return _seg_redirect(err=str(exc))
    audit.log(
        actor=getattr(me, "email", "system"),
        action="segprofile.create",
        target=f"profile {prof.id} ({prof.name})",
        ip=_ip(request),
    )
    return _seg_redirect(msg=f"Profile '{prof.name}' created.")


@router.post("/settings/segprofiles/{pid}/clone")
def segprofile_clone(
    pid: int, request: Request, new_name: str = Form(...), customer_id: str = Form("")
):
    """Clone-before-edit (memoQ convention): duplicate a profile's ruleset under a
    new name; blobs themselves are immutable."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _src, blocked = _owned_profile_or_redirect(request, pid)
    if blocked:
        return blocked
    # #586: same as segprofile_create: another customer's id is "no such customer".
    _bound = _my_customer_id(request)
    _asked = (customer_id or "").strip()
    if _bound is not None and _asked and _asked != str(_bound):
        return _seg_redirect(err=f"no such customer: {_asked}")
    try:
        prof = segprof.clone_profile(
            pid,
            (new_name or "").strip(),
            customer_id=customer_id or None,
            actor=getattr(me, "email", "system"),
            org_id=_my_org_id(request),
        )
    except segprof.SegProfileError as exc:
        return _seg_redirect(err=str(exc))
    if prof is None:
        return _seg_redirect(err="No such profile.")
    audit.log(
        actor=getattr(me, "email", "system"),
        action="segprofile.clone",
        target=f"profile {pid} -> {prof.id} ({prof.name})",
        ip=_ip(request),
    )
    return _seg_redirect(msg=f"Cloned to '{prof.name}'.")


@router.post("/settings/segprofiles/{pid}/default")
def segprofile_default(pid: int, request: Request, make_default: str = Form("")):
    """Mark (or clear) the single system-default profile — the third rung of the
    resolution chain."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _src, blocked = _owned_profile_or_redirect(request, pid)
    if blocked:
        return blocked
    prof = segprof.update_profile(pid, is_system_default=(make_default == "1"))
    if prof is None:
        return _seg_redirect(err="No such profile.")
    audit.log(
        actor=getattr(me, "email", "system"),
        action="segprofile.default",
        target=f"profile {pid} default={make_default == '1'}",
        ip=_ip(request),
    )
    return _seg_redirect(
        msg=("System default set." if make_default == "1" else "System default cleared.")
    )


@router.post("/settings/segprofiles/{pid}/delete")
def segprofile_delete(pid: int, request: Request):
    """Delete a profile — refused while any project/customer still references it
    (the delete guard; reassign or clear those first). Existing documents keep
    their stamped seg_engine either way (TM safety)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _src, blocked = _owned_profile_or_redirect(request, pid)
    if blocked:
        return blocked
    use = segprof.in_use_count(pid)
    if use["total"]:
        return _seg_redirect(
            err=(
                f"Profile is in use by {use['projects']} project(s) and "
                f"{use['customers']} customer(s) — reassign them first."
            )
        )
    # The service now ALSO fails closed (SegProfileError) — catch it so a
    # rare check-then-delete race surfaces as the same banner, never a 500.
    try:
        if not segprof.delete_profile(pid):
            return _seg_redirect(err="No such profile.")
    except segprof.SegProfileError as exc:
        return _seg_redirect(err=str(exc))
    audit.log(
        actor=getattr(me, "email", "system"),
        action="segprofile.delete",
        target=f"profile {pid}",
        ip=_ip(request),
    )
    return _seg_redirect(msg="Profile deleted.")


@router.post("/settings/segprofiles/{pid}/rename")
def segprofile_rename(pid: int, request: Request, name: str = Form(...)):
    """Rename a segmentation profile (wires the existing service update_profile
    name= path to the web). Family gate = admin, matching every other /settings
    segprofile mutation. Keeps the service's app-level name dup-check (case-fold
    behavior unchanged). Audits old -> new in detail (the universal rename rule)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    src, blocked = _owned_profile_or_redirect(request, pid)
    if blocked:
        return blocked
    old_name = src.name
    try:
        prof = segprof.update_profile(pid, name=(name or "").strip(), org_id=_my_org_id(request))
    except segprof.SegProfileError as exc:
        return _seg_redirect(err=str(exc))
    if prof is None:
        return _seg_redirect(err="No such profile.")
    audit.log(
        actor=getattr(me, "email", "system"),
        action="segprofile.rename",
        target=f"profile {pid}",
        detail=f"{old_name!r} -> {prof.name!r}",
        ip=_ip(request),
    )
    return _seg_redirect(msg=f"Renamed to '{prof.name}'.")


# --- Enabled languages (P2, item 1) ---
@router.get("/settings/languages")
def languages_settings(request: Request):
    """The 'Enabled languages' admin page: a searchable checkbox grid over the full
    BCP-47 catalog, marking which locales are OFFERED in the project / language-set
    pickers. Hiding a language never breaks projects already using it (grandfathered
    — enabled is a UI-offering filter, not a hard gate)."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    enabled = set(languages_catalog.enabled_locales())
    catalog = languages_catalog.all_locales()
    for row in catalog:
        row["enabled"] = row["code"] in enabled
    return templates.TemplateResponse(
        request,
        "languages_settings.html",
        {
            "request": request,
            "catalog": catalog,
            "enabled_count": sum(1 for r in catalog if r["enabled"]),
            "total": len(catalog),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/settings/languages")
async def languages_settings_save(request: Request):
    """Persist the enabled-languages selection. Reads the checked `enabled` codes
    from the raw form (a searchable grid posts many same-named checkboxes). Unknown
    codes are dropped by set_enabled; the result is stored as the enabled_locales
    Setting. Its own audited action (languages.enabled)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    codes = [str(v) for v in form.getlist("enabled")]
    # set_enabled canonicalizes/drops unknowns and writes its own audited
    # languages.enabled event (shared with the CLI path).
    languages_catalog.set_enabled(codes, actor=getattr(me, "email", "system"))
    return RedirectResponse("/settings/languages?saved=1", status_code=303)


# --- admin: users / security / audit (admin-gated) ---
USERS_PAGE_SIZE = 50


@router.get("/users")
def users_index(request: Request, page: int = 1, q: str = ""):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    base = settings.external_base_url(request)
    q = (q or "").strip()
    total = security.count_users(q, org_id=_my_org_id(request))
    pages = max(1, -(-total // USERS_PAGE_SIZE))
    page = min(max(1, page), pages)
    users = security.list_users(
        q,
        limit=USERS_PAGE_SIZE,
        offset=(page - 1) * USERS_PAGE_SIZE,
        org_id=_my_org_id(request),
    )
    key_counts = {}
    # #422 gate: owners of an active SCIM token. Disabling one revokes the token for good
    # (identity provider sync stops), so the toggle asks first.
    scim_token_owner_ids = set()
    for u in users:
        live = [k for k in apikeys.list_keys(u.id) if k.active]
        key_counts[u.id] = sum(1 for k in live if not apikeys.key_is_expired(k))
        if any(security.is_scim_key(k) for k in live):
            scim_token_owner_ids.add(u.id)
    # TOTP train: each listed user's two-step sign-in state for the Users table (on / off /
    # required but not set up / not applicable to SSO accounts). One query for the page.
    mfa_on = mfa.enrolled_ids([u.id for u in users])
    mfa_policy = mfa.policy()
    mfa_state = {
        u.id: (
            "sso"
            if not mfa.applies_to(u)
            else "on"
            if u.id in mfa_on
            else "required"
            if mfa.required_for(u, mfa_policy)
            else "off"
        )
        for u in users
    }
    # Multi-org P7: show each user's org (gate ON only). A strict no-op with the
    # sacred gate off — the column is hidden and the page is byte-identical to today.
    # The org is READ-ONLY here: moving a user between orgs is a host operator action
    # (`cli user org`), not a web one, since the super-admin and its console were removed
    # on 2026-09-20. The column shows the caller's own org name and nothing else.
    multi_org_on = access.multi_org_active()
    org_names = {}
    if multi_org_on:
        from app.db import engine as _engine

        with Session(_engine) as _s:
            orgs = access.list_orgs(_s)
        # GATE R1 MED-1: `can_move_org` used to short-circuit this filter entirely, so a
        # super-admin's CUSTOMER-APP session was handed every org's name. Both that account
        # and the console it reached are gone now, so the home-org filter below is simply
        # the only behaviour there is; it is kept explicit rather than assumed.
        #
        # GATE R2 LOW-2: hoist the org id. Calling _my_org_id per comprehension element
        # measured 3 -> 407 calls and 71 -> 475 session_user database reads on a 201-org
        # install, a regression introduced by the MED-1 fix itself.
        home_org = _my_org_id(request)
        org_names = {o.id: o.name for o in orgs if o.id == home_org}
    # R2-3: the Customer-binding picker. ORG-FILTERED (the popout-leak lesson) — a
    # scoped admin must never see another org's customer names. None (gate off) =
    # all customers. Each user's bound-customer NAME is resolved for the table.
    from app.db import engine as _cengine
    from app.models import Customer

    with Session(_cengine) as _cs:
        cq = select(Customer).order_by(Customer.name)
        my_org = _my_org_id(request)
        if my_org is not None:
            cq = cq.where(Customer.org_id == my_org)
        all_bindable = list(_cs.exec(cq).all())
    # SPEC-customer-archive: the binding <select> is a PICKER — only ACTIVE customers
    # are offered for a NEW binding (an archived customer is retired from selection). A
    # user ALREADY bound to a now-archived customer keeps working, so its NAME is still
    # resolved for the users table from the FULL (unfiltered) list — the filter is on
    # the offer list only, never on the name map.
    bind_customers = [c for c in all_bindable if c.archived_at is None]
    customer_names = {c.id: c.name for c in all_bindable}
    # SCIM (Train I): a read-only "SCIM" badge on directory-provisioned accounts + a
    # read-only Groups panel. Org-scoped like everything else on this page (my_org).
    # Groups are DIRECTORY METADATA ONLY — membership confers no authorization in v1.
    scim_on = scim.enabled()
    scim_groups = scim.groups_with_members(my_org) if scim_on else []
    scim_managed_ids = {u.id for u in users if scim.is_scim_managed(u)}
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "request": request,
            "users": users,
            "roles": Role.ALL,
            "scim_on": scim_on,
            "scim_groups": scim_groups,
            "scim_managed_ids": scim_managed_ids,
            "key_counts": key_counts,
            "scim_token_owner_ids": scim_token_owner_ids,
            "mfa_state": mfa_state,
            "mfa_reset": request.query_params.get("mfa_reset"),
            "sign_in_unlocked": request.query_params.get("unlocked"),
            "multi_org_on": multi_org_on,
            "org_names": org_names,
            # R2-3: customer-binding controls.
            "bind_customers": bind_customers,
            "customer_names": customer_names,
            "bindable_roles": access.CUSTOMER_BINDABLE_ROLES,
            "auth_on": security.auth_required(),
            "pending": security.pending_invites(getattr(me, "org_id", None)),
            "expired": security.expired_invites(getattr(me, "org_id", None)),
            # #476: only a reference rides in the (signed, not encrypted) session.
            "invite_link": one_time_stash.pop(request, "invite_link_ref")[1],
            "revoked": request.query_params.get("revoked"),
            "deleted": request.query_params.get("deleted"),
            "base": base,
            "smtp_configured": mailer.configured(),
            "invited": request.query_params.get("invited"),
            "emailed": request.query_params.get("emailed"),
            "erased": request.query_params.get("erased"),
            "error": request.query_params.get("error"),
            "page": page,
            "pages": pages,
            "total": total,
            "q": q,
        },
    )


@router.post("/users")
def users_create(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    role: str = Form(Role.TRANSLATOR),
    password: str = Form(""),
    customer_id: str = Form(""),
    session: Session = Depends(get_session),
):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    if email.strip():
        # R2-3: validate the customer binding at the route (existence + ORG match)
        # before create_user enforces the role invariant. A blank customer_id =
        # internal staff (unchanged). A cross-org / unknown customer is refused
        # (not-found) so a forged POST can't bind to another org's customer.
        cid = (customer_id or "").strip()
        cust_id = None
        if cid:
            from app.kernel.security import _coerce_customer_id
            from app.models import Customer

            coerced = _coerce_customer_id(cid)  # bounds the SQLite binder + drops junk
            cust = session.get(Customer, coerced) if coerced is not None else None
            my_org = _my_org_id(request)
            # #586: a customer-bound admin cannot bind a user to another customer: that id
            # answers exactly like a missing one.
            if (
                cust is None
                or (my_org is not None and cust.org_id != my_org)
                or _customer_block(request, cust.id) is not None
            ):
                return RedirectResponse(
                    "/users?error=" + quote("No such customer."), status_code=303
                )
            cust_id = cust.id
        try:
            # GATE R1 MED-2: stamp the caller's org. Without it the service stored NULL,
            # and this train made that orphan far worse: before, the unscoped listing
            # happened to show the account so an admin could still repair it; now the
            # listing is correctly scoped, so a NULL-org account is invisible,
            # unsearchable, refused by every management route and unable to authenticate.
            security.create_user(
                email,
                name,
                role if role in Role.ALL else Role.TRANSLATOR,
                password,
                customer_id=cust_id,
                org_id=_my_org_id(request),
            )
        except ValueError as e:
            return redirect_with_error("/users", str(e), param="error")
        audit.log(
            actor=getattr(me, "email", "system"),
            action="user.create",
            target=email,
            detail=(f"customer {cust_id}" if cust_id else ""),
            ip=_ip(request),
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/role")
def users_role(
    uid: int, request: Request, role: str = Form(...), session: Session = Depends(get_session)
):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if u is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, u.org_id)
    if blocked:
        return blocked
    # #178-1: the last-admin guard lives in security.set_role (the target's own org).
    res = security.set_role(
        session, uid, role, actor=getattr(me, "email", "system"), org_id=_my_org_id(request)
    )
    if res.get("reason") == "last_admin":
        return _error_page(request, 409, "Cannot remove the last active administrator.")
    if res["ok"]:
        audit.log(
            actor=getattr(me, "email", "system"),
            action="user.role",
            target=res["email"],
            detail=f"{res['old']}->{res['new']}",
            ip=_ip(request),
        )
    elif res.get("reason") == "role_bound":
        # R2-3: refusing to promote a customer-bound user to admin/manager.
        from urllib.parse import quote

        return RedirectResponse(
            "/users?error="
            + quote(
                "A customer-bound account cannot be an admin "
                "or manager. Clear its customer binding first."
            ),
            status_code=303,
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/customer")
def users_customer(
    uid: int, request: Request, customer_id: str = Form(""), session: Session = Depends(get_session)
):
    """R2-3: bind (or clear) a user's customer — the "customer account" control.
    The customer dropdown is ORG-FILTERED, and the id is re-validated here against
    the admin's org (a forged POST can't bind to a foreign-org customer). The
    role<->binding invariant lives in security.set_customer (both surfaces)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if u is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, u.org_id)
    if blocked:
        return blocked
    from urllib.parse import quote

    cid = (customer_id or "").strip()
    # Existence + ORG check on a non-blank id BEFORE the service seam (which owns
    # existence too, but the org match needs the request's org context). Coerce
    # first so a huge/garbage id can't overflow the SQLite binder here.
    if cid and cid not in ("0",):
        from app.kernel.security import _coerce_customer_id
        from app.models import Customer

        coerced = _coerce_customer_id(cid)
        cust = session.get(Customer, coerced) if coerced is not None else None
        my_org = _my_org_id(request)
        # #586: a customer-bound admin binding a user to ANOTHER customer reads "no such
        # customer", exactly like a missing id.
        if (
            cust is None
            or (my_org is not None and cust.org_id != my_org)
            or _customer_block(request, cust.id) is not None
        ):
            return RedirectResponse("/users?error=" + quote("No such customer."), status_code=303)
    res = security.set_customer(
        session, uid, cid, actor=getattr(me, "email", "system"), org_id=_my_org_id(request)
    )
    if res["ok"]:
        audit.log(
            actor=getattr(me, "email", "system"),
            action="user.customer",
            target=res["email"],
            detail=f"{res['old'] or '(none)'}->{res['new'] or '(none)'}",
            ip=_ip(request),
        )
        return RedirectResponse("/users", status_code=303)
    if res.get("reason") == "role_bound":
        return RedirectResponse(
            "/users?error="
            + quote("Only translator, linguist or viewer accounts can be bound to a customer."),
            status_code=303,
        )
    if res.get("reason") == "archived_customer":
        # SPEC-customer-archive (fix 2): a forged/same-org POST tried to bind to
        # an archived customer — the picker already filters these out, this is
        # the service-level refusal for a POST that skips the picker.
        return RedirectResponse(
            "/users?error="
            + quote(
                "That customer is archived — unarchive it, "
                "or pick an active customer, to bind a user."
            ),
            status_code=303,
        )
    if res.get("reason") == "no_customer":
        return RedirectResponse("/users?error=" + quote("No such customer."), status_code=303)
    return RedirectResponse("/users?error=" + quote("Could not update binding."), status_code=303)


@router.post("/users/{uid}/department")
def users_department(
    uid: int, request: Request, department: str = Form(""), session: Session = Depends(get_session)
):
    """Set a user's department (drives content routing + the department inbox).

    Stored lower-cased so it is the ONE canonical form: matching, inbox identity,
    and budget keys all compare case-insensitively, so "Sales" and "sales" must not
    fork into two departments or two inboxes/budgets (Finding 6)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if u is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, u.org_id)
    if blocked:
        return blocked
    res = security.set_department(
        session, uid, department, actor=getattr(me, "email", "system"), org_id=_my_org_id(request)
    )
    if res["ok"]:
        audit.log(
            actor=getattr(me, "email", "system"),
            action="user.department",
            target=res["email"],
            detail=f"{res['old'] or '(none)'}->{res['new'] or '(none)'}",
            ip=_ip(request),
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/apikeys/revoke")
def users_apikeys_revoke(uid: int, request: Request, session: Session = Depends(get_session)):
    """Admin kill switch: revoke every API key a user holds (e.g. on offboarding)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if u is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, u.org_id)
    if blocked:
        return blocked
    if u:
        n = apikeys.revoke_all_for_user(uid, org_id=_my_org_id(request))
        audit.log(
            actor=getattr(me, "email", "system"),
            action="apikey.revoke_all",
            target=u.email,
            detail=f"{n} key(s) revoked",
            ip=_ip(request),
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/toggle")
def users_toggle(uid: int, request: Request, session: Session = Depends(get_session)):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if u is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, u.org_id)
    if blocked:
        return blocked
    if u:
        res = security.set_active(
            session,
            uid,
            not u.active,
            actor=getattr(me, "email", "system"),
            org_id=_my_org_id(request),
        )
        # #178-1: the last-admin guard lives in security.set_active (the target's own org).
        if res.get("reason") == "last_admin":
            return _error_page(request, 409, "Cannot remove the last active administrator.")
        revoked = int(res.get("keys_revoked") or 0)
        audit.log(
            actor=getattr(me, "email", "system"),
            action="user.toggle",
            target=res["email"],
            detail=(
                "active" if res["active"] else f"disabled; sessions ended, {revoked} key(s) revoked"
            ),
            ip=_ip(request),
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/unlock")
def users_unlock(uid: int, request: Request, session: Session = Depends(get_session)):
    """An org admin can clear the separate password account lock."""
    from app.kernel import login_throttle

    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    user = session.get(User, uid)
    if user is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, user.org_id)
    if blocked:
        return blocked
    login_throttle.unlock(uid, actor=getattr(me, "email", "system"))
    return RedirectResponse("/users?unlocked=1", status_code=303)


@router.post("/users/{uid}/mfa-reset")
def users_mfa_reset(uid: int, request: Request, session: Session = Depends(get_session)):
    """Admin reset of a person's two-step sign-in (TOTP train): a lost or replaced phone.
    Deletes their authenticator setup and recovery codes and ends every session they hold
    (mfa.reset bumps the session epoch in the same transaction). If the policy requires
    two-step sign-in for them, they set it up again at their next sign-in. An admin who
    resets their OWN signs themselves out with it, like an own password reset."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if u is None:
        return _error_page(request, 404, "No such user.")
    blocked = _org_block(request, u.org_id)
    if blocked:
        return blocked
    res = mfa.reset(uid)
    audit.log(
        actor=getattr(me, "email", "system"),
        action="mfa.reset",
        target=res["email"],
        detail="was on" if res["was_enrolled"] else "was not set up",
        ip=_ip(request),
    )
    return RedirectResponse("/users?mfa_reset=1", status_code=303)


# The customer-surface move-org route lived here and was REMOVED on 2026-09-20 with the
# super-admin. It had already been narrowed to refuse every CROSS-org move on this
# surface (the linchpin: cross-org authority lived only in the back-office), so once the
# org picker was confined to the caller's home org the only move it could still perform
# was moving a user into the org they were already in. Membership changes are a host
# operator action now: `cli user org <user> <org>`, gated by require_operator.


@router.get("/users/{uid}/export")
def users_export(uid: int, request: Request, session: Session = Depends(get_session)):
    """Admin downloads a copy of any user's data (GDPR subject-access) as NDJSON."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    u = session.get(User, uid)
    if not u:
        return _error_page(request, 404, "No such user.")
    # Multi-org P8: an org-admin can only export a user IN THEIR org. Cross-org is a
    # 404 (indistinguishable from a missing user). None with the gate off = unscoped.
    org_scope = _my_org_id(request)
    if org_scope is not None and u.org_id != org_scope:
        return _error_page(request, 404, "No such user.")
    audit.log(
        actor=getattr(me, "email", "system"), action="gdpr.export", target=u.email, ip=_ip(request)
    )

    def gen():
        for section, rec in gdpr.export_user(uid, caller_org_id=org_scope):
            yield json.dumps({"section": section, **rec}, ensure_ascii=False) + "\n"

    fname = "gdpr-export-%d-%s.ndjson" % (uid, datetime.utcnow().strftime("%Y%m%d-%H%M%S"))  # noqa: UP031
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/users/{uid}/erase")
def users_erase(uid: int, request: Request):
    """Right to erasure (GDPR Art. 17): anonymize the user. DESTRUCTIVE and
    irreversible — the User row is scrubbed and every email pointer is rewritten
    to a tombstone. The audit history is retained (Art. 17(3)(b))."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Multi-org P8: an org-admin can only erase a user IN THEIR org (cross-org is
    # refused as "User not found."). None with the gate off = unscoped, as today.
    ok, msg = gdpr.erase_user(
        uid, getattr(me, "email", "system"), ip=_ip(request), caller_org_id=_my_org_id(request)
    )
    if not ok:
        return redirect_with_error("/users", msg, param="error")
    return RedirectResponse(f"/users?erased={msg}", status_code=303)


#: Passwords card: the page error for a refused value of each policy key (codes in
#: app/web/form_errors.py ERRORS, each linking to its own field).
_PASSWORD_POLICY_ERRORS = {
    "password_min_length": "/security?err=pw_min_length",  # pregate: allow-secret key
    "password_require_classes": "/security?err=pw_require_classes",  # pregate: allow-secret key
    "password_block_common": "/security?err=pw_block_common",  # pregate: allow-secret key
    "password_history": "/security?err=pw_history",  # pregate: allow-secret key
    "password_max_age_days": "/security?err=pw_max_age",  # pregate: allow-secret key
}


@router.get("/security")
def security_page(request: Request):
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    keys = [
        "oauth_airtable_client_id",
        "oauth_atlassian_client_id",
        "security_auth_required",
        "audit_enabled",
        "audit_fail_open",
        "pii_redaction",
        "encrypt_uploads",
        "encrypt_db",
        "sso_enabled",
        "sso_provider",
        "sso_issuer",
        "sso_client_id",
        "sso_trust_unverified_email",
        "ldap_enabled",
        "ldap_host",
        "ldap_ca_cert",
        "ldap_base_dn",
        "ldap_bind_dn",
        "ldap_user_filter",
        "ldap_default_role",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_from",
        "smtp_tls",
        "notify_on_review_assigned",
        "notify_on_budget_threshold",
        "app_base_url",
        "session_idle_minutes",
        "api_rate_limit_per_minute",
        "api_read_rate_limit_per_minute",
        "login_trusted_networks",
        "session_max_hours",
        "accessibility_contact",
        "password_min_length",
        "password_require_classes",
        "password_block_common",
        "password_history",
        "password_max_age_days",
        "mfa_required",
    ]
    return templates.TemplateResponse(
        request,
        "security.html",
        {
            "fly_proxy_warning": (
                security.fly_proxy_trust_status() + ". " + security.FLY_PROXY_FIX
                if security.fly_proxy_trust_status()
                else ""
            ),
            "request": request,
            "external_base_url": settings.external_base_url(request),
            "external_base_url_warning": settings.external_base_url_warning(request),
            "vals": {k: settings.get(k) for k in keys},
            # #220: audit logging is on and may not be switched off from this page.
            "audit_locked": settings.get("audit_enabled") == "1"
            and settings.audit_off_is_host_only(),
            # Gate round 1 L5: the contention posture is host-only on the same installs.
            "audit_posture_locked": settings.audit_off_is_host_only(),
            # secrets (sso_client_secret / smtp_password) are never echoed back to
            # the browser — only whether one is already stored, so the template can
            # show "(unchanged)" vs. a blank required field.
            "sso_client_secret_set": bool(settings.get("sso_client_secret")),
            "ldap_bind_password_set": bool(settings.get("ldap_bind_password_enc")),
            "oauth_airtable_secret_set": bool(settings.get("oauth_airtable_client_secret")),
            "oauth_atlassian_secret_set": bool(settings.get("oauth_atlassian_client_secret")),
            "roles": Role.ALL,
            "user_count": security.user_count(),
            "audit_count": audit.count(),
            # Route through the resolver's own provenance so the display agrees with
            # the actual resolution -- including the unprefixed ENCRYPTION_KEY spelling
            # and the keyfile-wins rule (Train S). A bare env check would wrongly say
            # "keyfile" when only the unprefixed var is honored.
            "key_source": "env var" if config.secret_source("encryption") == "env" else "keyfile",
            "saved": request.query_params.get("saved"),
            # LOW-1: a refused save (e.g. LDAP toggled on without the ldap3 extra) redirects
            # here with ?err=<message>; the template renders it as an error banner.
            "save_error": request.query_params.get("err"),
        },
    )


@router.post("/security")
def security_save(
    request: Request,
    security_auth_required: str = Form("0"),
    audit_enabled: str | None = Form(None),
    audit_fail_open: str | None = Form(None),
    pii_redaction: str = Form("0"),
    encrypt_uploads: str = Form("0"),
    encrypt_db: str | None = Form(None),
    sso_enabled: str = Form("0"),
    sso_provider: str = Form("oidc"),
    sso_issuer: str = Form(""),
    sso_client_id: str = Form(""),
    sso_client_secret: str = Form(""),
    sso_trust_unverified_email: str = Form("0"),
    ldap_enabled: str = Form("0"),
    ldap_host: str = Form(""),
    ldap_ca_cert: str = Form(""),
    ldap_base_dn: str = Form(""),
    ldap_bind_dn: str = Form(""),
    ldap_bind_password: str = Form(""),
    ldap_user_filter: str = Form(""),
    ldap_default_role: str = Form("translator"),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_tls: str = Form("0"),
    notify_on_review_assigned: str = Form("0"),
    notify_on_budget_threshold: str = Form("0"),
    app_base_url: str = Form(""),
    oauth_airtable_client_id: str = Form(""),
    oauth_airtable_client_secret: str = Form(""),
    oauth_atlassian_client_id: str = Form(""),
    oauth_atlassian_client_secret: str = Form(""),
    session_idle_minutes: str | None = Form(None),
    api_rate_limit_per_minute: str | None = Form(None),
    api_read_rate_limit_per_minute: str | None = Form(None),
    login_trusted_networks: str | None = Form(None),
    login_networks_sent: str | None = Form(None),
    session_max_hours: str | None = Form(None),
    accessibility_contact: str | None = Form(None),
    accessibility_contact_sent: str | None = Form(None),
    password_min_length: str | None = Form(None),
    password_require_classes: str | None = Form(None),
    password_block_common: str | None = Form(None),
    password_history: str | None = Form(None),
    password_max_age_days: str | None = Form(None),
    pwpolicy_card_sent: str | None = Form(None),
    mfa_required: str | None = Form(None),
):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # a11y T9 Sessions card. A post that omits a field (an older form, a script) leaves
    # that limit unchanged; a present value must pass the same validator as `config set`
    # and /api config, or nothing on the page is saved.
    session_updates = {
        k: v.strip()
        for k, v in (
            ("session_idle_minutes", session_idle_minutes),
            ("api_rate_limit_per_minute", api_rate_limit_per_minute),
            ("api_read_rate_limit_per_minute", api_read_rate_limit_per_minute),
            ("session_max_hours", session_max_hours),
        )
        if v is not None
    }
    # a11y T10 Accessibility card: omitted = unchanged (like the session limits); a present
    # value is stripped and must pass the `config set` / /api config validator, or nothing
    # on the page is saved and the error summary links to the field. FastAPI hands an EMPTY
    # form field to an optional parameter as None, so the card's hidden
    # accessibility_contact_sent marker is what tells "cleared" (blank = "Contact your
    # administrator") from "not on this form".
    contact_update = {}
    if accessibility_contact is not None or accessibility_contact_sent == "1":
        accessibility_contact = (accessibility_contact or "").strip()
        try:
            settings.validate_value("accessibility_contact", accessibility_contact)
        except settings.InvalidSettingValue:
            return RedirectResponse("/security?err=a11y_contact", status_code=303)
        contact_update["accessibility_contact"] = accessibility_contact
    # Passwords card (password policy, HECVAT AAAI-03). Omitted = unchanged, like the
    # session limits; the card's hidden pwpolicy_card_sent marker says the card was on
    # the form, so an unchecked "block common passwords" box reads as "0", not "absent".
    # Every present value must pass the `config set` / /api config validator, or nothing
    # on the page is saved and the error summary links to the field.
    password_updates = {
        k: v.strip()
        for k, v in (
            ("password_min_length", password_min_length),
            ("password_require_classes", password_require_classes),
            ("password_history", password_history),
            ("password_max_age_days", password_max_age_days),
        )
        if v is not None
    }
    if password_block_common is not None or pwpolicy_card_sent == "1":
        password_updates["password_block_common"] = (password_block_common or "0").strip()
    for _k, _v in password_updates.items():
        try:
            settings.validate_value(_k, _v)
        except settings.InvalidSettingValue:
            return RedirectResponse(_PASSWORD_POLICY_ERRORS[_k], status_code=303)
    # Two-step sign-in card (TOTP train): omitted = unchanged; a present value must pass the
    # `config set` / /api config validator, or nothing on the page is saved. The change is
    # audited below with before/after like every other key on this page.
    # #220: turning audit logging OFF is host-only on a server install or any install that
    # requires sign-in (HECVAT AAAI-09/AAAI-11: always on). The page renders the switch
    # locked (a hidden "1" rides with the form); a post that leaves the field out keeps it
    # on, and a post that explicitly sends "off" is refused with nothing saved. Turning it
    # ON is always allowed here. The host keeps `config set audit_enabled 0` (recorded).
    # Unlocked (a single-user desktop install), an unticked box is absent = "0", as before.
    if settings.get("audit_enabled") == "1" and settings.audit_off_is_host_only():
        if audit_enabled is not None and audit_enabled != "1":
            return RedirectResponse("/security?err=audit_locked", status_code=303)
        audit_enabled = "1"
    elif audit_enabled is None:
        audit_enabled = "0"
    # Gate round 1 L5: the contention posture is host-only on the same installs, for the same
    # reason (a stolen admin session must not flip the trail to fail-open and then induce
    # contention). The page renders it locked with a hidden copy of the stored value; a post
    # without the field keeps it, a post that tries to change it is refused, nothing saved.
    if settings.audit_off_is_host_only():
        current_posture = settings.get("audit_fail_open")
        if audit_fail_open is not None and audit_fail_open != current_posture:
            return RedirectResponse("/security?err=audit_locked", status_code=303)
        audit_fail_open = current_posture
    elif audit_fail_open is None:
        audit_fail_open = "0"
    # #411: trusted sign-in networks. Omitted = unchanged; a present value must pass the
    # `config set` / /api config validator, or nothing on the page is saved. FastAPI hands an
    # empty field over as None, so the hidden login_networks_sent marker tells "cleared"
    # (blank = no trusted networks) from "not on this form", as for the accessibility card.
    networks_update = {}
    if login_trusted_networks is not None or login_networks_sent == "1":
        login_trusted_networks = (login_trusted_networks or "").strip()
        try:
            settings.validate_value("login_trusted_networks", login_trusted_networks)
        except settings.InvalidSettingValue:
            return RedirectResponse("/security?err=login_networks", status_code=303)
        networks_update["login_trusted_networks"] = login_trusted_networks
    mfa_update = {}
    if mfa_required is not None:
        try:
            settings.validate_value("mfa_required", mfa_required.strip())
        except settings.InvalidSettingValue:
            return RedirectResponse("/security?err=mfa_required_value", status_code=303)
        mfa_update["mfa_required"] = mfa_required.strip()
    updates = {
        "security_auth_required": security_auth_required,
        "audit_enabled": audit_enabled,
        "audit_fail_open": audit_fail_open,
        "pii_redaction": pii_redaction,
        "encrypt_uploads": encrypt_uploads,
        "sso_enabled": sso_enabled,
        "sso_provider": sso_provider,
        "sso_issuer": sso_issuer,
        "sso_client_id": sso_client_id,
        "sso_trust_unverified_email": "1" if sso_trust_unverified_email == "1" else "0",
        "ldap_enabled": ldap_enabled,
        "ldap_host": ldap_host.strip(),
        "ldap_ca_cert": ldap_ca_cert.strip(),
        "ldap_base_dn": ldap_base_dn.strip(),
        "ldap_bind_dn": ldap_bind_dn.strip(),
        "ldap_user_filter": ldap_user_filter.strip(),
        "ldap_default_role": (ldap_default_role if ldap_default_role in Role.ALL else "translator"),
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port.strip() or "587",
        "smtp_user": smtp_user.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_tls": smtp_tls,
        "notify_on_review_assigned": notify_on_review_assigned,
        "notify_on_budget_threshold": notify_on_budget_threshold,
        "app_base_url": app_base_url.strip(),
        "oauth_airtable_client_id": oauth_airtable_client_id.strip(),
        "oauth_atlassian_client_id": oauth_atlassian_client_id.strip(),
        **session_updates,
        **contact_update,
        **password_updates,
        **mfa_update,
        **networks_update,
    }
    # #395: the retired checkbox is absent from the form; preserve the host's setting.
    if encrypt_db is not None:
        updates["encrypt_db"] = encrypt_db
    # R3 hardening ride-along: the client secrets + SMTP password are now stored
    # Fernet-ENCRYPTED at rest (settings.set_secret), exactly like the LDAP bind
    # password — NOT as plaintext Setting rows. They keep the masked pattern (an empty
    # submit never blanks a stored secret; this IS the rotation surface). Routed
    # through set_secret below rather than the plaintext `updates` dict; the write
    # MIGRATES any legacy plaintext row to ciphertext in place.
    secret_updates = {}
    if sso_client_secret:
        secret_updates["sso_client_secret"] = sso_client_secret
    if smtp_password:
        secret_updates["smtp_password"] = smtp_password
    if oauth_airtable_client_secret:
        secret_updates["oauth_airtable_client_secret"] = oauth_airtable_client_secret
    if oauth_atlassian_client_secret:
        secret_updates["oauth_atlassian_client_secret"] = oauth_atlassian_client_secret
    before = {k: settings.get(k) for k in updates}
    # #281: under multi-org every key on this page applies to EVERY org, so an org admin
    # may not change a security key here (host-only, like the Ledger controls). Only keys
    # this post actually CHANGES count, so the page still saves its non-security fields.
    # A strict no-op with the multi-org gate off (every install today).
    changed_keys = [k for k, v in updates.items() if (before.get(k) or "") != (v or "")]
    changed_keys += list(secret_updates)
    if ldap_bind_password:
        changed_keys.append("ldap_bind_password_enc")
    if access.install_wide_security_refused(changed_keys):
        return _error_page(request, 403, access.INSTALL_WIDE_REFUSAL)
    try:
        settings.validate_values(session_updates)
        settings.set_many(updates)
    except (RuntimeError, ValueError) as exc:
        # e.g. turning the LDAP toggle on while the opt-in ldap3 extra is absent —
        # settings.set_many refuses atomically (nothing written). Surface the
        # actionable message on the Security page rather than a generic 500 (LOW-1).
        from urllib.parse import quote

        return RedirectResponse("/security?err=" + quote(str(exc)), status_code=303)
    for _k, _v in secret_updates.items():
        settings.set_secret(_k, _v)  # Fernet at rest, live via set_many->apply
    # The LDAP bind password is stored Fernet-ENCRYPTED (settings.set_secret) too.
    # Same masked pattern: an empty submit never blanks a stored bind password.
    ldap_bind_pw_changed = False
    if ldap_bind_password:
        settings.set_secret("ldap_bind_password_enc", ldap_bind_password)
        ldap_bind_pw_changed = True
    # audit which keys changed, before->after — but never the secret's value
    changes = []
    if ldap_bind_pw_changed:
        changes.append("ldap_bind_password (changed)")
    for _k in secret_updates:
        changes.append(f"{_k} (changed)")
    for k, new in updates.items():
        old = before.get(k)
        if (old or "") == (new or ""):
            continue
        if k in (
            "smtp_password",
            "sso_client_secret",
            "oauth_airtable_client_secret",
            "oauth_atlassian_client_secret",
        ):
            changes.append(f"{k} (changed)")
        else:
            changes.append(f"{k} old={old or ''} new={new or ''}")
    audit.log(
        actor=getattr(me, "email", "system"),
        action="security.update",
        detail="; ".join(changes) or "no changes",
        ip=_ip(request),
        # #220: a save that switches audit logging (or its contention posture) is ALWAYS
        # recorded, even though the switch it records may have just closed the gate.
        force=any(
            (before.get(k) or "") != (updates.get(k) or "") for k in settings.AUDIT_POSTURE_KEYS
        ),
    )
    return RedirectResponse("/security?saved=1", status_code=303)


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(admin_part2_router)
router.include_router(admin_jev_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


@router.get("/audit/export")
def audit_export(request: Request, actor: str = "", action: str = "", since: str = ""):
    """Stream the audit log as NDJSON (one JSON object per line) for SIEM
    ingestion (Splunk, Elastic, Sentinel). Honors the same actor/action
    filters as the page, plus `since` (ISO 8601, inclusive) for incremental
    pulls. Hashes are included so the chain re-verifies downstream."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    actor_q = (actor or "").strip()
    action_q = (action or "").strip()
    since_q = (since or "").strip()
    since_dt = None
    if since_q:
        try:
            since_dt = datetime.fromisoformat(since_q.replace("Z", "+00:00"))
        except ValueError:
            return _error_page(
                request,
                400,
                "Invalid 'since' — use ISO 8601, e.g. 2026-07-01 or 2026-07-01T12:00:00Z.",
            )
        if since_dt.tzinfo is not None:
            # AuditLog.ts is stored naive-UTC; normalize before comparing
            since_dt = since_dt.astimezone(timezone.utc).replace(tzinfo=None)
    # log the export itself first, so the event ships in the export when it matches
    audit.log(
        actor=getattr(me, "email", "system"),
        action="audit.export",
        detail="actor={} action={} since={}".format(
            actor_q or "*", action_q or "*", since_q or "*"
        ),
        ip=_ip(request),
    )

    # Multi-org P8: confine the export to the admin's own org (the linchpin — a
    # super-admin exporting from the CUSTOMER app gets only home-org rows; the
    # back-office is the cross-org door). None with the gate off = unscoped, as today.
    org_scope = _my_org_id(request)
    # #586 (gate): a customer-bound admin exports only its own customer's rows, as on the
    # /api twin and the customer-scoped /audit view. None for internal staff (unchanged).
    cust_scope = _my_customer_id(request)

    def gen():
        for rec in audit.iter_export(
            actor=actor_q or None,
            action=action_q or None,
            since=since_dt,
            org_id=org_scope,
            customer_id=cust_scope,
        ):
            yield json.dumps(rec, ensure_ascii=False) + "\n"

    fname = "audit-{}.ndjson".format(datetime.utcnow().strftime("%Y%m%d-%H%M%S"))
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# --- email invitations (admin) ---
@router.post("/users/invite")
def users_invite(
    request: Request,
    email: str = Form(...),
    role: str = Form(Role.TRANSLATOR),
    resend_id: int | None = Form(None),
):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # BUG-A3-1: a malformed email is now refused at the shared seam — surface it as a
    # clean flash, never a 500.
    org_id = getattr(me, "org_id", None)
    if resend_id is not None:
        # Resolve the row in the caller's scope; posted email/role cannot retarget it.
        previous = next(
            (
                inv
                for inv in [*security.pending_invites(org_id), *security.expired_invites(org_id)]
                if inv.id == resend_id
            ),
            None,
        )
        if previous is None:
            return _error_page(request, 404, "Invitation not found.")
        email, role = previous.email, previous.role
        if (
            security.revoke_invite(
                resend_id,
                getattr(me, "email", "system"),
                org_id=org_id,
                ip=_ip(request),
                allow_expired=True,
            )
            is None
        ):
            return _error_page(request, 404, "Invitation not found.")
    try:
        inv = security.create_invite(
            email, role, getattr(me, "email", "system"), org_id=org_id, ip=_ip(request)
        )
    except ValueError as exc:
        return redirect_with_error("/users", str(exc), param="error")
    link = settings.external_url(request, f"/auth/accept/{inv.token}")
    emailed = mailer.send(
        inv.email,
        f"You're invited to {branding.org_name()}",
        f"You've been invited to {branding.org_name()} as '{inv.role}'.\n\n"
        f"Set up your account here (expires in 7 days):\n{link}\n",
    )
    audit.log(
        actor=getattr(me, "email", "system"),
        action="user.invite",
        target=inv.email,
        detail="emailed" if emailed else "link-shown",
        ip=_ip(request),
    )
    if not emailed:
        # #476: keep the link (it carries the invite token) server-side.
        one_time_stash.put(request, "invite_link_ref", link)
    return RedirectResponse(
        f"/users?invited={inv.email}&emailed={'1' if emailed else '0'}", status_code=303
    )


@router.post("/users/invite/{invite_id}/revoke")
def users_invite_revoke(request: Request, invite_id: int):
    from urllib.parse import quote

    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    org_id = getattr(me, "org_id", None)
    # "Delete" on an Expired invitations row posts here too (SPEC-expired-invite-delete):
    # the same org-scoped lists decide what the caller may touch, and it is a SOFT delete
    # (revoked_at set, row kept for audit). Accepted/revoked/foreign ids stay 404.
    pending_ids = {inv.id for inv in security.pending_invites(org_id)}
    expired_ids = {inv.id for inv in security.expired_invites(org_id)}
    if invite_id not in pending_ids | expired_ids:
        return _error_page(request, 404, "Invitation not found.")
    inv = security.revoke_invite(
        invite_id,
        getattr(me, "email", "system"),
        org_id=org_id,
        ip=_ip(request),
        allow_expired=invite_id in expired_ids,
    )
    if inv is None:
        return _error_page(request, 404, "Invitation not found.")
    param = "deleted" if inv.expires_at < inv.revoked_at else "revoked"
    return RedirectResponse(f"/users?{param}=" + quote(inv.email), status_code=303)
