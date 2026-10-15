"""/api routes: customers

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import (
    audit,
    content,
)
from app.kernel import ids as ids
from app.models import (
    Customer,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _creator,
    _customer_config_block,
    _customer_in_org,
    _gate,
    _json,
    _json_object,
    _list_truncate,
    _list_window,
    _manage_block,
    _scoped_customers,
)

router = APIRouter()


# ============================================================================
# Customers
# ============================================================================
@router.get("/api/customers")
def api_customers_list(
    request: Request,
    limit: str | None = Query(None, description="max rows; default and hard cap 1000"),
    offset: str | None = Query(None, description="rows to skip (paging)"),
    archived: str = Query("", description="archive filter: '' / active, all, only"),
    include_archived: str = Query("", description="legacy 'both' toggle (1/true/yes)"),
    name: str = Query(
        "", description="exact case-insensitive (Unicode casefold), whitespace-trimmed name match"
    ),
):
    """List the caller's scoped customers. `?archived=active|all|only` (M4): active
    (default), both, or archived-only. `?include_archived=1` is the legacy 'both' toggle,
    kept for existing callers; `archived=` wins when both are given. `?name=` is an exact
    match compared with **Unicode casefold + whitespace trim** on both sides (M5c/NEW-2),
    so a name-to-id resolve never has to page the whole roster and never misses a
    non-ASCII or padded name. BOUNDED (AU-7): optional `limit` (default and hard cap 1000)
    + `offset`; the response carries `truncated`, `limit` and `offset`."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    name_q = name.strip()
    # SPEC-customer-archive: default = ACTIVE only (parity with the default web list +
    # the CLI `customer list`). M4: an explicit `archived=` mode is a SERVER-SIDE filter
    # so the --remote CLI arm no longer narrows client-side (which diverged past the cap).
    archived = archived.strip().lower()
    include_archived = include_archived.strip().lower() in ("1", "true", "yes")
    if archived and archived not in ("active", "all", "only"):
        return JSONResponse(
            {"ok": False, "error": "archived must be active, all or only."}, status_code=400
        )
    # AU-7: bound the result (was every scoped customer, unbounded). SQL limit/offset.
    limit, offset, err = _list_window(limit, offset)
    if err:
        return err
    with Session(engine) as s:
        stmt = _scoped_customers(ident, org_id)
        if archived == "only":
            stmt = stmt.where(Customer.archived_at.is_not(None))
        elif archived == "all" or (not archived and include_archived):
            pass  # both active + archived
        else:
            stmt = stmt.where(Customer.archived_at.is_(None))  # active (default)
        # L8: (name, id) so the LIMIT/OFFSET window is deterministic on Postgres.
        stmt = stmt.order_by(Customer.name, Customer.id)
        if name_q:
            # NEW-2: exact match in PYTHON with casefold + trim on BOTH sides. SQLite's
            # lower() is ASCII-only (misses 'Ünïcorn AG') and would compare a trimmed
            # param against the UN-trimmed stored column, so an SQL match silently missed
            # real rows -> MCP create_project then duplicated the customer. This resolve
            # is admin-time; O(n) over the scoped customers is fine (gate direction).
            needle = name_q.casefold()
            matched = [c for c in s.exec(stmt).all() if (c.name or "").strip().casefold() == needle]
            custs, truncated = _list_truncate(matched[offset : offset + limit + 1], limit)
        else:
            rows = s.exec(stmt.offset(offset).limit(limit + 1)).all()
            custs, truncated = _list_truncate(list(rows), limit)
        payload = [
            {"id": c.id, "name": c.name, "archived": c.archived_at is not None} for c in custs
        ]
    return JSONResponse(
        {"ok": True, "customers": payload, "truncated": truncated, "limit": limit, "offset": offset}
    )


@router.post("/api/customers")
async def api_customers_create(request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    # Mirror the web /customers create route: can_manage (>= manager). This also
    # subsumes R2-3 Finding F -- a customer-bound lsu_ key is only ever
    # translator/linguist/viewer (never admin/manager, per
    # access.CUSTOMER_BINDABLE_ROLES), so it can never pass can_manage and thus
    # can never create org-level customer records.
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    # RAW name so the service refuses a non-string (BUG-A13); a blank/whitespace string
    # keeps the friendly "blank" 400.
    raw_name = body.get("name")
    if raw_name is None or (isinstance(raw_name, str) and not raw_name.strip()):
        return JSONResponse({"ok": False, "error": "customer name is blank."}, status_code=400)
    with Session(engine) as s:
        try:
            # #580: a retried client call must not mint a twin; the 409 names the existing id.
            cust = content.customer_create(
                s, raw_name, creator=_creator(ident), reject_duplicate=True
            )
        except ids.InvalidNameError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except content.DuplicateNameError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "existing_id": exc.existing_id}, status_code=409
            )
    return JSONResponse({"ok": True, "id": cust.id, "name": cust.name})


@router.post("/api/customers/{cid}/rename")
async def api_customers_rename(cid: int, request: Request):
    """Remote-CLI/web parity for customer rename (SPEC-rename-delete).
    can_configure_customer-gated (#475: admin, manager or linguist, matching the web
    rename and the customer settings update, #284). Org+customer bound via
    _customer_in_org. A blank name is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _customer_config_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            cust = customer_service.rename_customer(
                s,
                cid,
                str(body.get("name") or ""),
                actor=ident["actor"],
                ip=deps._ip(request),
                org_id=org_id,
            )
        except content.DuplicateNameError as exc:
            # #617: a rename may not take another customer's name; same 409 as create.
            return JSONResponse(
                {"ok": False, "error": str(exc), "existing_id": exc.existing_id}, status_code=409
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "id": cid, "name": cust.name})


@router.post("/api/customers/{cid}/delete")
async def api_customers_delete(cid: int, request: Request):
    """Remote-CLI/web parity for customer delete (SPEC-rename-delete). ADMIN-only. FAILS
    CLOSED with an itemized reference count while anything references the customer (no
    cascade). Org bound via _customer_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        # Ring BEFORE role (permanent route-sweep finding) — see api_projects_delete.
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        try:
            customer_service.delete_customer(
                s, cid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except customer_service.CustomerDeleteBlocked as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "references": exc.refs}, status_code=400
            )
    return JSONResponse({"ok": True, "customer_id": cid, "deleted": True})


@router.post("/api/customers/{cid}/pairs/import")
async def api_customer_pairs_import(cid: int, request: Request):
    """Remote-CLI/web parity for bulk `customer pairs import` (CLI-parity backlog
    item 2). can_configure_customer-gated (admin, manager, linguist), matching the
    web /customers/{id}/pairs add/remove routes. ATOMIC
    (customers.import_pairs_csv validates every row before any write) + IDEMPOTENT
    (already-present pairs are skipped, reported). Org+customer bound via
    _customer_in_org. csv_b64 travels as base64 JSON, mirroring the glossary/TM
    import routes' transport."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _customer_config_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    try:
        data = base64.b64decode(str(body.get("csv_b64") or ""))
    except Exception:
        return JSONResponse({"ok": False, "error": "bad csv_b64."}, status_code=400)
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            # #599: the CSV parse runs on a bounded worker, never on the event loop.
            res = await deps.offload_blocking(customer_service.import_pairs_csv, s, cid, data)
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except (customer_service.PairImportError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if res["added"]:
            audit.log(
                actor=ident["actor"],
                action="customer.pairs_import",
                target=f"customer {cid}",
                detail=f"added {len(res['added'])}, skipped {len(res['skipped'])}",
                ip=deps._ip(request),
                customer_id=cid,
            )
    return JSONResponse({"ok": True, "customer_id": cid, **res})


@router.post("/api/customers/{cid}/archive")
async def api_customers_archive(cid: int, request: Request):
    """Remote-CLI/web parity for customer archive (SPEC-customer-archive). ADMIN-only
    (same posture as delete). Retires the customer from every picker; existing projects
    keep working (no cascade). Org+customer bound via _customer_in_org. Idempotent-safe
    (archiving an already-archived customer is a clean 400)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        # Ring BEFORE role (permanent route-sweep finding): a foreign-customer id is
        # 404 (not enumerable), never a 403 that leaks its existence.
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        try:
            customer_service.archive_customer(
                s, cid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except customer_service.CustomerArchiveError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "customer_id": cid, "archived": True})


@router.post("/api/customers/{cid}/unarchive")
async def api_customers_unarchive(cid: int, request: Request):
    """Remote-CLI/web parity for customer unarchive (SPEC-customer-archive). ADMIN-only.
    Restores the customer to every picker. Org bound via _customer_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        try:
            customer_service.unarchive_customer(
                s, cid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except customer_service.CustomerArchiveError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "customer_id": cid, "archived": False})


# ============================================================================
# R1a content-parity: Customer 360 detail + settings/defaults/pairs (SPEC-full-
# remote-parity Wave R1). The detail read is can_manage; the settings/defaults/pairs
# writes are can_configure_customer (admin, manager or linguist; 2026-09-26,
# translators/reviewers/viewers refused per #284). Org+customer bound via
# _customer_in_org: a foreign id 404s, and a customer-BOUND caller may only ever
# reach its OWN customer (it can never be can_manage anyway, per
# CUSTOMER_BINDABLE_ROLES, so this is belt-and-suspenders).
# Thin shells over app/services/customers.py (get_customer_360 / update_customer_
# settings / set_customer_defaults / add_pair / remove_pair).
# ============================================================================
@router.get("/api/customers/{cid}/detail")
def api_customer_detail(cid: int, request: Request):
    """The customer-360 view (mirrors `customer show --json`). can_manage. Every list
    inside is scoped to this one customer by the service; nothing cross-customer is
    computed."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        from app.kernel import access

        data = customer_service.get_customer_360(
            s, cid, org_id=org_id, visible_pids=access.visible_project_ids(s, ident.get("user"))
        )
        if data is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, **data})


@router.post("/api/customers/{cid}/ai-opt-out")
async def api_customer_ai_opt_out(cid: int, request: Request):
    """AI opt-out for EVERY project of a customer: `{"ai_opt_out": true}`
    switches AI off for all its projects, current and future; `false` switches the
    customer-level opt-out off again (project-level opt-outs stay). Manager or admin.
    Ring before role. Audited (customer.ai_opt_out / customer.ai_opt_in)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import ai_optout

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        value = ai_optout.parse_switch(body.get("ai_opt_out"))
        if value is None:
            return JSONResponse(
                {"ok": False, "error": "ai_opt_out must be true (AI off) or false (AI on)."},
                status_code=400,
            )
        cust = ai_optout.set_customer(
            s, cid, value, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
        )
        if cust is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = {"ok": True, "id": cid, "ai_opt_out": bool(cust.ai_opt_out)}
    return JSONResponse(out)


@router.post("/api/customers/{cid}/settings")
async def api_customer_settings(cid: int, request: Request):
    """Merge preferences into the customer's settings blob (mirrors `customer update
    --set KEY=VALUE`). can_configure_customer. A key whose value is "" / null is DROPPED (a cleared
    field removes the preference). Org bound via _customer_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _customer_config_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    updates = body.get("settings")
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        # SCOPE FIRST, validate second (the existence-oracle law + the by-id sweep):
        # the ring 404 must precede the bad-body 400, or the difference leaks existence.
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not isinstance(updates, dict):
            return JSONResponse(
                {"ok": False, "error": "settings must be an object of KEY: VALUE."}, status_code=400
            )
        cust = customer_service.update_customer_settings(s, cid, updates)
        audit.log(
            actor=ident["actor"],
            action="customer.settings",
            target=f"customer {cid}",
            detail=f"keys: {', '.join(sorted(str(k) for k in updates)) or 'none'}",
            ip=deps._ip(request),
            customer_id=cid,
        )
        out = dict(cust.settings or {})
    return JSONResponse({"ok": True, "id": cid, "settings": out})


@router.post("/api/customers/{cid}/defaults")
async def api_customer_defaults(cid: int, request: Request):
    """Set the customer's scoped defaults (mirrors `customer set-default`). can_configure_customer.
    Only fields PRESENT in the body change; a blank string clears one. default_workflow_id
    / segmentation / content-type are validated against this customer's own scope by the
    service (a foreign-customer id is a clean 400, never stored). Org bound via
    _customer_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _customer_config_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import customers as customer_service

    kwargs = {}
    if "provider" in body:
        kwargs["provider"] = body.get("provider")
    if "workflow_id" in body:
        kwargs["workflow_id"] = body.get("workflow_id")
    if "segmentation_profile_id" in body:
        kwargs["segmentation_profile_id"] = body.get("segmentation_profile_id")
    if "content_type_id" in body:
        kwargs["content_type_id"] = body.get("content_type_id")
    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            cust = customer_service.set_customer_defaults(
                s, cid, user=(ident or {}).get("user"), **kwargs
            )
        except content.MockEngineAdminOnlyError as exc:
            # #577: choosing the offline Mock engine is an administrator decision.
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        audit.log(
            actor=ident["actor"],
            action="customer.set_default",
            target=f"customer {cid}",
            ip=deps._ip(request),
            customer_id=cid,
        )
        out = {
            "default_provider": cust.default_provider,
            "default_workflow_id": cust.default_workflow_id,
            "default_segmentation_profile_id": cust.default_segmentation_profile_id,
            "default_content_type_id": cust.default_content_type_id,
        }
    return JSONResponse({"ok": True, "id": cid, **out})


@router.post("/api/customers/{cid}/pairs/add")
async def api_customer_pairs_add(cid: int, request: Request):
    """Add one "src->tgt" active language pair (mirrors `customer pairs add`).
    can_configure_customer. A malformed pair (or one not in the BCP-47 catalog) is a clean 400.
    Idempotent (`added` False when already present). Org bound via _customer_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _customer_config_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            added, canon = customer_service.add_pair(s, cid, str(body.get("pair") or ""))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if added:
            audit.log(
                actor=ident["actor"],
                action="customer.pair_add",
                target=f"customer {cid} {canon}",
                ip=deps._ip(request),
                customer_id=cid,
            )
    return JSONResponse({"ok": True, "customer_id": cid, "pair": canon, "added": added})


@router.post("/api/customers/{cid}/pairs/remove")
async def api_customer_pairs_remove(cid: int, request: Request):
    """Remove one active language pair (mirrors `customer pairs remove`). can_configure_customer.
    Accepts a canonical or loosely-formatted pair; `removed` False when it was not
    present. Org bound via _customer_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _customer_config_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import customers as customer_service

    with Session(engine) as s:
        if _customer_in_org(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            removed, canon = customer_service.remove_pair(s, cid, str(body.get("pair") or ""))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if removed:
            audit.log(
                actor=ident["actor"],
                action="customer.pair_remove",
                target=f"customer {cid} {canon}",
                ip=deps._ip(request),
                customer_id=cid,
            )
    return JSONResponse({"ok": True, "customer_id": cid, "pair": canon, "removed": removed})
