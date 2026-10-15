"""The Enterprise features panel (/settings/enterprise) -- the ONE web door that turns the
six flag-OFF + REMOTE_IMMUTABLE sprint features (OTel / metrics / SAML / SCIM / PII-NER /
durable job queue) plus the S3 chain on and off.

AUTH MODEL (two rungs):
  * VIEW (GET, "Run preflight" POST): admin SESSION -- the same `_admin_block` gate the
    rest of the /settings surface uses.
  * ENABLE / DISABLE (POST): admin session PLUS an OPERATOR-KEY step-up -- the operator key
    is verified against the existing `operator_key_hash` (SHA-256), entered PER ACTION in
    the form and NEVER stored in the session. When no `operator_key_hash` is set the panel
    is READ-ONLY (status + preflight visible) and shows the one-liner to mint one.

CSRF: every POST is CSRF-protected exactly like the sibling admin forms (the global
_csrf_ok middleware governs it when the login wall is on; the templates carry _csrf.html).

NO /api, NO MCP. This is a host-console web surface only. Preflight results are an
infrastructure oracle, so there is deliberately no machine-readable route for them. All
logic lives in app/services/enterprise_enablement.py; this module is a thin caller.

Documented in: docs/architecture/configuration.md
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.kernel import enterprise_enablement as ee
from app.kernel import settings
from app.web.deps import _actor_email, _admin_block, _error_page, _ip, templates
from app.web.form_errors import redirect_with_error

router = APIRouter()


def _cards():
    """Render one view-model per feature: state, description, config fields (current
    values, secrets shown only as set/unset), and the OFFLINE preflight (panel load stays
    fast + offline-clean; the network probes run on the 'Run preflight' button)."""
    from app.kernel import settings

    cards = []
    for f in ee.FEATURES:
        pf = ee.preflight(f.key, run_network=False)
        fields = []
        for fld in f.fields:
            if fld.kind == "secret":
                # Never echo a stored secret -- only whether one is set.
                is_set = bool(settings.read_secret_setting(fld.setting_key))
                fields.append(
                    {
                        "form_name": fld.form_name,
                        "label": fld.label,
                        "kind": fld.kind,
                        "value": "",
                        "is_set": is_set,
                        "placeholder": fld.placeholder,
                        "help": fld.help,
                    }
                )
            else:
                fields.append(
                    {
                        "form_name": fld.form_name,
                        "label": fld.label,
                        "kind": fld.kind,
                        "value": settings.get(fld.setting_key, "") or "",
                        "is_set": bool(settings.get(fld.setting_key)),
                        "placeholder": fld.placeholder,
                        "help": fld.help,
                    }
                )
        cards.append(
            {
                "key": f.key,
                "title": f.title,
                "description": f.description,
                "enabled": f.is_enabled(),
                "setting_key": f.setting_key,
                "fields": fields,
                "preflight": pf,
            }
        )
    return cards


@router.get("/settings/enterprise")
def enterprise_page(request: Request):
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    net_pf = _consume_flash_preflight(request)
    return templates.TemplateResponse(
        request,
        "enterprise.html",
        {
            "request": request,
            "cards": _cards(),
            "operator_configured": ee.operator_key_configured(),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
            "net_preflight": net_pf,
        },
    )


def _consume_flash_preflight(request: Request):
    """Pull a one-shot 'Run preflight' result out of the session (set by the preflight POST
    then rendered once on the redirect target). Session-flash so a network result is not
    re-run on every page load and the URL stays clean."""
    try:
        data = request.session.pop("enterprise_net_pf", None)
    except Exception:
        data = None
    return data


@router.post("/settings/enterprise/preflight")
def enterprise_preflight(request: Request, feature: str = Form(...)):
    """Run the FULL preflight (incl. bounded live-network probes) for one feature. Admin
    session only -- this is a READ (no write), so it does NOT require the operator key.
    Result is flashed into the session and shown on the redirect target."""
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        pf = ee.preflight(feature, run_network=True)
    except ee.EnterpriseError:
        return _error_page(request, 404, "Not found.")
    request.session["enterprise_net_pf"] = {
        "feature": pf.feature,
        "ok": pf.ok,
        "network_ran": pf.network_ran,
        "checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "network": c.network}
            for c in pf.checks
        ],
    }
    return RedirectResponse(
        f"/settings/enterprise?saved=preflight#feature-{pf.feature}", status_code=303
    )


def _step_up_or_redirect(request, me, feature: str, operator_key: str):
    """The enable/disable step-up gate. Returns (feature_obj, None) on success, or
    (None, RedirectResponse) with an error to surface. Refuses when no operator key is
    configured (read-only panel) or the supplied key is wrong -- neither reveals whether
    the feature exists beyond the admin who already passed _admin_block.

    A REFUSAL IS AUDITED (gate round 1, probe B). The operator key is the one credential
    standing between an admin session and turning SSO / SCIM / data-topology switches on,
    and a refused attempt used to leave no trace at all -- so repeated guessing against it
    was invisible. The row records WHO and WHICH feature, never the supplied value."""
    from urllib.parse import quote

    if not ee.operator_key_configured():
        _audit_refusal(request, feature, "no operator key configured (panel is read-only)")
        return None, RedirectResponse(
            "/settings/enterprise?error="
            + quote(
                "This panel is read-only: no operator key is configured. Mint one on the "
                "host with `config set operator_key_hash <sha256hex>`."
            ),
            status_code=303,
        )
    if not ee.verify_operator_key(operator_key or ""):
        _audit_refusal(request, feature, "operator key did not match")
        return None, RedirectResponse(
            "/settings/enterprise?error=" + quote("Operator key does not match."),
            status_code=303,
        )
    try:
        f = ee.feature(feature)
    except ee.EnterpriseError:
        return None, _error_page(request, 404, "Not found.")
    return f, None


def _audit_refusal(request, feature: str, reason: str) -> None:
    """One audit row per REFUSED step-up. Never records the supplied key (not even a hash of
    it -- a hash of a guess is still a guess to replay against). ``feature`` is echoed back
    from the form, so it is bounded to a known key or reported as unknown rather than
    written through verbatim."""
    from app.kernel import audit

    known = feature if feature in ee.feature_keys() else "(unknown feature)"
    audit.log(
        actor=_actor_email(request) or "system",
        action="enterprise.step_up_refused",
        target=known,
        detail=reason,
        ip=_ip(request),
    )


@router.post("/settings/enterprise/enable")
async def enterprise_enable(request: Request):
    """Enable one feature -- or, when it is already on, SAVE its configuration (the field
    form renders in both states, so an IdP cert rotation never needs an SSO outage). Admin
    session + operator-key step-up either way. The boot-brick preflight gates the write; a
    'force' checkbox overrides a red preflight. Config fields come straight off the form
    (per-feature declared fields only)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    form = await request.form()
    feature = (form.get("feature") or "").strip()
    operator_key = form.get("operator_key") or ""
    force = (form.get("force") or "") == "1"
    f, redir = _step_up_or_redirect(request, me, feature, operator_key)
    if redir is not None:
        return redir
    # Collect ONLY this feature's declared field values from the form.
    proposed = {}
    for fld in f.fields:
        if fld.form_name in form:
            proposed[fld.form_name] = form.get(fld.form_name)
    try:
        # The operator-key step-up above IS the host-operator credential, so this write is
        # exempt from the request-door multi-org refusal (settings.host_operator_scope).
        with settings.host_operator_scope():
            res = ee.enable(
                f.key,
                actor=_actor_email(request) or "system",
                proposed=proposed,
                force=force,
                run_network=False,
                ip=_ip(request),
            )
    except ee.EnterpriseError as exc:
        return redirect_with_error(
            "/settings/enterprise", str(exc), anchor=f"feature-{f.key}", param="error"
        )
    msg = f"{f.title} enabled -- restart the server for it to take effect."
    if res.action == "noop":
        # Already on: this submit was a CONFIGURATION save (the field form renders while the
        # feature is on, so an IdP cert rotation never needs an SSO outage). Say so.
        msg = (
            f"{f.title} configuration saved -- restart the server for it to take effect."
            if proposed
            else f"{f.title} was already on."
        )
    return RedirectResponse(
        f"/settings/enterprise?saved={quote(msg)}#feature-{f.key}", status_code=303
    )


@router.post("/settings/enterprise/disable")
async def enterprise_disable(request: Request):
    """Disable one feature. Admin session + operator-key step-up. NEVER runs a preflight
    (turning off must never be blocked)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    form = await request.form()
    feature = (form.get("feature") or "").strip()
    operator_key = form.get("operator_key") or ""
    f, redir = _step_up_or_redirect(request, me, feature, operator_key)
    if redir is not None:
        return redir
    with settings.host_operator_scope():  # the operator-key step-up is the host credential
        res = ee.disable(f.key, actor=_actor_email(request) or "system", ip=_ip(request))
    msg = f"{f.title} disabled -- restart the server for it to take effect."
    if res.action == "noop":
        msg = f"{f.title} was already off."
    return RedirectResponse(
        f"/settings/enterprise?saved={quote(msg)}#feature-{f.key}", status_code=303
    )
