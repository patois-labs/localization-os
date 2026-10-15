"""/api/system-prompts: the pipeline system prompts, and org-level (phase 2a) and per-workflow
(phase 2b) overrides of two editable heads (GitHub #356).

  GET    /api/system-prompts               catalogue + the editable heads for the caller's org
  GET    /api/system-prompts/{key}         one editable head with its version history
  PUT    /api/system-prompts/{key}         save a new version      (admin)
  DELETE /api/system-prompts/{key}         reset to the built-in   (admin)
  POST   /api/system-prompts/{key}/restore copy an old version     (admin)

Reads use the Building blocks audience (can_tune_ai). Writes are ADMIN only and only while the
`system_prompt_overrides` feature flag is on (409 naming the switch otherwise). Every write is
audit-logged by the service. The org always comes from the caller's key, never the body.

PER WORKFLOW (phase 2b), the same two heads set on one workflow (resolution: workflow override >
org override > built-in):

  GET    /api/workflows/{wid}/system-prompts                 both heads as they apply to it
  GET    /api/workflows/{wid}/system-prompts/{key}           one head with its version history
  GET    /api/workflows/{wid}/system-prompts/{key}/history   the version history alone
  PUT    /api/workflows/{wid}/system-prompts/{key}           save a new version of its own text
  DELETE /api/workflows/{wid}/system-prompts/{key}           inherit again (history kept)
  POST   /api/workflows/{wid}/system-prompts/{key}/restore   copy an old version as the newest
  POST   /api/workflows/{wid}/system-prompts/{key}/restore-default   same as DELETE (#630, for MCP)

WHO MAY EDIT: an ADMIN or a MANAGER (#630, 2026-10-08; the org-level heads stay admin
only) whose org and customer ring contain that workflow (`_wf_in_org`; a foreign or missing
workflow is a 404). Reads: admin, manager or linguist (can_manage or can_tune_ai) within the same
org and customer ring. The predicates are system_prompts.can_edit_workflow_heads / _read_.

Its own small router, appended at the END of the /api router assembly in app/web/api_cli.py
(like the page-layouts api_router), because the SRP-split misc routers there are included by
fixed slice index: adding a route in the middle of one of them silently drops a neighbour.

Documented in: docs/architecture/workflow-engine.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import security, system_prompts
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _ai_block,
    _gate,
    _json,
    _manage_block,
    _role_block,
    _shared_write_block,
    _wf_in_org,
)

api_router = APIRouter()


def _can_edit(ident, workflow: bool = False) -> bool:
    """True when this caller may write overrides: an admin (for one workflow's heads also a
    manager, #630) or the login wall is off, while the feature flag is on. Reported to the page
    so the editor only shows when it can work."""
    if not system_prompts.overrides_enabled():
        return False
    if not security.auth_required():
        return True
    user = (ident or {}).get("user")
    if workflow:
        return system_prompts.can_edit_workflow_heads(user)
    return security.is_admin(user)


def _read_gate(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return None, None, blocked
    return ident, org_id, _ai_block(ident)


def _write_gate(request: Request, key: str):
    """(ident, org_id, blocked) for a write: key must be editable (404 otherwise), caller an
    admin, flag on (409 naming the switch)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return None, None, blocked
    if key not in system_prompts.EDITABLE_KEYS:
        return None, None, JSONResponse(_NOT_FOUND, status_code=404)
    blocked = _admin_block(ident)
    if blocked:
        return None, None, blocked
    if not system_prompts.overrides_enabled():
        return (
            None,
            None,
            JSONResponse({"ok": False, "error": system_prompts.OFF_MESSAGE}, status_code=409),
        )
    return ident, org_id, None


@api_router.get("/api/system-prompts")
def api_system_prompts_list(request: Request):
    """The pipeline LLM SYSTEM prompts (the built-in text; `effective_text` is what the
    caller's org sends once an override is in force) plus the two editable heads. Mirrors the
    Building blocks > System prompts section and `workflow prompt system`. Same audience as
    the Building blocks page: can_tune_ai. The product-assistant prompts are deliberately not
    listed. `overrides_enabled` is the feature flag; `can_edit` is true only for an admin
    while it is on."""
    ident, org_id, blocked = _read_gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        payload = system_prompts.catalogue_payload(s, org_id)
    return JSONResponse({"ok": True, "can_edit": _can_edit(ident), **payload})


@api_router.get("/api/system-prompts/{key}")
def api_system_prompts_detail(key: str, request: Request):
    """One editable head: the built-in default, the org's current override, the locked floor
    and the version history (newest first). can_tune_ai, read-only."""
    ident, org_id, blocked = _read_gate(request)
    if blocked:
        return blocked
    if key not in system_prompts.EDITABLE_KEYS:
        return JSONResponse(_NOT_FOUND, status_code=404)
    with Session(engine) as s:
        prompt = system_prompts.editable_detail(s, org_id, key)
    return JSONResponse(
        {
            "ok": True,
            "overrides_enabled": system_prompts.overrides_enabled(),
            "can_edit": _can_edit(ident),
            "prompt": prompt,
        }
    )


@api_router.put("/api/system-prompts/{key}")
async def api_system_prompts_save(key: str, request: Request):
    """Save `{"body": "..."}` as the newest version of an editable head. Admin only, flag on.
    Text problems are 400; a missing 'output only the result' instruction is a WARNING in the
    200 response, not a refusal."""
    ident, org_id, blocked = _write_gate(request, key)
    if blocked:
        return blocked
    body = await _json(request)
    raw = body.get("body")
    if not isinstance(raw, str):
        return JSONResponse({"ok": False, "error": "body must be a string."}, status_code=400)
    with Session(engine) as s:
        try:
            out = system_prompts.save_override(s, org_id, key, raw, ident["actor"])
        except system_prompts.OverrideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


@api_router.delete("/api/system-prompts/{key}")
def api_system_prompts_reset(key: str, request: Request):
    """Reset an editable head to the built-in text (history is kept). Admin only, flag on.
    `changed` is false when no override was in force."""
    ident, org_id, blocked = _write_gate(request, key)
    if blocked:
        return blocked
    with Session(engine) as s:
        try:
            out = system_prompts.reset_override(s, org_id, key, ident["actor"])
        except system_prompts.OverrideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


@api_router.post("/api/system-prompts/{key}/restore")
async def api_system_prompts_restore(key: str, request: Request):
    """Restore `{"version": N}`: save a copy of that older version as the newest. Admin only,
    flag on."""
    ident, org_id, blocked = _write_gate(request, key)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        try:
            out = system_prompts.restore_override(
                s, org_id, key, body.get("version"), ident["actor"]
            )
        except system_prompts.OverrideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


# --- per-workflow overrides (phase 2b) --------------------------------------------------
def _wf_gate(request: Request, wid: int, key: str | None = None, write: bool = False):
    """(ident, org_id, blocked) for a per-workflow call: docs-scoped caller, the workflow in the
    caller's org and customer ring (404 otherwise, BEFORE the role gate), a read: admin, manager
    or linguist; a write: admin or manager (#630), else 403; the key editable (404), and for a
    write the feature flag on (409 naming the switch)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return None, None, blocked
    # Ring BEFORE role: a customer-bound caller's FOREIGN (or missing) workflow is a 404, never
    # the 403 an admin-only write would otherwise return for an id that exists (an existence
    # oracle). Past the ring the role gate still refuses (admin or manager for a write; admin,
    # manager or linguist for a read).
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
    if wf is None:
        return None, None, JSONResponse(_NOT_FOUND, status_code=404)
    if write:
        # #468: a global workflow is read-only for a customer-bound caller.
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return None, None, blocked
    # Write: admin or manager (_manage_block = security.can_manage, the predicate behind
    # system_prompts.can_edit_workflow_heads). Read: that, or can_tune_ai (linguists).
    if write:
        blocked = _manage_block(ident)
    else:
        blocked = _role_block(ident, system_prompts.can_read_workflow_heads)
    if blocked:
        return None, None, blocked
    if key is not None and key not in system_prompts.EDITABLE_KEYS:
        return None, None, JSONResponse(_NOT_FOUND, status_code=404)
    if write and not system_prompts.overrides_enabled():
        return (
            None,
            None,
            JSONResponse({"ok": False, "error": system_prompts.OFF_MESSAGE}, status_code=409),
        )
    return ident, org_id, None


@api_router.get("/api/workflows/{wid}/system-prompts")
def api_workflow_system_prompts(wid: int, request: Request):
    """Both editable heads as they apply to this workflow: its own override (or none =
    inherit), the org's override it would inherit, which source wins now, the locked floor."""
    ident, org_id, blocked = _wf_gate(request, wid)
    if blocked:
        return blocked
    with Session(engine) as s:
        payload = system_prompts.workflow_payload(s, org_id, wid)
    return JSONResponse({"ok": True, "can_edit": _can_edit(ident, workflow=True), **payload})


@api_router.get("/api/workflows/{wid}/system-prompts/{key}")
def api_workflow_system_prompt_detail(wid: int, key: str, request: Request):
    """One editable head for a workflow with its own version history (newest first)."""
    ident, org_id, blocked = _wf_gate(request, wid, key)
    if blocked:
        return blocked
    with Session(engine) as s:
        prompt = system_prompts.workflow_editable_detail(s, org_id, wid, key)
    return JSONResponse(
        {
            "ok": True,
            "overrides_enabled": system_prompts.overrides_enabled(),
            "can_edit": _can_edit(ident, workflow=True),
            "prompt": prompt,
        }
    )


@api_router.get("/api/workflows/{wid}/system-prompts/{key}/history")
def api_workflow_system_prompt_history(wid: int, key: str, request: Request):
    """The version history of this workflow's own text for a head, newest first. Readable with
    the flag off (history stays inspectable)."""
    _ident, org_id, blocked = _wf_gate(request, wid, key)
    if blocked:
        return blocked
    with Session(engine) as s:
        history = system_prompts.workflow_editable_detail(s, org_id, wid, key)["history"]
    return JSONResponse({"ok": True, "workflow_id": wid, "key": key, "history": history})


@api_router.put("/api/workflows/{wid}/system-prompts/{key}")
async def api_workflow_system_prompt_save(wid: int, key: str, request: Request):
    """Save `{"body": "..."}` as the newest version of this workflow's own head text. Same
    validation and warnings as the org level. Flag on."""
    ident, org_id, blocked = _wf_gate(request, wid, key, write=True)
    if blocked:
        return blocked
    body = await _json(request)
    raw = body.get("body")
    if not isinstance(raw, str):
        return JSONResponse({"ok": False, "error": "body must be a string."}, status_code=400)
    with Session(engine) as s:
        try:
            out = system_prompts.save_workflow_override(s, org_id, wid, key, raw, ident["actor"])
        except system_prompts.OverrideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


@api_router.delete("/api/workflows/{wid}/system-prompts/{key}")
def api_workflow_system_prompt_reset(wid: int, key: str, request: Request):
    """Inherit again: the workflow stops using its own text (history is kept) and falls back
    to the org's, then the built-in text. `changed` is false when it already inherited."""
    ident, org_id, blocked = _wf_gate(request, wid, key, write=True)
    if blocked:
        return blocked
    with Session(engine) as s:
        try:
            out = system_prompts.reset_workflow_override(s, org_id, wid, key, ident["actor"])
        except system_prompts.OverrideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


@api_router.post("/api/workflows/{wid}/system-prompts/{key}/restore")
async def api_workflow_system_prompt_restore(wid: int, key: str, request: Request):
    """Restore `{"version": N}` of this workflow's own text as the newest version. Flag on."""
    ident, org_id, blocked = _wf_gate(request, wid, key, write=True)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        try:
            out = system_prompts.restore_workflow_override(
                s, org_id, wid, key, body.get("version"), ident["actor"]
            )
        except system_prompts.OverrideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})


@api_router.post("/api/workflows/{wid}/system-prompts/{key}/restore-default")
def api_workflow_system_prompt_restore_default(wid: int, key: str, request: Request):
    """Restore default prompt (#630): exactly what DELETE does (the workflow drops its own text,
    history kept, and the prompt it inherits applies again: base workflow, org, else the built-in
    text), as a POST so an MCP tool can reach it (DELETE routes are never toolified). Same gate,
    flag and audit row (workflow.system_prompt_reset) as the DELETE. `changed` is false when it
    already inherited."""
    return api_workflow_system_prompt_reset(wid, key, request)
