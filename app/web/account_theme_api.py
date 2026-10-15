"""POST /api/account/theme: the caller's own Light / Dark / Match my computer choice (ADR 0063).

The lsu_ token IS the account (same self-service posture as GET /api/account), so there is no
target parameter. Validated at the shared seam (security.UI_THEMES), the same one the web form
(POST /account/theme) and `account set-theme` use, so a bad value is refused identically on
every surface. Rides the api_cli router as a whole-router include (like page_layouts), so the
fixed route slices there are untouched.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.kernel import audit, security
from app.web.api_cli_common import _creator, _gate, _json

api_router = APIRouter()


@api_router.post("/api/account/theme")
async def api_account_set_theme(request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    raw = str(body.get("theme") or body.get("ui_theme") or "").strip().lower()
    if raw not in security.UI_THEMES:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown theme: {!r} (valid: {})".format(
                    raw, ", ".join(security.UI_THEMES)
                ),
            },
            status_code=400,
        )
    u = _creator(ident)
    choice = security.update_theme(u.id, raw)
    audit.log(actor=ident["actor"], action="account.update_theme", detail=choice)
    return JSONResponse({"ok": True, "ui_theme": choice})
