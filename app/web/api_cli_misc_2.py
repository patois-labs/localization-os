"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import (
    access,
    audit,
    languages,
    security,
    system_prompts,
    workflows,
)
from app.models import (
    Project,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _ab_chosen_locale,
    _ai_block,
    _api_workflow_customer,
    _cust_of,
    _customer_in_org,
    _gate,
    _grp_in_org,
    _int_id,
    _json,
    _langset_in_org,
    _mock_spend_block_for_doc,
    _project_in_org,
    _role_block,
    _shared_write_block,
    _visible_document,
    _wf_in_org,
)

router = APIRouter()


# ============================================================================
# Style guides (SPEC-workflows-redesign Track B, B2): first-class approvable
# assets. Org + R2-3 customer scoped; content edits + approval are can_tune_ai-gated
# (linguists own language assets — mirrors the glossary module + content types). A
# guide MAY be org-global (customer NULL), unlike customer-first glossaries.
# ============================================================================
def _style_guide_in_scope(s: Session, gid: int, org_id, ident):
    """Fetch a StyleGuide in the caller's org + customer scope, else None (404).
    Mirrors _glossary_in_scope — a foreign org's/customer's guide is indistinguishable
    from a missing one."""
    from app.kernel import style_guides as sg

    return sg.visible(s, gid, org_id=org_id, customer_id=_cust_of(ident))


@router.get("/api/style-guides")
def api_style_guides_list(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import style_guides as sg

    with Session(engine) as s:
        guides = sg.list_guides(s, org_id=org_id, customer_id=_cust_of(ident))
        payload = [sg.to_dict(s, g, include_body=False) for g in guides]
    return JSONResponse({"ok": True, "style_guides": payload})


@router.post("/api/style-guides")
async def api_style_guides_create(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # asset CRUD -> can_tune_ai (web /style-guides parity)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import style_guides as sg

    with Session(engine) as s:
        # R2-3: a customer-BOUND caller is FORCED to its own customer (cannot forge a
        # foreign/global guide). A STAFF caller may pass a customer_id (validated in-org)
        # or leave it blank (org-global — allowed for style guides).
        bound = _cust_of(ident)
        if bound is not None:
            cust = bound
        else:
            cust = body.get("customer_id")
            raw = str(cust).strip() if cust is not None else ""
            if raw:
                cid_val = _int_id(raw)
                if cid_val is None or _customer_in_org(s, cid_val, org_id, ident) is None:
                    return JSONResponse(
                        {"ok": False, "error": "unknown customer."}, status_code=400
                    )
                cust = cid_val
            else:
                cust = None
        try:
            g = sg.create(
                s,
                str(body.get("name") or ""),
                org_id=org_id,
                customer_id=cust,
                content_type_id=body.get("content_type_id"),
                source_locale=str(body.get("source_locale") or ""),
                target_locale=str(body.get("target_locale") or ""),
                body=str(body.get("body") or ""),
                created_by=ident["actor"],
            )
        except sg.StyleGuideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **sg.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action="style_guide.create",
        target=f"style_guide {out['id']}",
        ip=deps._ip(request),
        customer_id=out.get("customer_id"),
    )
    return JSONResponse(out)


@router.get("/api/style-guides/{gid}")
def api_style_guides_show(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import style_guides as sg

    with Session(engine) as s:
        g = _style_guide_in_scope(s, gid, org_id, ident)
        if g is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"ok": True, **sg.to_dict(s, g)})


@router.post("/api/style-guides/{gid}/update")
async def api_style_guides_update(gid: int, request: Request):
    """Update name / body / content-type / pair. Mirrors the CLI `style-guide update`:
    only the keys PRESENT in the body are changed; body + scope edits revert approval.
    A `clear_content_type` / `clear_pair` boolean clears that axis."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import style_guides as sg

    with Session(engine) as s:
        guide = _style_guide_in_scope(s, gid, org_id, ident)
        if guide is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(guide, "customer_id", None))  # #468
        if blocked:
            return blocked
        try:
            if "name" in body:
                sg.rename(s, gid, str(body.get("name") or ""))
            if "body" in body:
                sg.set_body(s, gid, str(body.get("body") or ""))
            change_ct = ("content_type_id" in body) or bool(body.get("clear_content_type"))
            change_pair = (
                "source_locale" in body or "target_locale" in body or bool(body.get("clear_pair"))
            )
            if change_ct or change_pair:
                sg.set_scope(
                    s,
                    gid,
                    content_type_id=body.get("content_type_id"),
                    source_locale=str(body.get("source_locale") or ""),
                    target_locale=str(body.get("target_locale") or ""),
                    change_content_type=change_ct,
                    change_pair=change_pair,
                )
        except sg.StyleGuideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **sg.to_dict(s, sg.get(s, gid))}
    audit.log(
        actor=ident["actor"],
        action="style_guide.update",
        target=f"style_guide {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/style-guides/{gid}/set-status")
async def api_style_guides_set_status(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # approval -> can_tune_ai (web parity; no manager gate)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import style_guides as sg

    with Session(engine) as s:
        guide = _style_guide_in_scope(s, gid, org_id, ident)
        if guide is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(guide, "customer_id", None))  # #468
        if blocked:
            return blocked
        try:
            status = str(body.get("status") or "")
            g = (
                sg.approve(s, gid, approver_email=ident["actor"])
                if status == "approved"
                else sg.set_status(s, gid, status)
            )
        except sg.StyleGuideError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **sg.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action=f"style_guide.{out['status']}",
        target=f"style_guide {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/style-guides/{gid}/delete")
async def api_style_guides_delete(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    from app.kernel import style_guides as sg

    with Session(engine) as s:
        guide = _style_guide_in_scope(s, gid, org_id, ident)
        if guide is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(guide, "customer_id", None))  # #468
        if blocked:
            return blocked
        sg.delete(s, gid)
    audit.log(
        actor=ident["actor"],
        action="style_guide.delete",
        target=f"style_guide {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "style_guide_id": gid, "deleted": True})


# ============================================================================
# Language Sets (CRUD)
# ============================================================================
@router.get("/api/langsets")
def api_langsets_list(request: Request):
    _ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    sets = languages.list_sets(org_id=org_id)
    payload = [
        {
            "id": ls.id,
            "name": ls.name,
            "source_locale": ls.source_locale,
            "target_locales": ls.target_locales or [],
        }
        for ls in sets
    ]
    return JSONResponse({"ok": True, "language_sets": payload})


@router.post("/api/langsets")
async def api_langsets_create(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web POST /languages is can_tune_ai (admin/linguist)
    if blocked:
        return blocked
    # #468: a new language set is shared org-wide, so a customer-bound caller cannot add one.
    blocked = _shared_write_block(ident, None)
    if blocked:
        return blocked
    body = await _json(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name is blank."}, status_code=400)
    try:
        ls = languages.create(
            name,
            str(body.get("source") or "en-US"),
            body.get("targets") or "",
            actor=ident["actor"],
            org_id=org_id,
        )
    except languages.InvalidLocaleError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "id": ls.id,
            "name": ls.name,
            "source_locale": ls.source_locale,
            "target_locales": ls.target_locales or [],
        }
    )


@router.post("/api/langsets/{sid}")
async def api_langsets_update(sid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web POST /languages/{id} is can_tune_ai (admin/linguist)
    if blocked:
        return blocked
    body = await _json(request)
    ls = _langset_in_org(languages.get(sid), org_id)
    if ls is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # #468: a language set is an org-level shared asset (no customer column): read-only
    # for a customer-bound caller.
    blocked = _shared_write_block(ident, None)
    if blocked:
        return blocked
    targets = body.get("targets")
    if targets in (None, ""):
        targets = ls.target_locales
    try:
        languages.update(sid, str(body.get("name") or ""), str(body.get("source") or ""), targets)
    except languages.InvalidLocaleError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    fresh = languages.get(sid)
    return JSONResponse(
        {
            "ok": True,
            "id": fresh.id,
            "name": fresh.name,
            "source_locale": fresh.source_locale,
            "target_locales": fresh.target_locales or [],
        }
    )


@router.post("/api/langsets/{sid}/delete")
def api_langsets_delete(sid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web POST /languages/{id}/delete is can_tune_ai (admin/linguist)
    if blocked:
        return blocked
    ls = _langset_in_org(languages.get(sid), org_id)
    if ls is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # #468: a language set is an org-level shared asset (no customer column): read-only
    # for a customer-bound caller.
    blocked = _shared_write_block(ident, None)
    if blocked:
        return blocked
    # SPEC-rename-delete §7: fails closed while a RoutingRule / Connection config still
    # references the set (itemized 400, never a 500).
    try:
        languages.delete(sid)
    except languages.LanguageSetInUseError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "references": exc.refs}, status_code=400
        )
    return JSONResponse({"ok": True, "id": sid, "deleted": True})


@router.post("/api/workflow-groups/{gid}/rename")
async def api_workflow_groups_rename(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        grp = _grp_in_org(s, gid, org_id, ident)
        if grp is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(grp, "customer_id", None))
        if blocked:
            return blocked
        old_name, customer_id = grp.name, grp.customer_id
        try:
            workflows.rename_group(s, gid, str(body.get("name") or ""))
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        new_name = grp.name
    audit.log(
        actor=ident["actor"],
        action="workflow_group.rename",
        target=f"workflow_group {gid}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=deps._ip(request),
        customer_id=customer_id,
    )
    return JSONResponse({"ok": True, "id": gid, "name": new_name})


@router.post("/api/workflow-groups/{gid}/delete")
async def api_workflow_groups_delete(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        # Customer ring BEFORE the admin gate (see api_workflows_delete): a bound
        # caller's foreign group is 404, not a 403 that leaks its existence.
        grp = _grp_in_org(s, gid, org_id, ident)
        if grp is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(grp, "customer_id", None))
        if blocked:
            return blocked
        blocked = _role_block(ident, security.is_admin)
        if blocked:
            return blocked
        name, customer_id = grp.name, grp.customer_id
        try:
            workflows.delete_group(s, gid)
        except workflows.WorkflowError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "references": exc.refs}, status_code=409
            )
    audit.log(
        actor=ident["actor"],
        action="workflow_group.delete",
        target=f"workflow_group {gid} '{name}'",
        ip=deps._ip(request),
        customer_id=customer_id,
    )
    return JSONResponse({"ok": True, "id": gid, "deleted": True})


def _pt_in_org(s: Session, tid: int, org_id, ident=None):
    """Fetch a PromptTemplate, or None if missing / in another org / (R2-3) blocked by
    the customer ring for a bound caller. A GLOBAL prompt template (customer_id NULL)
    stays visible to everyone; a customer-scoped one is confined to its own customer
    (same shared access.customer_ring_blocks predicate the workflow-family web routes
    use), so a bound caller can never rename/delete a foreign customer's template."""
    from app.models import PromptTemplate

    tmpl = s.get(PromptTemplate, tid)
    if tmpl is None:
        return None
    if org_id is not None and getattr(tmpl, "org_id", None) != org_id:
        return None
    if access.customer_ring_blocks(_cust_of(ident), getattr(tmpl, "customer_id", None)):
        return None
    return tmpl


@router.post("/api/prompt-templates/{tid}/rename")
async def api_prompt_templates_rename(tid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        tmpl = _pt_in_org(s, tid, org_id, ident)
        if tmpl is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(tmpl, "customer_id", None))
        if blocked:
            return blocked
        old_name = tmpl.name
        try:
            workflows.rename_prompt_template(s, tid, str(body.get("name") or ""))
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        new_name = tmpl.name
    audit.log(
        actor=ident["actor"],
        action="prompt_template.rename",
        target=f"prompt_template {tid}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "id": tid, "name": new_name})


@router.post("/api/prompt-templates/{tid}/delete")
async def api_prompt_templates_delete(tid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        # Customer ring BEFORE the admin gate (see api_workflows_delete): a bound
        # caller's foreign template is 404, not a 403 that leaks its existence.
        tmpl = _pt_in_org(s, tid, org_id, ident)
        if tmpl is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(tmpl, "customer_id", None))
        if blocked:
            return blocked
        blocked = _role_block(ident, security.is_admin)
        if blocked:
            return blocked
        name = tmpl.name
        try:
            refs = workflows.delete_prompt_template(s, tid)
        except workflows.WorkflowError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "references": exc.refs}, status_code=409
            )
    audit.log(
        actor=ident["actor"],
        action="prompt_template.delete",
        target=f"prompt_template {tid} '{name}'",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "id": tid, "deleted": True, "references": refs})


# --- workflow GROUPS: create / list / show / add / set-default (can_tune_ai) --
# Completes the family (rename/delete already exist above). _grp_in_org is the ring;
# a workflow attached/defaulted funnels through _wf_in_org so a foreign-org/customer
# workflow can never be pulled into a group the caller can see. MCP: manage_workflow_group.
@router.post("/api/workflow-groups")
async def api_workflow_groups_create(request: Request):
    """Create a workflow group (mirrors `workflow group create`). can_tune_ai. The
    body customer_id funnels through _api_workflow_customer (the S1/S2 funnel law: a
    bound caller may only scope to its OWN customer, a staff caller's foreign/cross-org
    id is 404, an archived customer is refused). Blank customer_id => a global group."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        customer_id, blocked = _api_workflow_customer(s, ident, org_id, body.get("customer_id"))
        if blocked:
            return blocked
        grp = workflows.create_group(
            s,
            str(body.get("name") or ""),
            customer_id=customer_id,
            description=str(body.get("description") or ""),
            org_id=org_id,
            created_by=ident["actor"],
        )
        if grp is None:
            return JSONResponse(
                {"ok": False, "error": "workflow group name must not be blank."}, status_code=400
            )
        gid, gname, gcust = grp.id, grp.name, grp.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow_group.create",
        target=f"workflow_group {gid} '{gname}'",
        ip=deps._ip(request),
        customer_id=gcust,
    )
    return JSONResponse({"ok": True, "id": gid, "name": gname, "customer_id": gcust})


@router.get("/api/workflow-groups")
def api_workflow_groups_list(request: Request):
    """List workflow groups visible to the caller (mirrors `workflow group list`). A
    read — org-filtered, and (for a customer-bound key) confined to GLOBAL groups + its
    own customer's via the SAME access.customer_ring_blocks predicate _grp_in_org uses."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        groups = [
            g
            for g in workflows.list_groups(s, org_id=org_id)
            if not access.customer_ring_blocks(_cust_of(ident), getattr(g, "customer_id", None))
        ]
        payload = [
            {
                "id": g.id,
                "name": g.name,
                "customer_id": g.customer_id,
                "variants": sum(
                    _wf_in_org(s, v.id, org_id, ident) is not None
                    for v in workflows.group_variants(s, g.id)
                ),
            }
            for g in groups
        ]
    return JSONResponse({"ok": True, "groups": payload})


@router.get("/api/workflow-groups/{gid}")
def api_workflow_groups_show(gid: int, request: Request):
    """Show a group + its per-pair variants (mirrors `workflow group show`). Org+customer
    bound via _grp_in_org (a foreign group is 404, indistinguishable from missing)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        grp = _grp_in_org(s, gid, org_id, ident)
        if grp is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        variants = [
            {
                "workflow_id": v.id,
                "name": v.name,
                "source_locale": v.source_locale,
                "target_locale": v.target_locale,
                "is_default": v.is_default,
            }
            for v in workflows.group_variants(s, grp.id)
            if _wf_in_org(s, v.id, org_id, ident) is not None
        ]
    return JSONResponse(
        {
            "ok": True,
            "id": grp.id,
            "name": grp.name,
            "customer_id": grp.customer_id,
            "variants": variants,
        }
    )


@router.post("/api/workflow-groups/{gid}/add")
async def api_workflow_groups_add(gid: int, request: Request):
    """Attach an EXISTING workflow to a group as the variant for one language pair
    (mirrors `workflow group add`). can_tune_ai. BOTH ids funnel through the ring: the
    group via _grp_in_org (404), the workflow via _wf_in_org (404) — a foreign-org/
    customer workflow can never be pulled into a visible group, and a bound caller can
    never reach either object across its ring. Body: workflow_id, pair ("src:tgt") OR
    source_locale/target_locale, make_default, force. add_to_group fails closed (400) on
    a junk locale or a silent pair-replace (issue #46)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # Ring BEFORE add: a foreign group OR workflow is a 404 (never enumerable),
        # resolved before any locale/pair validation (scope-before-validate): a malformed
        # pair must never turn a foreign-object 404 into a distinguishable 400.
        grp = _grp_in_org(s, gid, org_id, ident)
        if grp is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if body.get("make_default"):
            # #468: a bound caller may add its OWN workflow to a shared group (its variant
            # stays in its own ring), but making it the default changes the group for
            # every customer, so that needs a writable group.
            blocked = _shared_write_block(ident, getattr(grp, "customer_id", None))
            if blocked:
                return blocked
        wid = _int_id(body.get("workflow_id"))
        member = _wf_in_org(s, wid, org_id, ident) if wid is not None else None
        if member is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # #468: adding a workflow to a group changes the workflow too.
        blocked = _shared_write_block(ident, getattr(member, "customer_id", None))
        if blocked:
            return blocked
        blocked = _group_default_head_api_block(s, ident, wid, gid, bool(body.get("make_default")))
        if blocked:
            return blocked
        src = target = None
        pair = str(body.get("pair") or "").strip()
        if pair:
            raw = [x.strip() for x in pair.replace("->", ":").split(":") if x.strip()]
            if len(raw) != 2:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"invalid pair {pair!r} (want src:tgt, e.g. en-US:fr-FR).",
                    },
                    status_code=400,
                )
            src, target = raw[0], raw[1]
        else:
            src = str(body.get("source_locale") or "").strip() or None
            target = str(body.get("target_locale") or "").strip() or None
        try:
            wf = workflows.add_to_group(
                s,
                wid,
                gid,
                source_locale=src,
                target_locale=target,
                make_default=bool(body.get("make_default")),
                force=bool(body.get("force")),
            )
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = {
            "ok": True,
            "workflow_id": wf.id,
            "group_id": gid,
            "source_locale": wf.source_locale,
            "target_locale": wf.target_locale,
            "is_default": wf.is_default,
        }
        cust = grp.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow_group.add",
        target=f"workflow_group {gid} workflow {out['workflow_id']}",
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(out)


def _group_default_head_api_block(s, ident, wid, gid, make_default):
    """#365 gate fix: the /api half of the web `_group_default_head_block`. A default change
    that moves or removes the system prompt head the group's pair variants inherit, or (since
    #386) moving an ordinary variant so the move changes either effective head, needs an
    ADMIN or a MANAGER (2026-10-08: system_prompts.can_change_group_default_heads; a
    linguist is still refused); no head involved, flag off or login wall off: no extra gate."""
    if not system_prompts.group_default_change_needs_admin(s, wid, gid, make_default):
        return None
    return _role_block(
        ident,
        system_prompts.can_change_group_default_heads,
        body={"ok": False, "error": system_prompts.GROUP_DEFAULT_ADMIN_ERROR},
    )


@router.post("/api/workflow-groups/{gid}/default")
async def api_workflow_groups_set_default(gid: int, request: Request):
    """Flag an existing group MEMBER as the group's fallback default (mirrors `workflow
    group set-default`). Both ids ring-funnelled (_grp_in_org / _wf_in_org);
    membership is verified BEFORE flipping so the default is never set on the wrong
    group (a 400 naming the mismatch, never a leak). Exactly one default per group.
    Admin, linguist or manager (2026-10-08: system_prompts.can_set_group_default)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _role_block(ident, system_prompts.can_set_group_default)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        grp = _grp_in_org(s, gid, org_id, ident)
        if grp is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(grp, "customer_id", None))
        if blocked:
            return blocked
        wid = _int_id(body.get("workflow_id"))
        if wid is None:
            return JSONResponse({"ok": False, "error": "workflow_id is required."}, status_code=400)
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        if wf.group_id is None or wf.group_id != gid:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"workflow {wid} is not a member of group {gid}; add it to the group "
                    "first (POST /api/workflow-groups/{gid}/add).",
                },
                status_code=400,
            )
        blocked = _group_default_head_api_block(s, ident, wid, gid, True)
        if blocked:
            return blocked
        updated = workflows.set_default_variant(s, wid)
        if updated is None:
            return JSONResponse(
                {"ok": False, "error": f"could not set default for workflow {wid}."},
                status_code=400,
            )
        out = {
            "ok": True,
            "workflow_id": updated.id,
            "group_id": gid,
            "is_default": updated.is_default,
            "source_locale": updated.source_locale,
            "target_locale": updated.target_locale,
        }
        cust = grp.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow_group.set_default",
        target=f"workflow_group {gid} workflow {wid}",
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(out)


# --- prompt templates: create / list (can_tune_ai) ---------------------------
# Completes the family (rename/delete already exist). create_prompt_template enforces
# the bound-caller confinement at the SERVICE seam (bound_customer_id), and validates a
# staff caller's customer via customers.selectable (same-org, active); list_prompt_templates
# filters by the SAME access.customer_ring_blocks predicate.
@router.post("/api/prompt-templates")
async def api_prompt_templates_create(request: Request):
    """Create a reusable prompt template (mirrors `workflow prompt create`). can_tune_ai.
    A customer_id attaches it to one customer (a bound caller is FORCED to its own at the
    service seam; a staff caller's foreign/cross-org/archived id is refused); blank =
    GLOBAL."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        try:
            tmpl = workflows.create_prompt_template(
                s,
                str(body.get("name") or ""),
                body=str(body.get("body") or ""),
                org_id=org_id,
                customer_id=body.get("customer_id"),
                bound_customer_id=_cust_of(ident),
            )
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if tmpl is None:
            return JSONResponse(
                {"ok": False, "error": "prompt template name must not be blank."}, status_code=400
            )
        tid, tname, tscope, tcust = tmpl.id, tmpl.name, tmpl.scope, tmpl.customer_id
    audit.log(
        actor=ident["actor"],
        action="prompt_template.create",
        target=f"prompt_template {tid} '{tname}'",
        ip=deps._ip(request),
        customer_id=tcust,
    )
    return JSONResponse(
        {"ok": True, "id": tid, "name": tname, "scope": tscope, "customer_id": tcust}
    )


@router.get("/api/prompt-templates")
def api_prompt_templates_list(request: Request):
    """List reusable prompt templates (mirrors `workflow prompt list`). A read — org
    scoped, and (for a bound key) confined to GLOBAL + its own customer's via the SAME
    ring predicate the by-id routes use. `pinned_by` = how many workflow steps pin it."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        tmpls = workflows.list_prompt_templates(s, org_id=org_id, customer_id=_cust_of(ident))
        payload = [
            {
                "id": t.id,
                "name": t.name,
                "scope": t.scope,
                "customer_id": t.customer_id,
                "pinned_by": workflows.prompt_template_reference_count(s, t.id)["steps"],
            }
            for t in tmpls
        ]
    return JSONResponse({"ok": True, "prompt_templates": payload})


# --- style-guide A/B + context A/B (can_tune_ai, SPEND) ----------------------
# Measurement harnesses: run the SAME segments with the asset ON vs OFF and score both.
# SPEND — the mock-409 guard refuses a keyless-mock 'real measurement'. run_ab writes
# NOTHING (a pure read + in-memory score). The style-guide {gid} in the path funnels
# through _style_guide_in_scope (a foreign guide is 404); the document funnels through
# _visible_document + _project_in_org.
@router.post("/api/style-guides/{gid}/ab")
async def api_style_guides_ab(gid: int, request: Request):
    """Run the style-guide A/B measurement (mirrors `style-guide ab`). can_tune_ai, SPEND.
    Body: document (doc id), locale (optional; default the doc's first target), provider
    (optional engine override), max_segments (optional cap). The {gid} anchors the guide
    the caller is measuring the effect of (ring-checked, 404 foreign); run_ab itself
    resolves the approved guide for the pair and compares guide ON vs OFF."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import style_ab
    from app.kernel.context_ab import ABError

    with Session(engine) as s:
        # Ring the style guide (path) + the document (body) BEFORE any measurement.
        if _style_guide_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc_id = _int_id(body.get("document"))
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        project = s.get(Project, doc.project_id)
        locale = str(body.get("locale") or "").strip() or None
        chosen = _ab_chosen_locale(s, doc.id, locale)
        if chosen is None:
            return JSONResponse(
                {"ok": False, "error": f"document {doc.id} has no segments to A/B."},
                status_code=400,
            )
        provider = str(body.get("provider") or "").strip() or None
        mock = _mock_spend_block_for_doc(s, project, chosen, provider)
        if mock:
            return mock
        # #746: an A/B translates the sample twice through the engine; off the event loop.
        try:
            res = await deps.offload_blocking(
                style_ab.run_ab,
                s,
                doc,
                locale=locale,
                provider=provider,
                max_segments=(
                    body.get("max_segments")
                    if body.get("max_segments") is not None
                    else style_ab.DEFAULT_MAX_SEGMENTS
                ),
                org_id=org_id,
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except ABError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        data = res.as_dict()
    audit.log(
        actor=ident["actor"],
        action="style_guide.ab",
        target=f"style_guide {gid} document {doc_id}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "style_guide_id": gid, "ab": data})
