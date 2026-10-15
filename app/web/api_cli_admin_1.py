"""/api routes: admin 1

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.kernel import (
    access,
    audit,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _gate,
    _gate_admin,
    _json,
    _r3_gate,
    _remote_admin_block,
)

router = APIRouter()


# --- semantic TM (install-global settings) ---------------------------------
def _admin_setting_write(request: Request, items: dict, action: str, detail: str):
    """Shared: the R3 three-lock preamble (admin SCOPE + is_admin role +
    remote_admin_enabled) then a VALIDATED install-global settings write through
    _write_validated_settings (immutable-key law, unknown-key refusal, sensitive-key
    confirm, masked audit). Returns (blocked_or_None, ident). These toggles change how the
    install behaves for an ADMIN over /api (the R2 config extension), so they carry the
    SAME locks + validation spine as POST /api/admin/config -- NOT the old docs-scope +
    raw set_many path, which let a docs-scoped key arm install-global config (the
    docs-scope-privilege HIGH)."""
    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked, None
    b, _normalized = _write_validated_settings(
        ident, request, items, action=action, audit_detail=detail
    )
    if b:
        return b, None
    return None, ident


# --- SRX segmentation (install-global settings) ----------------------------
@router.post("/api/admin/srx/enable")
def api_srx_enable(request: Request):
    """Use the SRX rule engine for segmentation (mirrors `admin srx enable`). is_admin."""
    blocked, _ident = _admin_setting_write(
        request, {"srx_enabled": "1"}, "segmentation.policy", "enabled=1"
    )
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "srx_enabled": "1"})


@router.post("/api/admin/srx/disable")
def api_srx_disable(request: Request):
    """Fall back to the legacy splitter (mirrors `admin srx disable`). is_admin."""
    blocked, _ident = _admin_setting_write(
        request, {"srx_enabled": "0"}, "segmentation.policy", "enabled=0"
    )
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "srx_enabled": "0"})


@router.post("/api/admin/srx/validate")
async def api_srx_validate(request: Request):
    """Parse-validate an SRX ruleset (mirrors `admin srx validate`). admin scope +
    is_admin + remote_admin_enabled. Body: srx_b64 (base64 of the .srx file). Writes
    NOTHING. Gated with the same R3 three-lock preamble as its enable/disable/upload
    siblings so the whole SRX admin surface is non-enumerable and uniform (was docs-scope +
    role only; the docs-scope-privilege fix)."""
    from app.kernel import srx

    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    raw, err = _decode_b64(body.get("srx_b64"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    ok, msg = srx.validate(raw)
    if not ok:
        return JSONResponse({"ok": False, "error": f"invalid SRX: {msg}"}, status_code=400)
    return JSONResponse({"ok": True, "message": msg})


@router.post("/api/admin/srx/upload")
async def api_srx_upload(request: Request):
    """Validate + install a custom SRX ruleset (mirrors `admin srx upload`). admin scope +
    is_admin + remote_admin_enabled. Body: srx_b64. An invalid file is rejected (400) and
    nothing is persisted. The `srx_file` setting write routes through the shared validation
    spine (was docs-scope + raw set_many; the docs-scope-privilege fix)."""
    from app.kernel import srx

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    # #424: under multi-org the install-wide ruleset is host-only. Refuse BEFORE the file
    # write (the settings spine below would refuse srx_file too, but only after the file
    # on disk had already been replaced). A strict no-op with the multi-org gate off.
    refused = access.install_wide_security_refused(("srx_enabled", "srx_file"))
    if refused:
        return JSONResponse(
            {
                "ok": False,
                "error": access.INSTALL_WIDE_REFUSAL
                + " Refused key(s): {}.".format(", ".join(refused)),
            },
            status_code=403,
        )
    body = await _json(request)
    raw, err = _decode_b64(body.get("srx_b64"))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    ok, msg = srx.validate(raw)
    if not ok:
        return JSONResponse({"ok": False, "error": f"rejected: {msg}"}, status_code=400)
    srx.write_custom_ruleset(raw)
    blocked, _normalized = _write_validated_settings(
        ident,
        request,
        {"srx_file": "segmentation.srx"},
        action="segmentation.upload",
        audit_detail="%d bytes" % len(raw),  # noqa: UP031
    )
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "bytes": len(raw), "message": msg})


def _decode_b64(raw):
    """(bytes, None) or (None, error): decode a base64 body field, capped at
    MAX_UPLOAD_BYTES. A missing/invalid field is a clean error string."""
    import base64 as _b64

    from app import config as _config

    if not raw or not isinstance(raw, str):
        return None, "srx_b64 is required (base64 of the .srx file)."
    try:
        data = _b64.b64decode(raw, validate=True)
    except Exception:
        return None, "srx_b64 is not valid base64."
    cap = getattr(_config, "MAX_UPLOAD_BYTES", 104857600)
    if len(data) > cap:
        return None, "srx file too large."
    return data, None


# --- retention -------------------------------------------------------------
@router.post("/api/admin/retention/config")
async def api_retention_config(request: Request):
    """Set the retention policy (mirrors `admin retention config`). admin scope + is_admin
    + remote_admin_enabled. Body: enabled (bool / "on" / "off"), days (>=0), and `confirm`
    (true, or the keys) since both keys are security-sensitive (#280). Install-global.

    THIS ROUTE ARMS THE DESTRUCTIVE RETENTION DAEMON (retention.purge deletes aged
    Documents/Segments/blobs/terminal Jobs and clears the TM, install-wide, with NO org
    filter), so it carries the SAME locks as its sibling `api_retention_purge` -- admin
    SCOPE + is_admin role + remote_admin_enabled -- and its write routes through the shared
    validation spine (was docs-scope + raw set_many, which let a docs-scoped key ARM the
    same deletion the purge route guards with four locks; the docs-scope-privilege HIGH)."""
    from app.kernel import settings as _settings

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    updates = {}
    if "enabled" in body and body.get("enabled") is not None:
        val = body.get("enabled")
        on = val is True or str(val).strip().lower() in ("1", "on", "true", "yes")
        updates["retention_enabled"] = "1" if on else "0"
    if "days" in body and body.get("days") is not None:
        try:
            days = int(body.get("days"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "days must be an integer."}, status_code=400)
        if days < 0:
            return JSONResponse({"ok": False, "error": "days must be >= 0."}, status_code=400)
        updates["retention_days"] = str(days)
    if updates:
        # #280: both retention keys are SENSITIVE (arming the purge daemon or shortening its
        # window is confirmed, like purge-now's confirm:"purge"), so the body carries
        # `confirm` exactly as on POST /api/admin/config.
        blocked, _normalized = _write_validated_settings(
            ident, request, updates, confirm=body.get("confirm")
        )
        if blocked:
            return blocked
    return JSONResponse(
        {
            "ok": True,
            "retention_enabled": _settings.get("retention_enabled"),
            "retention_days": _settings.get("retention_days"),
        }
    )


@router.get("/api/admin/retention/status")
def api_retention_status(request: Request):
    """Show the retention policy (mirrors `admin retention status`). is_admin, read."""
    from app.kernel import retention

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    en, days = retention.enabled(), retention.retention_days()
    return JSONResponse({"ok": True, "enabled": en, "days": days, "would_sweep": en and days > 0})


@router.post("/api/admin/retention/purge")
async def api_retention_purge(request: Request):
    """Run one retention sweep NOW (mirrors `admin retention purge-now`). DESTRUCTIVE +
    IRREVERSIBLE: `admin` scope + is_admin role + `remote_admin_enabled` + a
    `confirm:"purge"` echo. Refuses when retention is disabled or days=0 (nothing to
    do), and refuses a wrong/absent confirm (400) — deleting nothing either way."""
    from app.kernel import audit, retention

    ident, _org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    rb = _remote_admin_block(ident)
    if rb:
        return rb
    body = await _json(request)
    if (body.get("confirm") or "") != "purge":
        return JSONResponse(
            {
                "ok": False,
                "error": 'confirm the purge by sending confirm:"purge"; nothing was deleted.',
            },
            status_code=400,
        )
    if not retention.enabled():
        return JSONResponse(
            {"ok": False, "error": "retention is disabled; enable it first."}, status_code=409
        )
    if retention.retention_days() <= 0:
        return JSONResponse(
            {
                "ok": False,
                "error": "retention_days is 0 (no window); a purge would delete nothing.",
            },
            status_code=409,
        )
    result = retention.purge()
    if result.get("busy"):
        request.state.retention_purge_busy = True
        return JSONResponse(
            {
                "ok": False,
                "busy": True,
                "error": "a retention purge is already running; try again shortly.",
            },
            status_code=409,
        )
    audit.log(
        actor=ident.get("actor", ""),
        action="retention.purge",
        detail=f"removed {result}",
    )
    return JSONResponse({"ok": True, "removed": result})


@router.post("/api/admin/tick")
def api_admin_tick(request: Request):
    """Run ONE scheduler pass NOW, synchronously (mirrors the `admin tick` CLI verb).

    This is the EXTERNAL-CRON entry for the embedded/headless profile (Train 0a), where
    the in-process scheduler daemon is not started: an external scheduler drives the
    passage of time by hitting this route (paired with the retention purge above — the
    documented "external cron pair"). It is available in ALL modes; a manual tick in a
    normal install is harmless. tick() runs the piggybacked auto-close / vendor-expiry /
    tap-orphan-reap / tap-TTL sweeps, then — behind sync_scheduler_enabled — the vendor
    network sweeps + any due connector schedules, and reports how many connector-sync
    jobs it enqueued.

    Gates: `admin` scope (_gate_admin) + is_admin role (_admin_block). Deliberately NOT
    behind remote_admin_enabled — a tick is the routine passage of time, NOT an
    irreversible admin verb (contrast api_retention_purge above, which keeps its
    admin-scope + role + remote_admin_enabled + confirm-echo quad because it deletes
    data). scheduler.tick_now() serializes this against the daemon loop + a concurrent
    manual tick with a NON-BLOCKING guard: if a tick is already running this returns
    409 {ok:false, ran:false, busy:true} rather than double-running the egress sweeps.

    Returns only the COUNT of enqueued connector-sync jobs (`jobs_enqueued`), never the
    job ids: the ids are install-global (the scheduler fires across every org's due
    connections) and would leak cross-org activity to any admin caller."""
    import logging as _logging

    from app.kernel import scheduler

    ident, _org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    try:
        ran, fired = scheduler.tick_now()
    except Exception as exc:
        _logging.getLogger("loc_tms.api").exception("Manual scheduler tick failed")
        return JSONResponse(
            {
                "ok": False,
                "ran": False,
                "error": f"scheduler tick failed: {type(exc).__name__}",
            },
            status_code=500,
        )
    if not ran:
        return JSONResponse(
            {
                "ok": False,
                "ran": False,
                "busy": True,
                "error": "a scheduler tick is already running; try again shortly.",
            },
            status_code=409,
        )
    return JSONResponse({"ok": True, "ran": True, "jobs_enqueued": len(fired)})


# --- A/B. settings config ---------------------------------------------------
def _norm_setting_value(v) -> str:
    """Setting values are strings; coerce bools like `config apply` (True->"1")."""
    return "1" if v is True else "0" if v is False else str(v)


def _masked_setting(key: str):
    """Read through the shared masked settings seam."""
    from app.kernel import settings

    return settings.display_value(key)


def _write_validated_settings(
    ident,
    request: Request,
    updates: dict,
    *,
    action: str = "config.set",
    confirm=None,
    audit_detail=None,
):
    """The SINGLE validated path for an install-global settings write — the R3 spine that
    POST /api/admin/config enforces, extracted so EVERY setting-mutating admin route funnels
    through it and none can bypass the immutable-key law or drop the audit row (the
    docs-scope-privilege remediation). Validates, ALL before ANY write:
      * body must be a non-empty {key: value} map;
      * a non-scalar value (list/dict/None) is refused;
      * a REMOTE_IMMUTABLE key (the sacred gates) is refused, always, no confirm override;
      * a FEATURE-FLAG key is refused: flags have their own named door on the wire
        (see below);
      * an unknown key (not in DEFAULTS) is refused;
      * a SENSITIVE key requires `confirm` PER KEY.

    THE FEATURE-FLAG ARM (feature-flag admin train). Every feature `setting_key` is an
    ordinary DEFAULTS key, so before this arm existed the generic config door wrote it
    like any other setting -- which made this a SECOND, unnamed remote flag door, and
    (because integrations/mcp-server/server.py's manage_install(set_config) forwards
    straight to POST /api/admin/config) an MCP one too, contradicting the
    Maintainer-ratified "API yes, MCP no" posture and every place that documents it. A gate
    probe flipped 15 of the 19 flags through this door, recording them as `config.set`
    rather than `feature.toggle`.

    So the WIRE has exactly one flag door. A key that is a registry feature's
    `setting_key` is refused here with a 400 naming that door, which relays through the
    MCP handler as a tool error. Two consequences worth stating: `feature.toggle` is now
    the COMPLETE record of remote flag changes, and the MCP exclusion is enforced at the
    generic config door, not merely undeclared in the tool registry.

    HOST-LOCAL DOORS ARE UNCHANGED, deliberately. `config set` / `config apply` write
    through app.services.settings directly and never reach this helper, and the Settings
    form goes through features_admin; both are host/session doors, not wire doors, and a
    host operator keeps every escape hatch they had. A host `config set <feature_key>`
    therefore still writes a `config.set` row -- a second LOCAL record of a flag change.

    The refused set is DERIVED LIVE from features.FEATURES, never hand-listed, so a
    feature added to the registry closes this door on the same commit.
    Then writes plain keys in one atomic set_many, secret keys through set_secret (Fernet
    at rest), and emits a secret-MASKED audit row. `updates` maps keys to raw values (bools
    coerced like `config apply`). Returns (blocked, normalized): `blocked` is a ready-to-
    return 400 JSONResponse on any validation failure (NOTHING written), else None with
    `normalized` = {key: str-value}. Callers layer their OWN scope/role/switch gate ahead
    of this (this helper never gates); the config route + _admin_setting_write + the
    retention/srx routes all share it so the four validation guarantees are identical."""
    from app.kernel import settings as _settings

    if not isinstance(updates, dict) or not updates:
        return (
            JSONResponse(
                {
                    "ok": False,
                    "error": "body must carry a non-empty `settings` object {key: value, ...}.",
                },
                status_code=400,
            ),
            None,
        )
    # A Setting value is always a scalar. Reject a list/dict/null value BEFORE any write,
    # rather than str()-coercing it into stored garbage (e.g. {"smtp_port": [1,2,3]}).
    nonscalar = sorted(k for k, v in updates.items() if isinstance(v, (list, dict)) or v is None)
    if nonscalar:
        return (
            JSONResponse(
                {
                    "ok": False,
                    "error": "setting values must be scalar (string/number/"
                    "boolean); non-scalar value(s) for: {}.".format(", ".join(nonscalar)),
                },
                status_code=400,
            ),
            None,
        )
    normalized = {str(k): _norm_setting_value(v) for k, v in updates.items()}
    # 1. THE IMMUTABLE-KEYS LAW (first - the safety spine). No confirm can override it.
    immutable = sorted(k for k in normalized if k in _settings.REMOTE_IMMUTABLE_KEYS)
    if immutable:
        return (
            JSONResponse(
                {
                    "ok": False,
                    "error": "these keys are LOCAL-ONLY and can never be set "
                    "over /api (set them on the host with `config set`): {}.".format(
                        ", ".join(immutable)
                    ),
                },
                status_code=400,
            ),
            None,
        )
    # 1b. THE FEATURE-FLAG ARM. Flags get their own named wire door so the change is
    # recorded as `feature.toggle` and the MCP exclusion is real (see the docstring).
    # AFTER the immutable law on purpose: the four feature keys that are ALSO
    # install-locked (data_export, databricks, scim, saml) keep the LOCAL-ONLY refusal,
    # which is the stronger and more actionable statement for them -- the features route
    # would refuse them too, so pointing there would only add a hop. Derived live from
    # the registry; `app.features` imports nothing at module level, so this stays cheap.
    from app import features as _features

    flag_keys = {f.setting_key: f.slug for f in _features.FEATURES}
    flags = sorted(k for k in normalized if k in flag_keys)
    if flags:
        # NOT .format(): the message itself contains the literal "{slug}" of the route
        # template, which .format would read as a placeholder (it did, and raised).
        # .get, not [k]: a future edit that widens the predicate without widening the
        # lookup would otherwise KeyError into a 500 instead of refusing cleanly.
        named = ", ".join(f"{k} ({flag_keys.get(k, '?')})" for k in flags)
        return (
            JSONResponse(
                {
                    "ok": False,
                    "error": "feature flags are not set through the generic config "
                    "door: use POST /api/admin/features/{slug} or `config feature "
                    "enable|disable <slug>`, so the change is audited as a "
                    "feature.toggle. Refused key(s): " + named + ".",
                },
                status_code=400,
            ),
            None,
        )
    # 2. unknown-key refusal (reject the whole request; write nothing).
    unknown = sorted(k for k in normalized if k not in _settings.DEFAULTS)
    if unknown:
        return (
            JSONResponse(
                {"ok": False, "error": "unknown setting key(s): {}.".format(", ".join(unknown))},
                status_code=400,
            ),
            None,
        )

    # 2a. #281: under multi-org an install-wide SECURITY key applies to every org, so an
    # org admin's key may not write it (the Ledger-controls pattern). A strict no-op while
    # the multi-org gate is off. 403, not 400: the request is well-formed, the caller is not
    # the one who may make it.
    refused = access.install_wide_security_refused(normalized)
    if refused:
        return (
            JSONResponse(
                {
                    "ok": False,
                    "error": access.INSTALL_WIDE_REFUSAL
                    + " Refused key(s): {}.".format(", ".join(refused)),
                },
                status_code=403,
            ),
            None,
        )

    # 2b. PER-KEY VALUE VALIDATION (P3 round-2 M1 / round-3 M5). The money-bearing /
    # egress settings (advanced_web_search_cost_usd / _max_per_unit / _provider /
    # advanced_searxng_base_url) run their shared value-validator here, the ONE registry
    # every write door consults. A garbage price ("abc"/"-1"/"inf"), an unknown provider,
    # or an SSRF-blocked SearXNG host is refused BEFORE any write (nothing stored), so the
    # wire door cannot become the unvalidated hole. Belt-and-suspenders alongside the
    # read-side clamp; keys with no registered validator are unaffected.
    for _k, _v in normalized.items():
        try:
            _settings.validate_value(_k, _v)
        except _settings.InvalidSettingValue as _exc:
            return JSONResponse({"ok": False, "error": str(_exc)}, status_code=400), None

    # 3. sensitive keys require confirm PER KEY.
    def _confirmed(k: str) -> bool:
        if confirm is True:
            return True
        if isinstance(confirm, dict):
            return bool(confirm.get(k))
        if isinstance(confirm, (list, tuple)):
            return k in confirm
        return False

    unconfirmed = sorted(
        k for k in normalized if k in _settings.SENSITIVE_KEYS and not _confirmed(k)
    )
    if unconfirmed:
        return (
            JSONResponse(
                {
                    "ok": False,
                    "error": "these keys are security-sensitive; resend with "
                    "`confirm` set for each: {}.".format(", ".join(unconfirmed)),
                },
                status_code=400,
            ),
            None,
        )
    # Write: plain keys in one atomic set_many; secret keys through set_secret.
    plain, secret = {}, {}
    for k, v in normalized.items():
        (secret if _settings.is_secret_setting(k) else plain)[k] = v
    if plain:
        try:
            # Gate round 1 LOW-2: the actor names the admin in a slot-move audit row.
            _settings.set_many(plain, actor=ident.get("actor", ""))
        except (RuntimeError, ValueError) as exc:
            # e.g. arming the LDAP toggle while the opt-in ldap3 extra is absent —
            # set_many refuses atomically (nothing written). Return the actionable
            # message as a clean 400, not a generic 500 (LOW-1).
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400), None
    for k, v in secret.items():
        _settings.set_secret(k, v)  # Fernet at rest; migrates any legacy plaintext row
    # Audit WITHOUT any secret value (mask on the BROAD is_secret_key rule). Callers may
    # override with a purpose-specific detail (e.g. srx upload's byte count).
    if audit_detail is None:
        audit_detail = ", ".join(
            f"{k}=(changed)" if _settings.is_secret_key(k) else f"{k}={_settings.mask_value(k, v)}"
            for k, v in normalized.items()
        )
    audit.log(
        actor=ident.get("actor", ""), action=action, detail=audit_detail, ip=deps._ip(request)
    )
    return None, normalized


@router.get("/api/admin/config")
def api_admin_config_get(request: Request):
    """Read configuration under the full R3 scope, role and switch gate."""
    from app.kernel import settings as _settings

    _ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    payload = {k: _masked_setting(k) for k in _settings.DEFAULTS}
    payload["egress_private_allowlist"] = _settings.egress_policy_view()
    return JSONResponse({"ok": True, "settings": payload})


@router.post("/api/admin/config")
async def api_admin_config_set(request: Request):
    """Set one or more settings (mirrors `config set` / `config apply`). admin scope +
    is_admin + remote_admin_enabled. Body: {"settings": {key: value, ...}} plus an
    optional `confirm` (bool | [keys] | {key: bool}). Rules, ALL validated before ANY
    write:
      * a REMOTE_IMMUTABLE key (the sacred gates) is refused, always, no confirm
        override - a clean 400 naming it local-only;
      * an unknown key is a clean 400;
      * a SENSITIVE key requires confirm PER KEY (else 400 naming the unconfirmed keys);
      * a SECRET_SETTING key routes through set_secret (Fernet at rest, migrating any
        legacy plaintext row) - its value is accepted but NEVER echoed.
    The response is a MASKED read of the changed keys (no secret value ever leaves).

    Every validation + write step lives in the shared _write_validated_settings spine, so
    this route and the retention/srx/semantic config routes enforce the SAME immutable-key
    law, unknown-key refusal, sensitive-key confirm and masked audit (no divergence)."""
    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    blocked, normalized = _write_validated_settings(
        ident, request, body.get("settings"), confirm=body.get("confirm")
    )
    if blocked:
        return blocked
    return JSONResponse(
        {
            "ok": True,
            "changed": sorted(normalized),
            "settings": {k: _masked_setting(k) for k in normalized},
        }
    )


@router.post("/api/admin/ledger/envelopes/{state}")
async def api_ledger_envelopes(state: str, request: Request):
    from app.kernel import ledger_controls

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    if access.multi_org_active():
        return JSONResponse(
            {"error": "Use the host CLI with --operator for install-wide controls."},
            status_code=403,
        )
    if state not in ("on", "off"):
        return JSONResponse(_NOT_FOUND, status_code=404)
    body = await _json(request)
    try:
        return JSONResponse(
            ledger_controls.set_envelopes(
                state == "on",
                confirm=body.get("confirm", ""),
                reason=body.get("reason", ""),
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/api/admin/ledger/purge")
async def api_ledger_purge(request: Request):
    from app.kernel import ledger_controls

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    if access.multi_org_active():
        return JSONResponse(
            {"error": "Use the host CLI with --operator for install-wide controls."},
            status_code=403,
        )
    body = await _json(request)
    try:
        return JSONResponse(
            ledger_controls.purge(
                before=body.get("before"),
                confirm=body.get("confirm", ""),
                apply=body.get("apply") is True,
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
