"""workflows: part 4 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.db import get_session
from app.kernel import (
    audit,
)
from app.kernel import workflows as wf_service
from app.web.deps import (
    _actor_email,
    _admin_block,
    _error_page,
    _ip,
)
from app.web.workflows_part2 import _prompt_template_or_404

router = APIRouter()


@router.post("/templates/prompts/{template_id}/delete")
def delete_prompt_template(
    template_id: int, request: Request, session: Session = Depends(get_session)
):
    """Delete a prompt template. Admin-only (_admin_block). FAILS CLOSED (409) while
    any workflow step still pins it; otherwise a plain hard delete (it is a leaf)."""
    # Customer ring BEFORE the admin gate (see delete_workflow): a bound caller's
    # foreign template is a 404, not a 403 that would leak its existence.
    tmpl, blocked = _prompt_template_or_404(request, session, template_id, write=True)
    if blocked:
        return blocked
    _, blocked = _admin_block(request)
    if blocked:
        return blocked
    name = tmpl.name
    try:
        wf_service.delete_prompt_template(session, template_id)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 409, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="prompt_template.delete",
        target=f"prompt_template {template_id} '{name}'",
        ip=_ip(request),
    )
    return RedirectResponse("/workflows/building-blocks", status_code=303)
