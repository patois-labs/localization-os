"""workflows: part 3 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.kernel import (
    access,
    audit,
    orchestrator,
    security,
)
from app.kernel import workflows as wf_service
from app.models import (
    Workflow,
)
from app.web.deps import (
    _actor_email,
    _admin_block,
    _error_page,
    _ip,
    templates,
)
from app.web.workflows_common import (
    _group_pair_source,
    _group_pair_targets_ctx,
    _grp_or_404,
    _visible_group_variants,
    _visible_workflows,
)

router = APIRouter()


@router.get("/workflow-groups/{group_id}")
def group_detail(group_id: int, request: Request, session: Session = Depends(get_session)):
    grp, blocked = _grp_or_404(request, session, group_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    # Found while fixing R1: the members TABLE listed the raw group_variants, so a
    # customer-A viewer of a GLOBAL group read the NAMES of (and links to) customer-B's
    # variants — rows _wf_or_404 404s if opened, and that add-a-pair must not copy. The
    # page now shows exactly the ring it can act on (a strict no-op with auth off, and
    # for internal staff). Coverage below deliberately still classifies the WHOLE group:
    # it reports what DISPATCH would do, and dispatch applies its own compatibility
    # filter — so it stays honest without naming a foreign variant.
    variants = _visible_group_variants(request, session, group_id)
    # One fetch per variant, reused for both counts (step total + 4b pinned overrides).
    variant_steps = {v.id: orchestrator.get_steps(session, v.id) for v in variants}
    step_counts = {vid: len(sts) for vid, sts in variant_steps.items()}
    # SPEC-workflows-redesign 4b: how many steps in each variant are PINNED (overridden) —
    # deliberately diverged from the base and frozen against base-edit propagation.
    override_counts = {
        vid: sum(1 for st in sts if st.overridden) for vid, sts in variant_steps.items()
    }
    # ungrouped workflows the user may fold into this group (add-to-group picker)
    ungrouped = session.exec(
        access.org_filter(
            select(Workflow).where(Workflow.group_id.is_(None)), Workflow, me
        ).order_by(Workflow.name)
    ).all()
    ungrouped = _visible_workflows(request, session, ungrouped)
    from app.kernel import languages_catalog

    # SPEC-workflows-redesign Phase 4a: coverage. Gather the distinct (source, target)
    # PAIRS of the projects in this group's SCOPE (its customer's projects, or the org's
    # when the group is global) and classify each via the SAME dispatch ladder
    # resolve_variant uses (group_locale_coverage -> _variant_match), matched at each
    # pair's OWN source — so the readout reflects what a real run would dispatch (dispatch
    # is source-sensitive). Derived, no schema. A GLOBAL group's scope is confined to the
    # viewer's own customer when they are customer-bound, so it never aggregates other
    # customers' locales for a bound user (my_cust None for internal staff = org-wide).
    #
    # N1 (re-review): coverage is classified over the VISIBLE members. Reading the raw
    # list picked a foreign representative, which both named/linked another customer's
    # workflows here and reported rungs a run BY THIS VIEWER would never take.
    my_cust = access.current_customer_id(me)
    scope_pairs = wf_service.group_scope_pairs(session, grp, viewer_customer_id=my_cust)
    coverage = wf_service.group_locale_coverage(session, group_id, scope_pairs, variants=variants)
    # Issue #46(3): "Add a language pair" straight from the group page. The pair is copied
    # off the group's DEFAULT variant (else its first member) through the very same
    # copy_to_pairs seam the builder's Copy-to-language-pairs uses, so naming, the group
    # fold-in and the duplicate guard are identical on both surfaces. Targets the group
    # already covers at this source are rendered ticked-and-disabled rather than silently
    # accepted and skipped.
    # R1: the copy SOURCE is picked from the requester-VISIBLE members only, and the page
    # must offer exactly what the POST will accept (see _visible_group_variants).
    locales = languages_catalog.enabled_locales()
    pair_src_wf = _group_pair_source(variants)
    pair_source = (pair_src_wf.source_locale if pair_src_wf else None) or None
    # N5 (re-review): the picker renders `offered` (the customer's active pairs, which may
    # include a code that is NOT in enabled_locales) plus the rest of the enabled set.
    # Compute the covered set over exactly THOSE codes, or a covered-but-not-enabled
    # active pair renders un-ticked and enabled — inviting a submit that is then skipped.
    # Both lists are built here, not in the template, so the two can never disagree.
    pair_ctx = _group_pair_targets_ctx(request, session, grp, pair_source)
    offered = [r["code"] for r in pair_ctx["pair_target_rows"]]
    rest = [loc for loc in locales if loc not in offered]
    # N1: covered-FOR-THIS-VIEWER. A pair only a variant they cannot see covers must stay
    # addable (dispatch would never run that variant for them), so it is not ticked here
    # and copy_to_pairs (given the same viewer) will not refuse it as a duplicate.
    covered_targets = wf_service.group_covered_targets(
        session, group_id, pair_source, offered + rest, variants=variants
    )
    ctx = {
        "request": request,
        "grp": grp,
        "variants": variants,
        "step_counts": step_counts,
        "override_counts": override_counts,
        "ungrouped": ungrouped,
        "coverage": coverage,
        "locales": locales,
        "pair_source": pair_source,
        "pair_src_wf": pair_src_wf,
        "pair_src_pair": (
            wf_service.pair_key(pair_src_wf.source_locale, pair_src_wf.target_locale)
            if pair_src_wf
            else ""
        ),
        "covered_targets": covered_targets,
        "pair_offered": offered,
        "pair_rest": rest,
        "notice": request.query_params.get("notice"),
        "label_for": languages_catalog.label_for,
    }
    ctx.update(pair_ctx)
    return templates.TemplateResponse(request, "workflow_group.html", ctx)


@router.post("/workflow-groups/{group_id}/delete")
def delete_group(group_id: int, request: Request, session: Session = Depends(get_session)):
    """Delete a workflow group. Admin-only (_admin_block). FAILS CLOSED (409) while
    the group still has member workflows — ungrouping is the user's explicit action,
    never a delete side effect."""
    # Customer ring BEFORE the admin gate (see delete_workflow): a bound caller's
    # foreign group is a 404, not a 403 that would leak its existence.
    grp, blocked = _grp_or_404(request, session, group_id, write=True)
    if blocked:
        return blocked
    _, blocked = _admin_block(request)
    if blocked:
        return blocked
    name, customer_id = grp.name, grp.customer_id
    try:
        wf_service.delete_group(session, group_id)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 409, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="workflow_group.delete",
        target=f"workflow_group {group_id} '{name}'",
        ip=_ip(request),
        customer_id=customer_id,
    )
    return RedirectResponse("/workflows", status_code=303)
