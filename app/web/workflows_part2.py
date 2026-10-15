"""workflows: part 2 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    ids,
    orchestrator,
    security,
)
from app.kernel import (
    customers as customer_service,
)
from app.kernel import workflows as wf_service
from app.models import (
    Customer,
    Scope,
    Workflow,
    WorkflowGroup,
)
from app.web.deps import (
    _error_page,
    _my_customer_id,
    _my_org_id,
    _org_block,
    templates,
)
from app.web.workflows_common import (
    _MAX_REVIEW_STEPS,
    _STEP_BOOL_FIELDS,
    _STEP_TEXT_FIELDS,
    _WF_SORT_FIELDS,
    _int_or_none,
    _parse_branch_fields,
    _parse_run_condition_fields,
    _sort_rows,
    _visible_group_variants,
    _visible_prompt_templates,
    _visible_workflows,
    _wf_customer_block,
    _wf_glossary_customer,
    _wf_write_block,
    _workflow_review_customers,
)

router = APIRouter()


# --- workflows (the multi-agent orchestration centerpiece) ---
@router.get("/workflows")
def workflows_index(
    request: Request,
    sort: str = "",
    dir: str = "",
    archived: str = "",
    session: Session = Depends(get_session),
):
    """The management DASHBOARD (SPEC Workflow Studio Wave A): one lean table over
    every standalone workflow + each multilingual GROUP collapsed to a single row
    (its variant count + aggregate stats). Each row carries single-sourced usage +
    quality stats from workflow_stats (spend visible to ALL roles that can see the
    list — the owner's explicit call). Row click -> the per-workflow builder (an
    ungrouped workflow) or the group page (a group). Create moved to /workflows/new."""
    from app.kernel import workflow_shapes as wshapes
    from app.kernel import workflow_stats

    # Multi-org: only the caller's own org's workflows (leak vector 12); a GLOBAL-scope
    # workflow of another org must NOT be surfaced. No-op off.
    me = security.session_user(request)
    wq = access.org_filter(select(Workflow), Workflow, me)
    # Archive filter (residual sweep: archive verbs). DEFAULT = ACTIVE only (archived
    # workflows drop out of the default dashboard, like the customer/project precedent).
    # `archived=only` shows just the archived; `archived=all` shows both.
    archived_filter = (archived or "").strip().lower()
    if archived_filter == "only":
        wq = wq.where(Workflow.archived_at.is_not(None))
    elif archived_filter == "all":
        pass
    else:
        archived_filter = "active"
        wq = wf_service.workflow_active_only(wq)  # shipped seam (M4)
    cq = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    gq = access.org_filter(select(WorkflowGroup), WorkflowGroup, me).order_by(
        WorkflowGroup.created_at.desc()
    )
    # R2-3: a customer-bound user sees only GLOBAL workflows + its OWN customer's
    # (mirrors customer_workflows / the dispatch scoping); groups collapse to its one
    # customer so no other customer's name/workflow leaks. No-op for internal staff.
    my_cust = access.current_customer_id(me)
    if my_cust is not None:
        # GATE R2 MED-1: ownership is `customer_id`, never `scope`. `scope == GLOBAL` does
        # NOT mean "owned by nobody" -- eval_promote used to mint promoted workflows GLOBAL
        # WITH a customer_id set, so a scope-keyed ring showed every other customer's
        # promoted workflow (name, scope, step count and shape) to a bound linguist, while
        # `_wf_or_404` 404ed the very same id. See customers.workflow_offered_to_customer.
        wq = wq.where(Workflow.customer_id.is_(None) | (Workflow.customer_id == my_cust))
        cq = cq.where(Customer.id == my_cust)
        gq = gq.where(
            (WorkflowGroup.customer_id == my_cust) | (WorkflowGroup.customer_id.is_(None))
        )
    workflows = _visible_workflows(request, session, session.exec(wq).all())
    customers = session.exec(cq).all()
    cust_by_id = {c.id: c.name for c in customers}
    groups = session.exec(gq).all()
    # variant lists per group (one query per group, mirrors the pre-Wave-A rollup) —
    # used both for the collapsed variant count AND to aggregate the group's stats.
    group_variant_wfs = {g.id: _visible_group_variants(request, session, g.id) for g in groups}

    # One stats pass over EVERY workflow id in view (standalones + all group variants),
    # scoped to the caller's visible runs inside the service (access predicates). Three
    # grouped queries total, not N+1 — the dashboard's N rows cost the same as one.
    grouped_ids = {v.id for vs in group_variant_wfs.values() for v in vs}
    standalone = [w for w in workflows if not w.group_id]
    stat_ids = [w.id for w in standalone] + list(grouped_ids)
    bundle = workflow_stats.collect(session, stat_ids, user=me)

    rows = []
    # Standalone workflows: one row each, links to the builder. Fetch each workflow's
    # steps ONCE and reuse for both the shape detection and the step count (was 2N
    # queries).
    for w in standalone:
        steps = orchestrator.get_steps(session, w.id)
        result = wshapes.detect_shapes(steps)
        rows.append(
            {
                "kind": "workflow",
                "id": w.id,
                "name": w.name,
                "href": f"/workflows/{w.id}",
                "scope": w.scope,
                "archived": getattr(w, "archived_at", None) is not None,
                "customer": cust_by_id.get(w.customer_id, "—"),
                "shape_chips": wshapes.chip_descriptors(result, fanout=False),
                "origin": wshapes.origin_label(getattr(w, "created_via", None)),
                "steps": len(steps),
                "variants": None,
                "stats": bundle.for_workflow(w.id),
            }
        )
    # Groups: one collapsed row each, links to the group page. Shape/steps come from the
    # representative (default, else first) variant; stats aggregate across all variants
    # with UNION distinct semantics (no double-counting a shared customer).
    for g in groups:
        variants = group_variant_wfs.get(g.id, [])
        rep = next(
            (v for v in variants if getattr(v, "is_default", False)),
            variants[0] if variants else None,
        )
        rep_steps = orchestrator.get_steps(session, rep.id) if rep else []
        chips = (
            wshapes.chip_descriptors(wshapes.detect_shapes(rep_steps), fanout=len(variants) > 1)
            if rep
            else []
        )
        rows.append(
            {
                "kind": "group",
                "id": g.id,
                "name": g.name,
                "href": f"/workflow-groups/{g.id}",
                "scope": (Scope.CUSTOMER if g.customer_id else Scope.GLOBAL),
                "customer": cust_by_id.get(g.customer_id, "—") if g.customer_id else "—",
                "shape_chips": chips,
                "origin": "group",
                "steps": len(rep_steps),
                "variants": len(variants),
                "stats": bundle.for_workflows([v.id for v in variants]),
            }
        )

    active_sort, active_dir = _sort_rows(rows, sort, dir)
    return templates.TemplateResponse(
        request,
        "workflows.html",
        {
            "request": request,
            "rows": rows,
            "active_sort": active_sort,
            "active_dir": active_dir,
            "sortable": _WF_SORT_FIELDS,
            "archived_filter": archived_filter,
            "has_any": bool(rows),
        },
    )


def _wf_copy_targets_ctx(request, session, wf):
    """Active-pairs confinement (the maintainer's P2 follow-up) for the builder's "Copy to
    language pair(s)" picker: the SAME customer resolution _wf_glossary_customer
    already uses (bound caller, else the workflow's owning GROUP's customer_id) fed
    through the shared confined_target_rows seam, source-filtered on this workflow's
    OWN source_locale (mirrors the project add-locale seam exactly).

    A GLOBAL/ungrouped workflow (no owning customer) or a customer with no active
    pairs -> the full enabled subset, confined=False (confined_target_rows' mandatory
    never-brick-a-fresh-install fallback). Returns the three context keys the template
    reads: copy_target_rows (langpicker `enabled` slot), copy_pairs_confined (drives
    the note), copy_pairs_customer (names the note)."""
    cid = _wf_glossary_customer(request, session, wf)
    customer = session.get(Customer, cid) if cid is not None else None
    rows, confined = customer_service.confined_target_rows(
        session, customer, source_locale=wf.source_locale
    )
    return {
        "copy_target_rows": rows,
        "copy_pairs_confined": confined,
        "copy_pairs_customer": customer,
    }


def _validated_prompt_template_id(request, session, prompt_template_id):
    """Coerce + re-validate a submitted prompt_template_id against the caller's VISIBLE
    template set (mirrors the glossary_id re-validation): a tampered/foreign/stale id that
    is not in scope silently clears to None (no binding), never an error — the service
    then validates an in-scope id for existence + the source-placeholder rule."""
    ptid = ids.coerce_id(prompt_template_id)
    if ptid is None:
        return None
    allowed = {t.id for t in _visible_prompt_templates(request, session)}
    return ptid if ptid in allowed else None


def _render_workflow_review(
    request,
    session,
    fields,
    warnings,
    description,
    errors=None,
    provider="",
    model="",
    repaired=False,
    repaired_workflow_fields=None,
    repair_notice=None,
):
    """Render workflow_author_review.html from a validated draft (author route) OR an
    untrusted payload being re-shown with errors (confirm rejection) OR a repaired draft
    ("Fix this for me"). The per-step glossary dropdown is grounded in the SAME
    catalogs the builder uses, scoped to this caller. A designed workflow has no
    language pair of its own, so there is no source/target picker and glossary options
    resolve against a BLANK pair (every approved, visible glossary).

    `provider`/`model` are the drafting engine, threaded through as hidden form inputs so
    the "Fix this for me" button can call the SAME engine (and confirm ignores them).
    `repaired` (with each step carrying a `repaired_fields` list + `repaired_workflow_fields`)
    drives the changed-field badges/flash on a repair re-render. `repair_notice` is a
    one-shot banner (a repair failure message, or an "already valid" note)."""
    from app.kernel import glossary_module as gm
    from app.kernel import languages_catalog, setup_author

    # Customer-first: a bound caller is confined to its own customer; a staff caller's
    # dropdown is confined to the draft's CHOSEN customer (+ legacy unassigned) so the
    # review page only offers glossaries the drafted workflow could legitimately pin.
    bound = _my_customer_id(request)
    draft_cust = fields.get("customer_id")
    review_cust = bound if bound is not None else _int_or_none(draft_cust)
    # Blank pair: a designed workflow carries no pair, so every approved, visible
    # glossary is offered -- exactly the universe apply_workflow_draft validates against.
    # Each label carries the glossary's own language pairs (a glossary applies at run
    # time only for runs whose languages it covers).
    glossary_options = [
        {"id": g.id, "label": gm.option_label(session, g)}
        for g in gm.options_for_step(
            session, "", "", org_id=_my_org_id(request), customer_id=review_cust
        )
    ]
    # SPEC-workflows-redesign Phase 3: HEADLINE the shape the LLM drafted (the "categorize
    # agent-driven workflows" goal — make the drafted structure legible before confirm),
    # generated from the same detect_shapes + the real step names/thresholds.
    from app.kernel import workflow_shapes as wshapes

    _steps = fields.get("steps") or []
    # MED-1/MED-A: surface any step_type the user POSTED that the assistant does not draft
    # (external, junk, OR a now-BLANK one) as a SELECTED + DISABLED option on that step's
    # dropdown (see the template). Without it, a confirm-rejection re-render shows the excluded
    # step with NO <option> selected, so the <select> falls back to its first option
    # (llm_agent) and a blind re-confirm SILENTLY creates a stray llm_agent step (the BUG-A201
    # class). MED-A: a disabled <option> is EXCLUDED from the POST entry list (HTML spec), so
    # a re-POST of the blocked option submits NOTHING and the next parse sees a BLANK type --
    # so blank is collected here too (no `_norm_t` truthiness guard), keeping a
    # selected+disabled "(no type selected)" option that the select can never leave to submit
    # a real type the user did not choose. Blank is normalized to "" so the template's
    # `t == step.step_type` match (step_type is "" after _parse_review_form) is exact; a
    # non-blank raw value is kept verbatim for the same exact-match reason.
    _accepted_types = list(setup_author._WF_STEP_TYPES)
    _blocked_step_types: list = []
    for _st in _steps:
        _raw_t = _st.get("step_type")
        _norm_t = str(_raw_t or "").strip().lower().replace("-", "_")
        _key_t = _raw_t if (_raw_t not in (None, "")) else ""
        if _norm_t not in _accepted_types and _key_t not in _blocked_step_types:
            _blocked_step_types.append(_key_t)
    _result = wshapes.detect_shapes(_steps)
    _enabled = languages_catalog.enabled_locales()
    return templates.TemplateResponse(
        request,
        "workflow_author_review.html",
        {
            "request": request,
            "fields": fields,
            "warnings": warnings or [],
            "errors": errors or [],
            "description": description,
            "providers": config.PROVIDERS,
            # The ASSISTANT surface offers exactly the DRAFTABLE step types (setup_author's
            # accepted set: llm_agent/lqa/human_review/tm_match) -- NOT the manual builder's
            # full StepType.ALL. external is deliberately absent here: it needs a vendor
            # binding with no drafting UI, so it is added in the builder after the workflow
            # exists (setup_author._ASSISTANT_UNSUPPORTED, honestly rejected at confirm if
            # posted anyway). One seam so the dropdown can never drift from what confirm
            # accepts (BACKLOG §2026-08-31: the tm_match parity fix + honest external
            # exclusion). tm_match renders as a no-engine step via the template's data-types
            # (it appears in no data-types list, exactly like human_review's engine fields).
            "step_types": _accepted_types,
            # MED-1: a posted-but-not-draftable type shown as a selected+disabled option so a
            # rejection re-render never leaves the <select> silently defaulting to llm_agent.
            "blocked_step_types": _blocked_step_types,
            "customers": _workflow_review_customers(request, session),
            "glossary_options": glossary_options,
            "locales": _enabled,
            "label_for": languages_catalog.label_for,
            "shape_chips": wshapes.chip_descriptors(_result, fanout=False),
            "shape_headline": (wshapes.shape_headline(_steps, n_locales=0) if _steps else ""),
            # Draft-repair ("Fix this for me"): the drafting engine (hidden inputs, so the
            # repair button calls the same engine), the repaired-badge state, and a one-shot
            # repair banner. All benign no-ops on the author/confirm render paths.
            "author_provider": provider or "",
            "author_model": model or "",
            "repaired": repaired,
            "repaired_workflow_fields": repaired_workflow_fields or [],
            "repair_notice": repair_notice,
        },
    )


def _parse_review_form(form):
    """Parse the review page's flat form into (payload, description). Steps are
    indexed s{i}_<field> up to step_count; a checkbox absent == False. The payload
    shape matches what apply_workflow_draft re-validates as untrusted input."""
    try:
        count = int(form.get("step_count") or 0)
    except (TypeError, ValueError):
        count = 0
    # Clamp to a sane range: floor at 0 (a negative/absent count iterates nothing)
    # and ceil at _MAX_REVIEW_STEPS so a forged step_count (e.g. 100000000) cannot
    # make the parse loop for an unbounded time. apply_workflow_draft re-validates
    # the resulting step list, so a truncated-but-bounded parse is safe.
    count = max(0, min(count, _MAX_REVIEW_STEPS))
    steps = []
    for i in range(count):
        step = {f: (form.get(f"s{i}_{f}") or "") for f in _STEP_TEXT_FIELDS}
        # MED-A: a disabled blocked <option> submits NOTHING (HTML spec), so on a re-POST the
        # <select> yields a BLANK step_type. Fall back to the hidden `s{i}_posted_step_type`
        # mirror (the ORIGINALLY-posted value the last render preserved) so the confirm error
        # stays the HONEST "external ..." message instead of degrading to "unknown step type
        # '(blank)'". A real user pick wins (the enabled <select> submits a non-blank value,
        # so this fallback is a no-op); it only fills in when the select submitted nothing.
        if not step.get("step_type"):
            step["step_type"] = form.get(f"s{i}_posted_step_type") or ""
        for f in _STEP_BOOL_FIELDS:
            step[f] = form.get(f"s{i}_{f}") is not None
        # Normalize glossary_id to int (empty -> None) so the error re-render path
        # feeds the template the SAME type the happy path does; the Jinja option
        # test `g.id == step.glossary_id` compares int-to-int, keeping the user's
        # per-step glossary selection <option selected> on a confirm rejection.
        step["glossary_id"] = _int_or_none(step.get("glossary_id"))
        # SPEC §5 pre-step guard (Phase 2): assemble the {ref, op, value} guard from the
        # per-step ref/op/value selects. A blank ref = no guard (None = always run).
        # apply_workflow_draft re-validates it as untrusted input.
        step["run_condition"] = _parse_run_condition_fields(
            form.get(f"s{i}_run_condition_ref"),
            form.get(f"s{i}_run_condition_op"),
            form.get(f"s{i}_run_condition_value"),
        )
        # SPEC §5 Phase 3/4: the post-step branch from the on_pass/on_fail selects (None =
        # no branch). key + max_visits ride in via _STEP_TEXT_FIELDS above (plain scalars);
        # blank max_visits -> None so apply_workflow_draft inherits the run default cap.
        step["branch"] = _parse_branch_fields(
            form.get(f"s{i}_branch_on_pass"), form.get(f"s{i}_branch_on_fail")
        )
        step["key"] = (step.get("key") or "").strip() or None
        step["max_visits"] = (step.get("max_visits") or "").strip() or None
        steps.append(step)
    payload = {
        "name": form.get("name") or "",
        # Normalize customer_id to int (empty -> None) for the same reason: the
        # template's `fields.customer_id == c.id` must compare int-to-int so the
        # chosen customer stays selected when the confirm re-renders with errors.
        "customer_id": _int_or_none(form.get("customer_id")),
        "steps": steps,
        "notes_for_reviewer": form.get("notes_for_reviewer") or "",
    }
    return payload, (form.get("description") or "")


def _prompt_template_or_404(request, session, template_id, write=False):
    """Fetch a prompt template + org-gate it (leak vector: a template is org-owned).
    Returns (tmpl, blocked). A foreign / missing template is a 404 (not enumerable).

    R2-3: also runs the CUSTOMER ring (_wf_customer_block; NULL customer_id = GLOBAL,
    stays visible) so a customer-bound user cannot rename/delete another customer's
    prompt template by direct id — the same shared access.customer_ring_blocks seam the
    workflow/group routes use, so web + /api cannot drift. No-op for the common case
    (normally-created templates carry a NULL customer_id) and for internal staff."""
    tmpl = wf_service.get_prompt_template(session, template_id)
    if tmpl is None:
        return None, _error_page(request, 404, "Not found.")
    blocked = _org_block(request, getattr(tmpl, "org_id", None))
    if blocked:
        return None, blocked
    blocked = _wf_customer_block(request, getattr(tmpl, "customer_id", None))
    if blocked:
        return None, blocked
    if write:
        # #468: a global template is read-only for a customer-bound user (403).
        blocked = _wf_write_block(request, getattr(tmpl, "customer_id", None))
        if blocked:
            return None, blocked
    return tmpl, None
