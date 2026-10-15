"""Setup AI: the judge-engine Inspector page, its try-it call, and the one-click
"Create a Jev engine from TYPESAFE_API_KEY" import.

ADMIN-ONLY (_admin_block), stricter than the rest of Setup AI: a try can spend money on
hosted Jev and taps the scored text to the Ledger, and the import moves an install
credential into an engine. Org-scoped like every Setup AI engine route: a foreign,
missing or non-judge engine id is the same 404. The logic lives in
app/services/jev_inspector.py; the /api, CLI and MCP twins call the same functions.

Documented in: docs/architecture/engines-and-providers.md
"""

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.kernel import jev_inspector, security
from app.web.deps import _admin_block, _error_page, _ip, _my_org_id, templates

router = APIRouter()


def can_inspect(me) -> bool:
    """The same rung _admin_block enforces, for deciding whether Setup AI shows the
    Inspect buttons and the env-import offer at all."""
    return not security.auth_required() or security.is_admin(me)


@router.get("/setup-ai/engines/{eid}/inspect")
def setup_ai_engine_inspect(eid: int, request: Request):
    """What this judge is asked (criteria + the exact request body), how it decides
    (the rule rendered from the real constants), a try-it form, and the Ledger link."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    prof = jev_inspector.owned_judge(eid, _my_org_id(request))
    if prof is None:
        return _error_page(request, 404, "Not found.")
    return templates.TemplateResponse(
        request,
        "jev_inspect.html",
        {"request": request, "data": jev_inspector.inspect_payload(prof)},
    )


@router.post("/setup-ai/engines/{eid}/inspect/try")
def setup_ai_engine_inspect_try(
    eid: int,
    request: Request,
    source: str = Form(""),
    target: str = Form(""),
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    threshold: str = Form(""),
):
    """Score one segment through this judge (JSON for the page's Try button)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    org = _my_org_id(request)
    prof = jev_inspector.owned_judge(eid, org)
    if prof is None:
        return JSONResponse({"ok": False, "error": "No such engine."}, status_code=404)
    try:
        out = jev_inspector.try_it(
            prof,
            source,
            target,
            source_locale,
            target_locale,
            threshold,
            caller="web:" + (getattr(me, "email", "") or "system"),
            org_id=org,
        )
    except jev_inspector.TryBusy as exc:
        return busy_response(exc)
    except jev_inspector.InspectError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(out)


def busy_response(exc) -> JSONResponse:
    """The 429 for a refused try (rate or in-flight), with Retry-After guidance."""
    return JSONResponse(
        {"ok": False, "error": str(exc), "retry_after": exc.retry_after},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


@router.post("/setup-ai/jev/import-env")
def setup_ai_jev_import_env(request: Request):
    """Create a hosted Jev engine from TYPESAFE_API_KEY (idempotent; the key is never
    echoed). Redirects back to Setup AI's Quality judges group."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        _prof, created = jev_inspector.import_env(
            actor=getattr(me, "email", "system"), org_id=_my_org_id(request), ip=_ip(request)
        )
    except jev_inspector.InspectError as exc:
        return RedirectResponse(
            "/setup-ai?engine_error=" + quote(str(exc)) + "#quality-judges", status_code=303
        )
    suffix = (
        "engine_saved=1"
        if created
        else "engine_warning="
        + quote("A hosted Jev engine already exists, so nothing was imported.")
    )
    return RedirectResponse("/setup-ai?" + suffix + "#quality-judges", status_code=303)
