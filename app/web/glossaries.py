"""The dedicated Glossary module (Phase 5-B) — the /glossaries sidebar tab.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _manage_gate, _customers, _valid_customer_id,
_visible_or_404, _selected_customer, glossaries_index, glossary_detail, glossary_export,
glossary_set_content_type, glossary_rename, glossary_delete, glossary_approve, glossary_unapprove,
glossary_set_pairs, _dnt_bool, _meta_form, glossary_add_term, glossary_edit_term,
glossary_remove_term, glossary_add_sense, _redir, glossary_add_concept, glossary_edit_concept,
glossary_delete_concept, glossary_add_concept_term, glossary_edit_concept_term,
glossary_delete_concept_term, glossary_set_audience, glossary_import, glossary_build, _degraded_qs
from app.web.glossaries_approval_can_tune_ai.

Consolidates ALL glossary management: list + create/rename/delete, CSV import + the
LLM builder, term editing (add/remove terms + DNT, context senses), APPROVAL
(draft <-> approved) and LANGUAGE-PAIR association. Approved glossaries whose pairs
cover a workflow step's language pair then appear in the step's glossary dropdown.

Scoping (repo law): the list + every detail action honours the same org (multi-org)
+ R2-3 customer confinement as the rest of the app — a glossary is fetched via
glossary_module.visible(...) so a foreign org's/customer's glossary 404s.

Role gates (documented choice):
  * CONTENT editing (create/rename/delete/import/term+pair edits) — can_tune_ai
    (admin or linguist): glossaries are the linguist's home turf, matching how the
    workflow-step editor is gated.
  * APPROVAL (approve/unapprove) — can_tune_ai (admin or linguist), same gate as
    content editing. Per the maintainer: linguists manage both TM and glossaries without
    a manager approval step; the DRAFT/APPROVED state itself is unchanged (an
    edit still reverts an approved glossary to draft), only WHO may approve
    widened. (Previously gated on can_manage; widened 2026-07.)

Documented in: docs/architecture/tm-and-glossary.md
"""

from copy import copy as _srp_copy

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    glossary_tbx,
    ids,
    languages_catalog,
    security,
)
from app.kernel import (
    content_types as ctsvc,
)
from app.kernel import (
    glossary as gloss_engine,
)
from app.kernel import (
    glossary_concepts as gc,
)
from app.kernel import (
    glossary_module as gm,
)
from app.kernel import (
    term_extraction as tex,
)
from app.models import Customer
from app.web.deps import (
    _actor_email,
    _ai_block,
    _error_page,
    _ip,
    _my_customer_id,
    _my_org_id,
    offload_parse,
    read_capped_upload,
    templates,
)
from app.web.form_errors import redirect_with_error
from app.web.glossaries_approval_can_tune_ai import (
    _customers,
    _degraded_qs,
    _dnt_bool,
    _manage_gate,
    _meta_form,
    _redir,
    _selected_customer,
    _valid_customer_id,
    _visible_or_404,
    glossaries_index,
    glossary_add_concept,
    glossary_add_concept_term,
    glossary_add_sense,
    glossary_add_term,
    glossary_approve,
    glossary_build,
    glossary_delete,
    glossary_delete_concept,
    glossary_delete_concept_term,
    glossary_detail,
    glossary_edit_concept,
    glossary_edit_concept_term,
    glossary_edit_term,
    glossary_export,
    glossary_import,
    glossary_remove_term,
    glossary_rename,
    glossary_set_audience,
    glossary_set_content_type,
    glossary_set_pairs,
    glossary_unapprove,
)
from app.web.glossaries_approval_can_tune_ai import router as glossaries_approval_can_tune_ai_router

__all__ = [
    "Customer",
    "File",
    "JSONResponse",
    "Response",
    "UploadFile",
    "_customers",
    "_degraded_qs",
    "_dnt_bool",
    "_manage_gate",
    "_meta_form",
    "_redir",
    "_selected_customer",
    "_valid_customer_id",
    "_visible_or_404",
    "access",
    "config",
    "ctsvc",
    "gc",
    "gloss_engine",
    "glossaries_index",
    "glossary_add_concept",
    "glossary_add_concept_term",
    "glossary_add_sense",
    "glossary_add_term",
    "glossary_approve",
    "glossary_build",
    "glossary_delete",
    "glossary_delete_concept",
    "glossary_delete_concept_term",
    "glossary_detail",
    "glossary_edit_concept",
    "glossary_edit_concept_term",
    "glossary_edit_term",
    "glossary_export",
    "glossary_import",
    "glossary_remove_term",
    "glossary_rename",
    "glossary_set_audience",
    "glossary_set_content_type",
    "glossary_set_pairs",
    "glossary_tbx",
    "glossary_unapprove",
    "languages_catalog",
    "read_capped_upload",
    "select",
    "templates",
]

router = APIRouter()


_srp_piece_router = glossaries_approval_can_tune_ai_router
glossaries_approval_can_tune_ai_router = _srp_copy(glossaries_approval_can_tune_ai_router)
glossaries_approval_can_tune_ai_router.routes = glossaries_approval_can_tune_ai_router.routes[0:1]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(glossaries_approval_can_tune_ai_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
glossaries_approval_can_tune_ai_router = _srp_piece_router


@router.post("/glossaries")
def glossary_create(
    request: Request,
    name: str = Form(""),
    customer_id: str = Form(""),
    content_type_id: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    my_cust = _my_customer_id(request)
    try:
        # Customer-first: every glossary belongs to a customer. A
        # bound caller is forced to its own; a staff caller MUST pick one (a blank
        # customer -> require_customer raises). No "global" option anymore.
        cust = my_cust if my_cust is not None else _valid_customer_id(session, request, customer_id)
        g = gm.create(
            session,
            name,
            org_id=_my_org_id(request),
            customer_id=cust,
            created_by=_actor_email(request),
            content_type_id=content_type_id,
            require_customer=True,
        )
    except gm.GlossaryModuleError as exc:
        back = f"/glossaries?customer={customer_id}" if customer_id else "/glossaries"
        return redirect_with_error(back, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="glossary.create",
        target=f"glossary {g.id} '{g.name}'",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{g.id}", status_code=303)


@router.post("/glossaries/{glossary_id}/assign-customer")
def glossary_assign_customer(
    glossary_id: int,
    request: Request,
    customer_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """One-time assignment of a LEGACY unassigned glossary to a customer (can_manage,
    audited). Assignment is only ever FROM NULL — never a reassignment between customers
    (the service enforces the from-NULL invariant; term/TM provenance is bound to the
    owner). A bad/foreign customer id or an already-owned glossary is a clean redirect."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(
            request, 403, "Assigning a glossary to a customer is limited to managers and admins."
        )
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        cid = _valid_customer_id(session, request, customer_id)
        if cid is None:
            raise gm.GlossaryModuleError("choose a customer to assign this glossary to.")
        gm.assign_customer(session, glossary_id, cid)
    except gm.GlossaryModuleError as exc:
        return redirect_with_error(f"/glossaries/{glossary_id}", str(exc))
    audit.log(
        actor=_actor_email(request),
        action="glossary.assign_customer",
        target=f"glossary {glossary_id} -> customer {cid}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


_srp_piece_router = glossaries_approval_can_tune_ai_router
glossaries_approval_can_tune_ai_router = _srp_copy(glossaries_approval_can_tune_ai_router)
glossaries_approval_can_tune_ai_router.routes = glossaries_approval_can_tune_ai_router.routes[1:22]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(glossaries_approval_can_tune_ai_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
glossaries_approval_can_tune_ai_router = _srp_piece_router


@router.post("/glossaries/{glossary_id}/extract")
async def glossary_extract(
    glossary_id: int,
    request: Request,
    document_id: str = Form(""),
    target_locale: str = Form(""),
    source_locale: str = Form(""),
    session: Session = Depends(get_session),
):
    """Launch a two-stage term extraction from a chosen document into the review grid
    (research Option C). The document must be in the glossary's visible scope (its
    customer's projects) — a forged/foreign id is a clean error. Candidates land PENDING;
    nothing is auto-added. Spend-capped: a blocked run refuses cleanly with no partial
    candidates."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    did = ids.coerce_id(document_id)
    if did is None:
        return redirect_with_error(f"/glossaries/{glossary_id}", "pick_document")
    doc = tex.visible_document(
        session, g, did, org_id=_my_org_id(request), user=security.session_user(request)
    )
    if doc is None:
        return redirect_with_error(f"/glossaries/{glossary_id}", "document_not_found")
    # Spend-capped multi-call LLM extraction: off the event loop (#495).
    try:
        summary = await offload_parse(
            tex.extract,
            session,
            g,
            (target_locale or "").strip(),
            document=doc,
            source_locale=(source_locale or "").strip(),
        )
    except tex.ExtractionEngineUnavailable:
        return redirect_with_error(f"/glossaries/{glossary_id}", "engine_unavailable")
    if summary.get("blocked"):
        return redirect_with_error(f"/glossaries/{glossary_id}", "spend_cap")
    audit.log(
        actor=_actor_email(request),
        action="glossary.extract",
        target=f"glossary {glossary_id} <- doc {doc.id}",
        ip=_ip(request),
    )
    return RedirectResponse(
        f"/glossaries/{glossary_id}?gl_proposed={summary['proposed']}"
        f"{_degraded_qs(summary)}#candidates",
        status_code=303,
    )


@router.post("/glossaries/{glossary_id}/candidates/{candidate_id}/accept")
async def glossary_candidate_accept(
    glossary_id: int,
    candidate_id: int,
    request: Request,
    target_term: str = Form(""),
    session: Session = Depends(get_session),
):
    """Accept a candidate into this glossary as a real term (customer-bound; reverts an
    approved glossary to draft). `target_term` overrides the proposed translation."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    # A blank submitted target_term means "use the proposed translation" (pass None).
    override = target_term if (target_term or "").strip() else None
    entry = tex.accept(
        session,
        glossary_id,
        candidate_id,
        decided_by=_actor_email(request),
        target_term=override,
        user=security.session_user(request),  # #541
    )
    if entry is None:
        return redirect_with_error(
            f"/glossaries/{glossary_id}", "candidate_not_pending", anchor="candidates"
        )
    audit.log(
        actor=_actor_email(request),
        action="glossary.candidate_accept",
        target=f"glossary {glossary_id} candidate {candidate_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}#candidates", status_code=303)


@router.post("/glossaries/{glossary_id}/candidates/{candidate_id}/drop")
async def glossary_candidate_drop(
    glossary_id: int, candidate_id: int, request: Request, session: Session = Depends(get_session)
):
    """Drop a candidate. It persists as dropped so re-extraction never re-proposes it."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    tex.drop(session, glossary_id, candidate_id, decided_by=_actor_email(request))
    audit.log(
        actor=_actor_email(request),
        action="glossary.candidate_drop",
        target=f"glossary {glossary_id} candidate {candidate_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}#candidates", status_code=303)


@router.post("/glossaries/{glossary_id}/candidates/batch")
async def glossary_candidates_batch(
    glossary_id: int,
    request: Request,
    action: str = Form(""),
    candidate_id: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    """Batch accept/drop the selected candidates (checkbox list)."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    if action not in ("accept", "drop"):
        return RedirectResponse(f"/glossaries/{glossary_id}#candidates", status_code=303)
    res = tex.batch(
        session,
        glossary_id,
        candidate_id,
        action,
        decided_by=_actor_email(request),
        user=security.session_user(request),  # #541
    )
    audit.log(
        actor=_actor_email(request),
        action=f"glossary.candidate_batch_{action}",
        target=f"glossary {glossary_id} ({res['accepted']}a/{res['dropped']}d)",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}#candidates", status_code=303)
