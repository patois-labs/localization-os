"""/api routes: admin 2

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.kernel import (
    audit,
)
from app.web import deps
from app.web.api_cli_admin_1 import _write_validated_settings
from app.web.api_cli_common import _NOT_FOUND, _int_id, _json, _json_object, _r3_gate
from app.web.streaming import CAPACITY_MESSAGE, retry_after, run_sync, stream_slot

router = APIRouter()


# --- B2. feature flags (the central kill switches) ---------------------------
# The remote twin of the Settings > Features card. Both routes are thin shells over
# app.kernel.features_admin, the ONE write seam the web form and the CLI also call.
#
# NEVER FEATURE-GATED, ON PURPOSE. Neither route consults features.is_enabled /
# deps.feature_api_block: the door that turns features OFF may never itself sit behind a
# feature, or an operator could switch away their own ability to switch things back. The
# only locks are the R3 three-lock preamble every /api/admin/* config route carries.
#
# The IMMUTABLE-KEY LAW still holds here. Four feature keys are on
# settings.REMOTE_IMMUTABLE_KEYS (data_export, databricks, scim, saml); a remote flip of
# one is refused by the seam (`remote=True`) with a 400 naming it local-only, so this door
# is NOT a way around what POST /api/admin/config refuses. NOT ON MCP, by design -- see
# integrations/mcp-server/registry.py NEVER_COVER.
@router.get("/api/admin/features")
def api_admin_features_list(request: Request):
    """List every toggleable feature with its live on/off state (mirrors `config
    features` and the Settings > Features card). admin scope + is_admin +
    remote_admin_enabled. Install-global. Each row carries `slug`, `title`,
    `setting_key`, `enabled`, `semantics` (+ the `opt_in`/`removed` split),
    `local_only` (the key is install-locked, so only the host may flip it) and `door`
    (a non-empty path when the switch lives on another page)."""
    from app.kernel import features_admin

    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "features": features_admin.list_features()})


@router.post("/api/admin/features/{slug}")
async def api_admin_feature_set(request: Request, slug: str):
    """Turn one feature on or off (mirrors `config feature enable|disable <slug>`).
    admin scope + is_admin + remote_admin_enabled. Body: `enabled` (a real JSON boolean;
    a string "1"/"true" is a 400 -- a flag is true or false, never a spelling).

    POST, not PUT: every state change on this /api is a POST (the whole surface is
    GET/POST), and a flag write is not an idempotent whole-resource replacement anyway.

    Refusals: an unknown slug is 404 (not found, non-enumerable); a feature whose switch
    lives elsewhere (SCIM/SAML -> /settings/enterprise) is 409 with that door named; an
    install-locked key is 400 (local-only, exactly as POST /api/admin/config refuses it).
    A same-state write is an honest no-op: `changed: false`, nothing written, NO audit
    row (an unchanged toggle is not a forensics event). A real change writes one
    `feature.toggle` row (actor, ip, slug, new value) -- the same row the web form has
    always written, because it is the same call."""
    from app.kernel import features_admin

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"ok": False, "error": "enabled must be true or false."}, status_code=400
        )
    try:
        out = features_admin.set_feature_enabled(
            slug,
            enabled,
            actor=ident.get("actor", ""),
            ip=deps._ip(request),
            remote=True,
        )
    except features_admin.FeatureNotFound:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    except features_admin.FeatureDoorMoved as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except features_admin.FeatureLocalOnly as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except features_admin.FeatureAdminError as exc:
        # Backstop for any FUTURE seam refusal this route does not name yet (and for the
        # seam's own registry-defect guard, unreachable while verify_features holds). An
        # unhandled refusal here would be a 500, and a 5xx on /api is the exact class the
        # regression sweep hunts -- degrade to a clean 400 carrying the seam's message.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


# --- C. engines -------------------------------------------------------------
@router.post("/api/admin/engines")
async def api_admin_engine_create(request: Request):
    """Create an engine profile (mirrors `engine create`). admin scope + is_admin +
    remote_admin_enabled. Body: name, kind, base_url, model, api_key (WRITE-ONLY -
    Fernet-encrypted, never echoed), config (object), enabled. Install-global. The
    response is the masked engine_row (key_set flag only, never the key) plus a
    `warnings` list (non-fatal advice, e.g. a networked jev_local with no key).

    Quality-judge kinds: `jev` (hosted TypeSafe; api_key required, base_url pinned to
    https://api.typesafe.ai, any other value is a 400) and `jev_local` (a Jev-compatible
    server such as SemIf; base_url required, api_key optional). Their optional config
    keys: confidence_floor (0.5 <= value < 1.0), meaning_floor (0..1), timeout_s (seconds).

    Translation engines: config max_parallel_jobs (1..64, blank clears) caps how many translate
    jobs run on the engine at once. It only lowers concurrency under the global worker cap
    (LOC_TMS_JOB_WORKERS); unset = no per-engine limit."""
    from app.kernel import engines

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    cfg = body.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    enabled = body.get("enabled")
    try:
        prof = engines.create_profile(
            name=str(body.get("name") or ""),
            kind=str(body.get("kind") or ""),
            base_url=str(body.get("base_url") or ""),
            api_key=str(body.get("api_key") or ""),
            model=str(body.get("model") or ""),
            cfg=cfg if isinstance(cfg, dict) else None,
            enabled=(True if enabled is None else bool(enabled)),
            actor=ident.get("actor", ""),
            org_id=org_id,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident.get("actor", ""),
        action="engine.create",
        target=prof.name,
        detail="kind={} key={}".format(prof.kind, "(set)" if prof.api_key_enc else "(unset)"),
        ip=deps._ip(request),
    )
    return JSONResponse(
        {
            "ok": True,
            "engine": engines.engine_row(prof),
            "warnings": engines.profile_warnings(prof),
        }
    )


def _owned_engine(engines, eid, org_id):
    """The engine profile `eid` if the caller's org OWNS it, else None. Under the
    multi-org gate a foreign engine and a missing one are the SAME 404 (non-enumerable),
    with the strict `prof.org_id == org` rule the web Setup AI routes use. Gate off
    (org_id None) = unscoped, as before.

    Train A review A1 (HIGH): these three routes used to act by id alone, so an org-A
    admin could repoint org B's keyed engine at a host A controls and then call /test,
    which sent B's stored key there. Every /api route taking an engine id calls this
    BEFORE touching the row; MCP manage_install and `--remote` CLI reach only these."""
    if eid is None:
        return None
    prof = engines.get_profile(eid)
    if prof is None or (org_id is not None and prof.org_id != org_id):
        return None
    return prof


@router.post("/api/admin/engines/{eid}/update")
async def api_admin_engine_update(request: Request, eid: int):
    """Update an engine profile (mirrors `engine update`). admin scope + is_admin +
    remote_admin_enabled. api_key is WRITE-ONLY: a non-empty value ROTATES the stored
    key, an empty/absent one NEVER blanks it. config merges; `clear` removes config
    keys. Org-scoped under the multi-org gate (a foreign engine is a 404, like a missing
    one). Masked engine_row response (never the key)."""
    from app.kernel import engines

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    eid = _int_id(eid)
    before = _owned_engine(engines, eid, org_id)
    if before is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    body = await _json(request)
    cfg = body.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    clear = body.get("clear")
    if clear is not None and not isinstance(clear, (list, tuple)):
        return JSONResponse(
            {"ok": False, "error": "clear must be a list of config keys."}, status_code=400
        )
    enabled = body.get("enabled")
    try:
        prof = engines.update_profile(
            eid,
            name=body.get("name") if "name" in body else None,
            base_url=body.get("base_url") if "base_url" in body else None,
            api_key=body.get("api_key") if "api_key" in body else None,
            model=body.get("model") if "model" in body else None,
            cfg=cfg if isinstance(cfg, dict) else None,
            clear=list(clear) if clear else None,
            enabled=(None if enabled is None else bool(enabled)),
            clear_api_key=body.get("clear_api_key", False),
            clear_access_key_secret=body.get("clear_access_key_secret", False),
            actor=ident.get("actor", ""),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if prof is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident.get("actor", ""),
        action="engine.update",
        target=prof.name,
        detail=f"id={prof.id}"
        + ("; API key removed" if body.get("clear_api_key") else "")
        + ("; access key secret removed" if body.get("clear_access_key_secret") else ""),
        ip=deps._ip(request),
    )
    # #218: a moved destination drops the stored credentials; say so, never silently.
    cleared = engines.credentials_dropped(
        before,
        prof,
        clear_key=body.get("clear_api_key") is True,
        clear_secret=body.get("clear_access_key_secret") is True,
    )
    warnings = engines.profile_warnings(prof)
    return JSONResponse(
        {
            "ok": True,
            "engine": engines.engine_row(prof),
            "credentials_cleared": cleared,
            "warnings": [engines.DESTINATION_CLEARED_NOTE, *warnings] if cleared else warnings,
        }
    )


@router.post("/api/admin/engines/{eid}/delete")
async def api_admin_engine_delete(request: Request, eid: int):
    """Delete an engine profile (mirrors `engine delete`). admin scope + is_admin +
    remote_admin_enabled. FAILS CLOSED (409 with an itemized reference count) while any
    live configuration still references it as engine:<id> - never an orphaning delete.
    Org-scoped under the multi-org gate (a foreign engine is a 404, like a missing
    one); references are still counted across every org."""
    from app.kernel import engines

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    eid = _int_id(eid)
    if _owned_engine(engines, eid, org_id) is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    try:
        deleted = engines.delete_profile(eid) if eid is not None else False
    except engines.EngineInUseError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "references": engines.reference_count(eid)},
            status_code=409,
        )
    if not deleted:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident.get("actor", ""), action="engine.delete", target=str(eid), ip=deps._ip(request)
    )
    return JSONResponse({"ok": True, "id": eid, "deleted": True})


@router.post("/api/admin/engines/{eid}/test")
async def api_admin_engine_test(request: Request, eid: int):
    """Test-probe an engine's reachability/auth (mirrors `engine test`). admin scope +
    is_admin + remote_admin_enabled. Relays only (ok, message) - a lightweight per-kind
    probe that NEVER translates content and NEVER echoes the key. A missing engine is a
    404, and so is another org's engine under the multi-org gate (its stored key must
    never be sent anywhere on a foreign admin's say-so); a network/auth failure is a
    clean ok=false + message, never a 500."""
    from app.kernel import engines

    _ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    eid = _int_id(eid)
    if _owned_engine(engines, eid, org_id) is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # #746: the probe is a network call (connect + read timeouts); off the event loop.
    res = await _jev_offload(engines.test_profile, eid)
    if isinstance(res, JSONResponse):
        return res  # the bounded pool's retryable 503
    ok, msg = res
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/admin/engines/{eid}/models")
async def api_admin_engine_models(request: Request, eid: int):
    """List the models an engine's server offers, using its stored key (mirrors
    `engine models`). Same gate and org scoping as /test; relays {models, blocked,
    auth_failed?, unreachable?} and never the key."""
    from app.kernel import engines

    _ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    eid = _int_id(eid)
    if _owned_engine(engines, eid, org_id) is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # #746: listing models asks the engine's server over the network; off the event loop.
    res = await _jev_offload(engines.list_models, eid)
    if isinstance(res, JSONResponse):
        return res  # the bounded pool's retryable 503
    return JSONResponse(res)


# --- C2. quality-judge Inspector (Jev Train C) ---------------------------------------
# The /api twins of the Setup AI Inspect page (app/web/admin_jev.py), over the same
# app.kernel.jev_inspector functions. R3-gated like every engine route, and org-scoped:
# a foreign, missing or non-judge engine id is the same 404. No response carries a key.
async def _jev_offload(func, *args, **kwargs):
    """Bound Inspector work and return a retryable refusal before starting a worker."""
    async with stream_slot() as admitted:
        if not admitted:
            return JSONResponse(
                {"ok": False, "error": CAPACITY_MESSAGE, "retry_after": retry_after()},
                status_code=503,
                headers={"Retry-After": str(retry_after())},
            )
        return await run_sync(func, *args, **kwargs)


@router.get("/api/admin/engines/{eid}/inspect")
async def api_admin_engine_inspect(request: Request, eid: int):
    """Inspect a quality-judge engine (mirrors `engine inspect` and the Setup AI Inspect
    page). admin scope + is_admin + remote_admin_enabled. Returns the masked engine row,
    the criteria built from the live question set, the exact request body template, the
    decision rule rendered from the real constants (`rule.sections` / `rule.text`), the
    post-editor system prompt and the Ledger link. Sends nothing, costs nothing."""
    from app.kernel import jev_inspector

    _ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked

    def inspect():
        prof = jev_inspector.owned_judge(_int_id(eid), org_id)
        if prof is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse(jev_inspector.inspect_payload(prof))

    return await _jev_offload(inspect)


@router.post("/api/admin/engines/{eid}/jev-try")
async def api_admin_engine_jev_try(request: Request, eid: int):
    """Score ONE segment through a quality-judge engine exactly as a QE-gated step would
    (mirrors `engine jev-try` and the Inspect page's Try it). admin scope + is_admin +
    remote_admin_enabled. Body: source, target, source_locale, target_locale, threshold
    (optional, default 70). Returns the request sent (never an auth header or key), the
    raw response, the parsed card, `verdict` (pass / fail / undecided / not scored, the
    real gate decision), cost and latency, and `autofix` (the exact round-1 post-editor
    prompt). Hosted Jev is priced: once the spend cap is reached nothing is sent and the
    answer is ok false with blocked "spend_cap". Bad input is a 400. Over the per-minute
    try rate, or while another try on the engine (or any hosted try) is in flight, the
    answer is a 429 with Retry-After and nothing is sent."""
    from app.kernel import jev_inspector

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    prof = await _jev_offload(jev_inspector.owned_judge, _int_id(eid), org_id)
    if isinstance(prof, JSONResponse):
        return prof
    if prof is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    body = await _json(request)
    try:
        out = await _jev_offload(
            jev_inspector.try_it,
            prof,
            body.get("source"),
            body.get("target"),
            body.get("source_locale"),
            body.get("target_locale"),
            body.get("threshold"),
            caller="api:" + (ident.get("actor") or "anon"),
            org_id=org_id,
        )
    except jev_inspector.TryBusy as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "retry_after": exc.retry_after},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
    except jev_inspector.InspectError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return out if isinstance(out, JSONResponse) else JSONResponse(out)


@router.post("/api/admin/engines/import-jev-env")
async def api_admin_engine_import_jev_env(request: Request):
    """Create a hosted Jev engine from the install's TYPESAFE_API_KEY (mirrors the Setup
    AI button and `engine import-jev-env`). admin scope + is_admin + remote_admin_enabled.
    Idempotent: when a hosted Jev engine already exists it is returned with created
    false. The key is stored encrypted and never echoed (the masked engine row only). A
    missing env key, or a TYPESAFE_BASE_URL that is not TypeSafe, is a 400."""
    from app.kernel import engines, jev_inspector

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    try:
        result = await _jev_offload(
            jev_inspector.import_env,
            actor=ident.get("actor", ""),
            org_id=org_id,
            ip=deps._ip(request),
        )
    except jev_inspector.InspectError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if isinstance(result, JSONResponse):
        return result
    prof, created = result
    return JSONResponse({"ok": True, "created": created, "engine": engines.engine_row(prof)})


# --- D. provider keys (cloud_keys) ------------------------------------------
@router.post("/api/admin/providers/{name}/key")
async def api_admin_provider_key_set(request: Request, name: str):
    """Set a cloud provider's in-app API key (mirrors `providers key set`). admin scope
    + is_admin + remote_admin_enabled. Body: {"key": "..."}. WRITE-ONLY: the key is
    Fernet-encrypted at rest and NEVER echoed - the response carries only source + a
    last4 fingerprint. Install-global (cloud keys have no per-tenant owner)."""
    from app.kernel import cloud_keys

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    try:
        tail = cloud_keys.set_key(
            str(name),
            str(body.get("key") or ""),
            actor=ident.get("actor", ""),
            ip=deps._ip(request),
        )
    except cloud_keys.UnknownCloudProvider as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {"ok": True, "provider": str(name).strip().lower(), "source": "app", "last4": tail}
    )


@router.post("/api/admin/providers/{name}/key/clear")
async def api_admin_provider_key_clear(request: Request, name: str):
    """Remove a cloud provider's app-stored key (mirrors `providers key clear`). admin
    scope + is_admin + remote_admin_enabled. Falls back to the .env key if present, else
    keyless. No secret in or out. Install-global."""
    from app.kernel import cloud_keys

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    try:
        had = cloud_keys.remove_key(str(name), actor=ident.get("actor", ""), ip=deps._ip(request))
        src = cloud_keys.source(str(name))
    except cloud_keys.UnknownCloudProvider as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {"ok": True, "provider": str(name).strip().lower(), "removed": had, "source": src}
    )


@router.get("/api/admin/providers/{name}/key")
def api_admin_provider_key_show(request: Request, name: str):
    """Show a cloud provider's key SOURCE + last4 fingerprint (mirrors `providers key
    show`). admin scope + is_admin + remote_admin_enabled. NEVER the key value.
    Install-global."""
    from app.kernel import cloud_keys

    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    try:
        st = cloud_keys.status(str(name))
    except cloud_keys.UnknownCloudProvider as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **st})


_LDAP_KEYS = (
    "ldap_enabled",
    "ldap_host",
    "ldap_ca_cert",
    "ldap_base_dn",
    "ldap_bind_dn",
    "ldap_user_filter",
    "ldap_default_role",
)


_OIDC_KEYS = (
    "sso_enabled",
    "sso_provider",
    "sso_issuer",
    "sso_client_id",
    "sso_default_role",
    "sso_trust_unverified_email",
)


_SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_tls")


# --- G. LDAP / OIDC / SMTP config -------------------------------------------
def _identity_status(text_keys, secret_keys):
    """A masked status dict for an identity/SMTP surface: text keys as-is, secret keys
    as "(set)"/"(unset)" (never a value)."""
    from app.kernel import settings as _settings

    out = {k: _settings.get(k) for k in text_keys}
    for k in secret_keys:
        out[k] = "(set)" if bool(_settings.read_secret_setting(k)) else "(unset)"
    return out


def _identity_write(request: Request, ident, body: dict, keys, secret_field, secret_key, label):
    """#280: the dedicated LDAP / OIDC / SMTP routes write through the SAME validated spine
    as POST /api/admin/config (_write_validated_settings): the immutable-key law, the
    multi-org refusal (#281), per-key value validation and the SENSITIVE confirm step, which
    these routes used to skip by calling set_many directly. `confirm` rides in the body
    exactly as on /api/admin/config (true, a list of keys, or {key: true}). The write-only
    secret field joins the same atomic batch (Fernet at rest via set_secret); an empty or
    absent value never blanks a stored secret. Returns a ready 4xx response or None."""
    # #513(2): RAW values. _write_validated_settings refuses a list/dict/null and then
    # normalises (bool -> "1"/"0"); normalising here first turned `null` into the string
    # "None" and a list into "['a', 'b']" before that refusal could ever see them.
    updates = {k: body[k] for k in keys if k in body}
    secret = body.get(secret_field)
    if secret:
        updates[secret_key] = str(secret)
    if not updates:
        return None
    detail = f"{label}: " + ", ".join(
        sorted(f"{k} (changed)" if k == secret_key else k for k in updates)
    )
    blocked, _normalized = _write_validated_settings(
        ident, request, updates, confirm=body.get("confirm"), audit_detail=detail
    )
    return blocked


@router.post("/api/admin/ldap")
async def api_admin_ldap_set(request: Request):
    """Configure LDAP/AD auth (mirrors the Security page's LDAP block). admin scope +
    is_admin + remote_admin_enabled. Body: any of ldap_enabled/host/base_dn/bind_dn/
    user_filter/default_role (plain) + bind_password (WRITE-ONLY -> Fernet at rest via
    set_secret as ldap_bind_password_enc; an empty/absent value never blanks a stored
    password) + `confirm` for the security-sensitive keys (every LDAP key is one since
    #280). Install-global. Never echoes the password."""
    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    # #513 gate: a non-object body is a 400, never a silent no-op 200.
    body, bad = await _json_object(request)
    if bad:
        return bad
    blocked = _identity_write(
        request, ident, body, _LDAP_KEYS, "bind_password", "ldap_bind_password_enc", "ldap"
    )
    if blocked:
        return blocked
    return JSONResponse(
        {"ok": True, "ldap": _identity_status(_LDAP_KEYS, ("ldap_bind_password_enc",))}
    )


@router.get("/api/admin/ldap")
def api_admin_ldap_status(request: Request):
    """Masked LDAP status. admin scope + is_admin + remote_admin_enabled. The bind
    password shows (set)/(unset), never a value."""
    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    return JSONResponse(
        {"ok": True, "ldap": _identity_status(_LDAP_KEYS, ("ldap_bind_password_enc",))}
    )


@router.post("/api/admin/oidc")
async def api_admin_oidc_set(request: Request):
    """Configure OIDC/SSO (mirrors `admin oidc config`). admin scope + is_admin +
    remote_admin_enabled. Body: any of sso_enabled/provider/issuer/client_id/
    default_role/sso_trust_unverified_email (plain) + client_secret (WRITE-ONLY ->
    Fernet at rest via set_secret; an empty/absent value never blanks a stored secret)
    + `confirm` for the
    security-sensitive keys (every OIDC key is one since #280). Install-global."""
    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    # #513 gate: a non-object body is a 400, never a silent no-op 200.
    body, bad = await _json_object(request)
    if bad:
        return bad
    blocked = _identity_write(
        request, ident, body, _OIDC_KEYS, "client_secret", "sso_client_secret", "oidc"
    )
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "oidc": _identity_status(_OIDC_KEYS, ("sso_client_secret",))})


@router.get("/api/admin/oidc")
def api_admin_oidc_status(request: Request):
    """Masked OIDC status. admin scope + is_admin + remote_admin_enabled. The client
    secret shows (set)/(unset), never a value."""
    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "oidc": _identity_status(_OIDC_KEYS, ("sso_client_secret",))})


@router.post("/api/admin/smtp")
async def api_admin_smtp_set(request: Request):
    """Configure outbound SMTP (mirrors the Security page's SMTP block). admin scope +
    is_admin + remote_admin_enabled. Body: any of smtp_host/port/user/from/tls (plain) +
    smtp_password (WRITE-ONLY -> Fernet at rest via set_secret; an empty/absent value
    never blanks a stored password). Install-global. Never echoes the password. Writes
    through the shared validated spine (#280), like the LDAP and OIDC routes."""
    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    # #513 gate: a non-object body is a 400, never a silent no-op 200.
    body, bad = await _json_object(request)
    if bad:
        return bad
    blocked = _identity_write(
        request, ident, body, _SMTP_KEYS, "smtp_password", "smtp_password", "smtp"
    )
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "smtp": _identity_status(_SMTP_KEYS, ("smtp_password",))})


@router.get("/api/admin/smtp")
def api_admin_smtp_status(request: Request):
    """Masked SMTP status. admin scope + is_admin + remote_admin_enabled. The password
    shows (set)/(unset), never a value."""
    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "smtp": _identity_status(_SMTP_KEYS, ("smtp_password",))})
