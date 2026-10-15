"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    access,
    audit,
    security,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _ai_block,
    _cust_of,
    _gate,
    _gate_admin,
    _int_id,
    _json,
    _org_list,
    _remote_admin_block,
    _resolve_user_ref,
    _user_in_org,
)

router = APIRouter()


# ============================================================================
# Routing rule read-back — remote arm of `admin routing rule show`. RBAC mirrors
# the web /routing surface (deps._admin_block): admin-only. Org-scoped read (a
# cross-org id 404s, indistinguishable from missing). Single shared serializer
# (routing.rule_to_dict) so /api and both CLI arms never drift on row shape.
# ============================================================================
@router.get("/api/routing/rules/{rule_id}")
def api_routing_rule_show(rule_id: int, request: Request):
    blocked = deps.feature_api_block("routing")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    from app.kernel import routing

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    admin_blocked = _admin_block(ident)  # web /routing parity: admin-only
    if admin_blocked:
        return admin_blocked
    with Session(engine) as session:
        rule = routing.get_rule(session, rule_id, org_id=org_id)
        if rule is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"ok": True, **routing.rule_to_dict(rule)})


# ============================================================================
# API-key enumeration (system-wide, metadata ONLY) — remote arm of `apikey list`.
# RBAC: admin-only (a regular user is refused; their own keys are on /account). The
# lsu_ token is ALWAYS org-bound (the linchpin — never super-admin over /api), so an
# org admin sees ONLY their own org's keys; cross-org isolation is by construction.
# The key material/hash is NEVER emitted (apikeys.key_metadata_dict excludes it).
# ============================================================================
@router.get("/api/apikeys")
def api_apikeys_list(request: Request, user: str = ""):
    from app.kernel import apikeys

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    admin_blocked = _admin_block(ident)  # admin-only: a regular user is refused
    if admin_blocked:
        return admin_blocked
    with Session(engine) as session:
        uid = None
        ref = (user or "").strip()
        if ref:
            from app.models import User

            ref_int = _int_id(ref)
            u = (
                session.get(User, ref_int)
                if ref_int is not None
                else session.exec(select(User).where(User.email == ref.lower())).first()
            )
            # A cross-org (or missing) owner is not-found (empty) — no existence oracle.
            if u is None or (org_id is not None and getattr(u, "org_id", None) != org_id):
                return JSONResponse({"ok": True, "keys": []})
            uid = u.id
        payload = apikeys.list_keys_scoped(session, org_id=org_id, user_id=uid)
    return JSONResponse({"ok": True, "keys": payload})


# ============================================================================
# Reports (R2-1) - remote surface for `report run --remote`
# ============================================================================
@router.get("/api/reports/run")
def api_reports_run(request: Request):
    """Run a report over the caller's in-scope data. Query params mirror the CLI:
    kind, group_by, from, to, customer_id, project_id, provider, format.

    A READ endpoint: NO write-role gate (consistent with the other /api GET reads).
    _gate binds the caller to their org AND stashes their customer confinement on the
    ident; run_report applies BOTH via the resolved User (visible_project_ids) - a
    customer-bound lsu_ key can only ever aggregate its own customer, and a foreign
    ?customer_id is clamped to their own (never the foreign data). ?format=csv returns
    text/csv; otherwise JSON rows."""
    blocked = deps.feature_api_block("reports")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    from app.kernel import reports as reports_service

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    qp = request.query_params

    def _int(name):
        raw = (qp.get(name) or "").strip()
        try:
            n = int(raw) if raw else None
        except (TypeError, ValueError):
            return None
        # Bound the overflow edge: a value wider than signed 64 bits (a 40-nines typo /
        # fuzzer) survives int() as a bigint but raises a raw OverflowError at the
        # SQLite bind in run_report -- treat it as an unset filter, never a 500.
        return n if n is None or -(2**63) <= n < 2**63 else None

    try:
        q = reports_service.ReportQuery(
            kind=(qp.get("kind") or "cost"),
            group_by=(qp.get("group_by") or "none"),
            date_from=reports_service.parse_date(qp.get("from")),
            date_to=reports_service.parse_date(qp.get("to")),
            customer_id=_int("customer_id"),
            project_id=_int("project_id"),
            provider=(qp.get("provider") or "").strip() or None,
        ).normalized()
    except reports_service.UnknownReportKind as exc:
        # #25: an unknown ?kind= is a clean 400, not a silent coercion to 'cost'.
        # (Ordered before the generic ValueError below since UnknownReportKind IS a
        # ValueError — it must win to give the kind-specific message.)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad date - use YYYY-MM-DD."}, status_code=400)
    # The caller's User carries org + customer confinement (visible_project_ids);
    # org_id is threaded too for defense in depth (same-org, so a no-op intersect).
    user = (ident or {}).get("user")
    # CV-4: the cost kind is manager+ (spend data). Refuse an under-ranked key with the
    # standard 403 envelope; tokens/volume stay viewer-readable. No-op when the login wall
    # is off (single-tenant trusted, mirroring _role_block). can_see_cost's None arm is the
    # in-process CLI local path, not this /api route (a keyed caller always resolves a User).
    if q.kind == "cost" and security.auth_required() and not reports_service.can_see_cost(user):
        return JSONResponse(
            {"ok": False, "error": "cost reports require the manager role (or higher)."},
            status_code=403,
        )
    # SPEC-wave2 §4.4 + 2026-08-19: the qa/postedit `actor` axis is manager+ (it ranks
    # named people — for postedit the bucket key is an editor's EMAIL, PII). Unlike the cost
    # gate above, this gate is ALWAYS-ON: it does NOT condition on security.auth_required(),
    # so a viewer-tier key is refused the actor axis even with the login wall OFF (auth-off no
    # longer leaks editor emails to any docs-scoped key). _require_doc_scope always resolves a
    # real per-user key here (keyless -> 401), so `user` carries the key's real role; the None
    # trusted-operator arm belongs to the in-process CLI, never this route. The web route
    # resolves SYSTEM admin under auth-off (clears can_manage), so single-tenant web is 200.
    if q.group_by == "actor" and not reports_service.can_see_actor_axis(user):
        return JSONResponse(
            {"ok": False, "error": "the actor axis requires the manager role (or higher)."},
            status_code=403,
        )
    with Session(engine) as s:
        report = reports_service.run_report(s, q, user=user, org_id=org_id)
    fmt = (qp.get("format") or "").strip().lower()
    if fmt == "csv":
        from fastapi.responses import Response

        csv_text = reports_service.to_csv(report)
        fname = reports_service.csv_filename(q.kind, q.group_by)
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    return JSONResponse(
        {
            "ok": True,
            "kind": report["kind"],
            "group_by": report["group_by"],
            "columns": report["columns"],
            "key_column": report["key_column"],
            "rows": report["rows"],
            "totals": reports_service.totals_row(report),
            "caveats": report.get("caveats") or [],
        }
    )


# ============================================================================
# TM management module (browse / usage / lifecycle) — /api/tm
# ============================================================================
# Scoping: SegmentTM has no org/customer column of its own, so visibility is resolved
# through project_id -> Project via access.visible_project_ids(session, user) (the
# Reports v1 seam) — org + membership + R2-3 customer confinement in one set. A
# customer-bound lsu_ key can only ever see/delete its own customer's TM; a foreign
# entry id 404s. Reads (list/show/stats) carry NO write-role gate (consistent with
# every other /api GET). DELETE (single + bulk) mirrors the web can_manage gate.
def _tm_pids(s: Session, ident):
    """The visible project-id scope set for a gated API caller (None = unrestricted)."""
    return access.visible_project_ids(s, (ident or {}).get("user"))


class _TmBadCustomer(ValueError):
    """#610: an unusable ?customer_id= on the TM browse (answered as HTTP 400)."""


def _tm_requested_customer(qp):
    """The customer a TM browse asks for. `customer` is the documented parameter; `customer_id`
    is accepted as the same thing because every other /api route spells it that way (a script
    that sent it got `needs_customer` and an empty list with no hint why). A non-numeric value,
    or two different values, is a 400 rather than a silent fallback."""
    named = (qp.get("customer") or "").strip()
    alias = (qp.get("customer_id") or "").strip()
    if not alias:
        return qp.get("customer")
    if not alias.isdigit():
        raise _TmBadCustomer("customer_id must be a customer id (a whole number).")
    if named and named != alias:
        raise _TmBadCustomer("customer and customer_id disagree; send only one of them.")
    return alias


def _tm_browse(s: Session, ident, org_id, qp):
    """Resolve the customer-FIRST browse for /api/tm (2026-07-14: TM is
    organized by customer, there is NO global TM). Returns (selected_customer, pids,
    customers):
      * a customer-bound key (R2-3) is FORCED to its own customer;
      * a ?customer= that resolves in the visible set selects it;
      * exactly ONE visible customer auto-selects;
      * otherwise selected is None and the caller returns needs_customer + the picker
        list rather than an all-customers TM dump.
    `pids` is the base visible set narrowed to the chosen customer's projects."""
    from app.kernel import tm_module

    bound = _cust_of(ident)
    customers = tm_module.browsable_customers(s, org_id=org_id, bound_customer_id=bound)
    selected = tm_module.resolve_browse_customer(customers, _tm_requested_customer(qp))
    if selected is None:
        return None, None, customers
    pids = tm_module.scoped_pids_for_customer(s, _tm_pids(s, ident), selected.id)
    return selected, pids, customers


def _needs_customer_payload(customers, **extra):
    return {
        "ok": True,
        "needs_customer": True,
        "customers": [{"id": c.id, "name": c.name} for c in customers],
        "entries": [],
        "total": 0,
        **extra,
    }


def _tm_int(qp, name):
    raw = (qp.get(name) or "").strip()
    try:
        n = int(raw) if raw else None
    except (TypeError, ValueError):
        return None
    # Bound the overflow edge (mirrors _int above): a >2**63 value binds cleanly through
    # int() but raises OverflowError at the SQLite bind in the TM browse query. Preserve
    # the 0/negative domain (min_use/max_use/page can legitimately be 0) -- only the
    # 64-bit overflow becomes an unset value.
    return n if n is None or -(2**63) <= n < 2**63 else None


def _tm_date(qp, name):
    from datetime import datetime, timezone

    raw = (qp.get(name) or "").strip()
    if not raw:
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _tm_content_type_filter(qp):
    """The content-type browse filter for /api/tm (SPEC-content-types phase 3).
    Blank = no filter; "__none__" = the General (NULL) bucket; a digit = that type id."""
    raw = (qp.get("content_type_id") or "").strip()
    if not raw:
        return None
    if raw == "__none__":
        return "__none__"
    return raw if raw.isdigit() else None


def _tm_filters_from_qp(qp):
    return {
        "source_locale": (qp.get("source_locale") or "").strip() or None,
        "target_locale": (qp.get("target_locale") or "").strip() or None,
        "project_id": _tm_int(qp, "project_id"),
        "content_type_id": _tm_content_type_filter(qp),
        "q": (qp.get("q") or "").strip() or None,
        "created_before": _tm_date(qp, "created_before"),
        "created_after": _tm_date(qp, "created_after"),
        "min_use": _tm_int(qp, "min_use"),
        "max_use": _tm_int(qp, "max_use"),
        "unused": (qp.get("unused") or "") in ("1", "true", "yes", "on"),
    }


@router.get("/api/tm")
def api_tm_list(request: Request):
    from app.kernel import tm_module

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    qp = request.query_params
    page = _tm_int(qp, "page") or 1
    per_page = _tm_int(qp, "per_page") or 50
    order = (qp.get("order") or "recent").strip()
    with Session(engine) as s:
        try:
            selected, pids, customers = _tm_browse(s, ident, org_id, qp)
        except _TmBadCustomer as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if selected is None:
            return JSONResponse(
                _needs_customer_payload(customers, page=page, per_page=per_page, pages=0)
            )
        res = tm_module.list_entries(
            s,
            page=page,
            per_page=per_page,
            order=order,
            visible_pids=pids,
            **_tm_filters_from_qp(qp),
        )
    return JSONResponse({"ok": True, "customer_id": selected.id, **res})


@router.get("/api/tm/stats")
def api_tm_stats(request: Request):
    from app.kernel import tm_module

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    qp = request.query_params
    with Session(engine) as s:
        try:
            selected, pids, customers = _tm_browse(s, ident, org_id, qp)
        except _TmBadCustomer as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if selected is None:
            return JSONResponse(_needs_customer_payload(customers))
        summary = tm_module.stats(s, visible_pids=pids, top_n=10)
    return JSONResponse({"ok": True, "customer_id": selected.id, **summary})


@router.get("/api/tm/{entry_id}")
def api_tm_show(entry_id: int, request: Request):
    from app.kernel import tm_module

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        pids = _tm_pids(s, ident)
        e = tm_module.get_entry(s, entry_id, visible_pids=pids)
        if e is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"ok": True, **tm_module.entry_to_dict(e)})


@router.post("/api/tm/{entry_id}/delete")
def api_tm_delete(entry_id: int, request: Request):
    from app.kernel import tm_module

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    gate = _ai_block(ident)  # web parity: delete is a can_tune_ai act (per owner, no manager gate)
    if gate:
        return gate
    with Session(engine) as s:
        pids = _tm_pids(s, ident)
        ok = tm_module.delete_entry(s, entry_id, visible_pids=pids)
        if not ok:
            return JSONResponse(_NOT_FOUND, status_code=404)
        audit.log(actor=ident["actor"], action="tm.delete", target=f"tm entry {entry_id}")
    return JSONResponse({"ok": True, "count": 1, "entry_id": entry_id})


@router.post("/api/tm/{entry_id}/edit")
async def api_tm_edit(entry_id: int, request: Request):
    """Edit a TM entry's TARGET in place — web /tm/{id}/edit parity: same can_tune_ai
    gate, same scope, same refusal choke point (tm_module.edit_entry). Source is not
    editable. Body: {target}."""
    from app.kernel import tm_module

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    gate = _ai_block(ident)  # web parity: edit is a can_tune_ai act (same as delete)
    if gate:
        return gate
    body = await _json(request)
    target = str(body.get("target") or "")
    # #567: only a JSON boolean true acknowledges a placeholder loss (a string is refused;
    # any other shape means no acknowledgement = fail closed).
    ack_raw = body.get("acknowledge_placeholder_loss", False)
    if not isinstance(ack_raw, bool):
        return JSONResponse(
            {"ok": False, "error": "acknowledge_placeholder_loss must be a JSON boolean."},
            status_code=400,
        )
    with Session(engine) as s:
        pids = _tm_pids(s, ident)
        try:
            e = tm_module.edit_entry(
                s,
                entry_id,
                target,
                visible_pids=pids,
                actor=ident["actor"],
                acknowledge_placeholder_loss=ack_raw,
            )
        except tm_module.TMEditIntegrityRefused as exc:
            return JSONResponse({"ok": False, "error": str(exc), "code": exc.code}, status_code=400)
        except (tm_module.TMEditRefused, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if e is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"ok": True, **tm_module.entry_to_dict(e)})


@router.post("/api/tm/bulk-delete")
async def api_tm_bulk_delete(request: Request):
    from app.kernel import tm_module

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    gate = _ai_block(ident)  # per the maintainer, no manager gate -- same as single delete
    if gate:
        return gate
    body = await _json(request)
    try:
        filters = tm_module.strict_bulk_filters(body)
        dry_run = tm_module.parse_flag(body.get("dry_run"), "dry_run")
        confirm_all = tm_module.parse_flag(body.get("confirm_all"), "confirm_all")
    except tm_module.BulkDeleteRefused as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    no_filters = all(v in (None, False) for v in filters.values())
    # MEDIUM finding fix: a filterless bulk-delete wipes the caller's ENTIRE visible
    # TM with no count-first confirm step (unlike the web form, which always shows
    # a count before deleting, and the CLI, which always dry-runs first). Require an
    # explicit confirm_all:true opt-in for the no-filter case; filtered deletes and
    # dry-runs are unaffected.
    if not dry_run and no_filters and not confirm_all:
        return JSONResponse(
            {
                "ok": False,
                "error": "refusing an unfiltered bulk-delete without confirm_all: true "
                "(this would delete the entire visible TM). Add a filter, or "
                "pass confirm_all: true to proceed.",
            },
            status_code=400,
        )
    with Session(engine) as s:
        # #490: the delete (and its dry-run count) is scoped SERVER side to one
        # customer; a missing or foreign customer on a multi-customer ring is refused.
        customers = tm_module.browsable_customers(
            s, org_id=_org_id, bound_customer_id=_cust_of(ident)
        )
        try:
            pids = tm_module.resolve_bulk_delete_scope(
                s,
                _tm_pids(s, ident),
                customers=customers,
                customer=body.get("customer"),
                project_id=filters["project_id"],
            )
        except tm_module.BulkDeleteRefused as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        n = tm_module.bulk_delete(s, dry_run=dry_run, visible_pids=pids, **filters)
        if not dry_run and n:
            scope = tm_module.scope_label(body.get("customer"), filters["project_id"])
            audit.log(
                actor=ident["actor"], action="tm.bulk_delete", target=f"{n} tm entries ({scope})"
            )
    return JSONResponse({"ok": True, "count": n, "dry_run": dry_run})


@router.post("/api/tm/scrub-hedges")
async def api_tm_scrub_hedges(request: Request):
    """Historical hedge scrub (Deliverable 4): DRY-RUN by default (report matches);
    apply:true deletes the poisoned segment + full-text TM entries. org-scoped +
    can_tune_ai (web parity with the other TM lifecycle acts); apply is audit-logged
    inside tm_module.scrub_hedges."""
    from app.kernel import tm_module

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    gate = _ai_block(ident)  # can_tune_ai, mirroring the other TM lifecycle endpoints
    if gate:
        return gate
    body = await _json(request)
    # #490: like bulk-delete this mutates TM, so it is scoped SERVER side to ONE customer
    # (customer required when the caller sees more than one, or a project filter) and a
    # malformed input is refused, never dropped (a dropped `project` widened the scrub).
    try:
        apply = tm_module.parse_flag(body.get("apply"), "apply")
        project_id = tm_module.strict_bulk_filters({"project_id": body.get("project")})[
            "project_id"
        ]
    except tm_module.BulkDeleteRefused as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    with Session(engine) as s:
        customers = tm_module.browsable_customers(
            s, org_id=_org_id, bound_customer_id=_cust_of(ident)
        )
        try:
            pids = tm_module.resolve_bulk_delete_scope(
                s,
                _tm_pids(s, ident),
                customers=customers,
                customer=body.get("customer"),
                project_id=project_id,
                what="scrub-hedges",
            )
        except tm_module.BulkDeleteRefused as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        res = tm_module.scrub_hedges(
            s,
            apply=apply,
            actor=ident["actor"],
            visible_pids=pids,
            project_id=project_id,
            scope_label=tm_module.scope_label(body.get("customer"), project_id),
        )
    return JSONResponse({"ok": True, **res})


# --- API keys --------------------------------------------------------------
@router.post("/api/apikeys/issue")
async def api_apikey_issue(request: Request):
    """Mint a per-user API key (mirrors `apikey issue`). is_admin, docs scope. Body:
    user (id/email, must be the caller), name, scopes. The plaintext key
    is in the response ONCE and is unrecoverable + never logged. CRITICAL LAW: minting
    an admin- or SCIM-scoped key is REFUSED here (400). Privileged mints stay local so a
    stolen admin key cannot self-replicate. The owner is RINGED to the caller's org."""
    from app.kernel import apikeys, audit
    from app.models import User

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    if ident.get("auth_kind") == "mcp_oauth":
        # #753: a consented MCP OAuth connection is short-lived and revocable; letting it mint
        # a long-lived lsu_ key would turn a one-hour, revocable grant into a permanent one.
        return JSONResponse(
            {
                "ok": False,
                "error": "An app connected through MCP OAuth cannot mint API keys; create keys "
                "on your account page.",
            },
            status_code=403,
        )
    body = await _json(request)
    scopes = body.get("scopes")
    # Input hygiene (adversarial review, LOW): a non-string scopes value (a list/dict)
    # would str()-cast into a junk grant that gets stored ("['admin'] api"). Reject it
    # rather than persist noise; and reject any UNKNOWN scope token (only api/docs/keys/
    # admin exist) so a typo can't quietly mint a key granting nothing.
    if scopes in (None, ""):
        scopes = "docs"
    elif not isinstance(scopes, str):
        return JSONResponse(
            {"ok": False, "error": "scopes must be a space/comma-separated string."},
            status_code=400,
        )
    requested = apikeys.scope_set(scopes)
    unknown = sorted(requested - apikeys.ALL_SCOPES)
    if unknown:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown scope(s): {} (valid: {}).".format(
                    ", ".join(unknown), ", ".join(sorted(apikeys.ALL_SCOPES))
                ),
            },
            status_code=400,
        )
    # THE admin-mint refusal: reject BEFORE creating anything, whatever spelling.
    if requested & {"admin", "scim"}:
        return JSONResponse(
            {
                "ok": False,
                "error": "minting an admin-scoped or SCIM-scoped key is not allowed over "
                "/api; admin-scope keys must be issued locally so a stolen admin key "
                "cannot self-replicate.",
            },
            status_code=400,
        )
    with Session(engine) as s:
        ref = body.get("user")
        cu = ident.get("user")
        # Never resolve another account: existing, foreign and unknown references
        # receive the same refusal, without creating an account-existence oracle.
        own_refs = {str(cu.id), cu.email.casefold()} if cu is not None else set()
        if ref not in (None, "") and str(ref).strip().casefold() not in own_refs:
            return JSONResponse(
                {"ok": False, "error": "You may issue keys only for your own account."},
                status_code=403,
            )
        owner = s.get(User, cu.id) if cu is not None else None
        if owner is None:
            return JSONResponse({"ok": False, "error": "no target user."}, status_code=400)
        owner_id, owner_email = owner.id, owner.email
    # Optional lifetime (#270 r2-apiauth-10): `expires_in_days` (a positive whole
    # number of days from now) or absent = no expiry. The org-level maximum
    # (apikey_max_age_days) is enforced at the shared mint seam; its refusal is
    # a clean 400, exactly like a bad scope.
    try:
        expires_at = apikeys.expiry_from_days(body.get("expires_in_days"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        key, full_key = apikeys.create_remote_key(
            ident.get("user"),
            owner_id,
            name=body.get("name") or "",
            scopes=scopes,
            expires_at=expires_at,
        )
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # Audit the mint WITH the scopes but NEVER the key material.
    audit.log(
        actor=ident.get("actor", ""),
        action="apikey.create",
        target=owner_email,
        detail=f"scopes={key.scopes}",
    )
    return JSONResponse(
        {
            "ok": True,
            "id": key.id,
            "user": owner_email,
            "prefix": key.prefix,
            "scopes": key.scopes,
            "api_key": full_key,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        }
    )


@router.post("/api/apikeys/{kid}/revoke")
def api_apikey_revoke(request: Request, kid: int):
    """Revoke one API key by id (mirrors `apikey revoke`). is_admin, docs scope. The
    key's OWNER is ringed to the caller's org — a key owned by another org's user is a
    404, never revoked."""
    from app.kernel import apikeys, audit
    from app.models import ApiKey

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    kid = _int_id(kid)
    with Session(engine) as s:
        key = s.get(ApiKey, kid) if kid is not None else None
        if key is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # Ring the key's owner to the caller's org (a cross-org key is non-enumerable).
        if _user_in_org(s, key.user_id, org_id) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
    if not apikeys.revoke(kid):
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(actor=ident.get("actor", ""), action="apikey.revoke", target=str(kid))
    return JSONResponse({"ok": True, "id": kid, "revoked": True})


@router.post("/api/apikeys/revoke-all")
async def api_apikey_revoke_all(request: Request):
    """Revoke EVERY key a user holds (mirrors `apikey revoke-all`). BULK-DESTRUCTIVE:
    `admin` scope + is_admin role + `remote_admin_enabled`. Body: user (id/email),
    ringed to the caller's org."""
    from app.kernel import apikeys, audit

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    rb = _remote_admin_block(ident)
    if rb:
        return rb
    body = await _json(request)
    with Session(engine) as s:
        owner = _resolve_user_ref(s, body.get("user"), org_id)
        if owner is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        owner_id, owner_email = owner.id, owner.email
    n = apikeys.revoke_all_for_user(owner_id, org_id=org_id)
    audit.log(
        actor=ident.get("actor", ""),
        action="apikey.revoke_all",
        target=owner_email,
        detail="%d key(s) revoked" % n,  # noqa: UP031
    )
    return JSONResponse({"ok": True, "user": owner_email, "revoked": n})


# ============================================================================
# Wave R2 — admin ops (is_admin, docs scope): routing rules, department budgets,
# LQA rulebooks, semantic TM, SRX segmentation, retention, audit, languages. Each
# mirrors the `admin` / `lang` CLI seam and its web RBAC (is_admin). The install-
# GLOBAL settings toggles (semantic/srx/retention) change how the install behaves
# for an ADMIN over /api — a deliberate R2 extension of the old "config is
# local-only" rule (llm.md updated in-train); the perimeter switch
# remote_admin_enabled still guards the one DESTRUCTIVE verb (purge-now) below.
# ============================================================================
# --- routing rules ---------------------------------------------------------
@router.post("/api/routing/rules")
async def api_routing_rule_add(request: Request):
    """Create a content-routing rule (mirrors `admin routing rule add`). is_admin, docs
    scope, org-stamped. Body: ext, department, priority, project, provider. A rule
    matches on ext + department and targets a project and/or provider. `subject`,
    `workflow` and `language_set` are RESERVED FOR V2 and refused (400) if present and
    non-empty -- no reader consults them at run time yet. A bad-FK rule is a clean 400."""
    blocked = deps.feature_api_block("routing")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    from app.kernel import routing

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    with Session(engine) as s:
        # Reserved-for-v2 fields (subject/workflow/language-set) are consulted by no
        # reader in v1 -- refuse them (400) before storing dead config (QW-D trap),
        # byte-identical to the CLI + web form. Checked on the RAW body values BEFORE
        # coercion (MED-2): a present, non-empty reserved key refuses regardless of
        # coercibility, so workflow=0/-1/"abc"/true/10**20 cannot be swallowed by
        # _int_id->None and silently create a rule (the BUG-A3-2 "honoured or refused,
        # never quietly rewritten" rule, applied to the reserved keys). Absent/empty is
        # allowed. The MCP add_rule tool relays this 400.
        dead = routing.reserved_field_error(
            match_subject=body.get("subject"),
            workflow_id=body.get("workflow"),
            language_set_id=body.get("language_set"),
        )
        if dead:
            return JSONResponse({"ok": False, "error": dead}, status_code=400)
        pid = _int_id(body.get("project")) if body.get("project") not in (None, "") else None
        wid = _int_id(body.get("workflow")) if body.get("workflow") not in (None, "") else None
        lsid = (
            _int_id(body.get("language_set"))
            if body.get("language_set") not in (None, "")
            else None
        )
        err = routing.validate_rule(
            s, workflow_id=wid, project_id=pid, language_set_id=lsid, org_id=org_id
        )
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        # BUG-A3-2: a non-int `priority` is a clean 400 (matching /api/budgets' handling
        # of a bad `limit`), NOT silently coerced to the default 100 — the caller's
        # stated priority is honoured or refused, never quietly rewritten. Absent =>
        # the documented default 100.
        raw_priority = body.get("priority", 100)
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "priority must be an integer."}, status_code=400
            )
        try:
            rule = routing.create_rule(
                s,
                priority=priority,
                match_ext=body.get("ext") or "",
                match_subject=body.get("subject") or "",
                department=body.get("department") or "",
                project_id=pid,
                workflow_id=wid,
                language_set_id=lsid,
                provider=body.get("provider") or "",
                actor=ident.get("actor", ""),
                org_id=org_id,
            )
        except routing.RoutingRuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **routing.rule_to_dict(rule)})


@router.get("/api/routing/rules")
def api_routing_rule_list(request: Request):
    """List routing rules (mirrors `admin routing rule list`), org-scoped. is_admin."""
    blocked = deps.feature_api_block("routing")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    from app.kernel import routing
    from app.models import RoutingRule

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        stmt = _org_list(select(RoutingRule), RoutingRule, org_id)
        rules = s.exec(stmt.order_by(RoutingRule.priority, RoutingRule.id)).all()
        payload = [routing.rule_to_dict(r) for r in rules]
    return JSONResponse({"ok": True, "rules": payload})


@router.post("/api/routing/rules/{rule_id}/toggle")
def api_routing_rule_toggle(request: Request, rule_id: int):
    """Flip a routing rule active/inactive (mirrors `admin routing rule toggle`).
    is_admin, org-ringed (a cross-org id is 404)."""
    blocked = deps.feature_api_block("routing")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    from app.kernel import routing

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        rule = routing.toggle_rule(s, _int_id(rule_id), actor=ident.get("actor", ""), org_id=org_id)
        if rule is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"ok": True, "id": rule.id, "active": rule.active})


@router.post("/api/routing/rules/{rule_id}/delete")
def api_routing_rule_delete(request: Request, rule_id: int):
    """Delete a routing rule (mirrors `admin routing rule delete`). is_admin,
    org-ringed."""
    blocked = deps.feature_api_block("routing")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    from app.kernel import routing

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    rid = _int_id(rule_id)
    with Session(engine) as s:
        if not routing.delete_rule(s, rid, actor=ident.get("actor", ""), org_id=org_id):
            return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "id": rid, "deleted": True})
