"""/api routes: glossaries 1

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import (
    audit,
    glossary,
)
from app.web import deps
from app.web.api_cli_common import (
    _FORBIDDEN,
    _NOT_FOUND,
    _ai_block,
    _b64,
    _cust_of,
    _customer_in_org,
    _gate,
    _glossary_in_scope,
    _glossary_window,
    _int_id,
    _json,
    _manage_block,
    glossary_import_error_response,
)

router = APIRouter()


@router.get("/api/glossaries")
def api_glossaries_list(request: Request):
    """Remote-CLI/web parity for `glossary module list [--customer ID] [--unassigned]`
    (DATA-SAFETY chip): `?unassigned=1` mirrors the legacy-glossary review section;
    `?customer=ID` narrows to one customer (validated against the caller's org — a
    foreign/unknown id is a clean 400, same as the in-process CLI's `_get_customer`).
    Neither param given -> the org-wide (R2-3 customer-confined) list, unchanged.

    `?unassigned=1` carries the web page's TWO conditions, not just its query
    (app/web/glossaries.py glossaries_index renders that section only
    `if can_manage and my_cust is None`). `gm.list_unassigned` takes no customer_id
    — it CANNOT be customer-confined — so without both checks the filter was a
    scope-widener: a customer-BOUND lsu_ key (R2-3 confined to its own customer on
    every other list) could enumerate every org-level legacy glossary's
    name/pairs/term-count, and any viewer token could enumerate them at all."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import glossary_module as gm

    unassigned = request.query_params.get("unassigned")
    customer_raw = request.query_params.get("customer")
    with Session(engine) as s:
        if unassigned:
            blocked = _manage_block(ident)  # web: `can_manage`
            if blocked:
                return blocked
            if _cust_of(ident) is not None:  # web: `my_cust is None`
                return JSONResponse(_FORBIDDEN, status_code=403)
            gls = gm.list_unassigned(s, org_id=org_id)
        elif customer_raw:
            cid_val = _int_id(customer_raw)
            if cid_val is None or _customer_in_org(s, cid_val, org_id, ident) is None:
                return JSONResponse({"ok": False, "error": "unknown customer."}, status_code=400)
            gls = gm.list_for_customer(s, org_id, cid_val)
        else:
            gls = gm.list_glossaries(s, org_id=org_id, customer_id=_cust_of(ident))
        payload = [gm.to_dict(s, g) for g in gls]
    return JSONResponse({"ok": True, "glossaries": payload})


@router.post("/api/glossaries")
async def api_glossary_module_create(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # content edit -> can_tune_ai (web /glossaries create)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        # Customer scope (parity with the web /glossaries create + the CLI + the
        # api_content_types_create block just below): a customer-BOUND caller is
        # FORCED to its own customer (R2-3 confinement — cannot forge another). A
        # STAFF caller (bound None) may pass a customer_id in the body (validated
        # against its org) or leave it blank (global). Without this a staff linguist's
        # customer-scoped glossary was silently forced GLOBAL, so a matching
        # customer-scoped content_type_id then 400'd — a 2-surface-vs-3rd parity gap.
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
            # Customer-first: require_customer -> a missing customer
            # is a clean 400 (no "global" glossaries from any seam).
            g = gm.create(
                s,
                str(body.get("name") or ""),
                org_id=org_id,
                customer_id=cust,
                created_by=ident["actor"],
                content_type_id=body.get("content_type_id"),
                require_customer=True,
            )
        except gm.GlossaryModuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **gm.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action="glossary.create",
        target=f"glossary {out['id']}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/content-type")
async def api_glossary_module_set_content_type(gid: int, request: Request):
    """Remote-CLI/web parity for setting a glossary's content type (SPEC-content-types
    §3). can_tune_ai-gated (a content-scope edit, same as the other glossary edits).
    Blank/absent content_type_id clears it (General). A bad/foreign id is a clean 400.
    Does NOT revert an approved glossary to draft (scoping metadata, not content)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            g = gm.set_content_type(s, gid, body.get("content_type_id"))
        except gm.GlossaryModuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **gm.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action="glossary.content_type_set",
        target=f"glossary {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/rename")
async def api_glossary_module_rename(gid: int, request: Request):
    """Remote-CLI/web parity for glossary rename (SPEC-rename-delete). can_tune_ai-gated
    (matches the web glossary rename + the other glossary edits). Rename is metadata and
    does NOT revert approval (glossary_module.rename). A blank name is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            g = gm.rename(s, gid, str(body.get("name") or ""))
        except gm.GlossaryModuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **gm.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action="glossary.rename",
        target=f"glossary {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/set-customer")
async def api_glossary_module_set_customer(gid: int, request: Request):
    """One-time assignment of a LEGACY unassigned glossary to a customer (web+CLI
    parity). can_manage-gated (>= manager), audited. Assignment is only ever FROM NULL
    — a glossary that already has a customer is a clean 400 (never a reassignment
    between customers; term/TM provenance stays with the owner). A bad/foreign customer
    id is a 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        cid_val = _int_id(body.get("customer_id"))
        if cid_val is None or _customer_in_org(s, cid_val, org_id, ident) is None:
            return JSONResponse({"ok": False, "error": "unknown customer."}, status_code=400)
        try:
            g = gm.assign_customer(s, gid, cid_val)
        except gm.GlossaryModuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **gm.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action="glossary.assign_customer",
        target=f"glossary {gid} -> customer {cid_val}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.get("/api/glossaries/{gid}")
def api_glossary_module_show(gid: int, request: Request):
    """DATA-SAFETY chip follow-up: the payload now carries the same `terms` array the
    in-process CLI's `glossary module show --json` prints (id/source/target/locales/
    dnt/meta/senses) -- previously this route returned only the to_dict() summary
    (pairs + entry_count), so a --remote `glossary module show` had no term rows to
    render at all. Additive field; every existing key is unchanged."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        g = _glossary_in_scope(s, gid, org_id, ident)
        if g is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        limit, offset, err = _glossary_window(request)
        if err:
            return err
        page = glossary.window_entries(
            s,
            glossary_id=gid,
            cursor=request.query_params.get("cursor"),
            limit=limit,
            offset=offset,
        )
        entries = page["entries"]
        paging = {
            "limit": limit,
            "offset": offset,
            "truncated": page["has_more"],
            "next_cursor": page["next_cursor"],
        }
        terms = [
            {
                "id": it["entry"].id,
                "source_term": it["entry"].source_term,
                "target_term": it["entry"].target_term,
                "source_locale": it["entry"].source_locale,
                "target_locale": it["entry"].target_locale,
                "dnt": it["entry"].dnt,
                "meta": it["entry"].meta or {},
                "senses": [
                    {
                        "id": sn.id,
                        "sense_label": sn.sense_label,
                        "context_cue": sn.context_cue,
                        "target_term": sn.target_term,
                    }
                    for sn in it["senses"]
                ],
            }
            for it in entries
        ]
        return JSONResponse({"ok": True, **gm.to_dict(s, g), "terms": terms, **paging})


@router.get("/api/glossaries/{gid}/export")
def api_glossary_module_export(
    gid: int,
    request: Request,
    format: str = "csv",
    dialect: str = "tbx-basic",
    strict_portable: bool = False,
):
    """Remote-CLI/web parity for the module glossary export (item 1, CLI-parity
    backlog). A read (no write-role gate, mirrors api_glossary_module_show) -- org +
    R2-3 customer scoped via _glossary_in_scope. Bytes travel as base64 JSON, the
    same transport tm/export uses (base64.b64encode below), so a single JSON
    response carries the file without multipart plumbing.

    `?format=tbx` returns a native TBX v3 termbase instead of the CSV;
    `?format=json` returns the concept-aware `.glossary.json` (`glossary-concept/v1`),
    the no-caveat lossless format (concepts + all term metadata + status/audience/
    term_type/cross_refs/homonyms) — module glossaries only. The bytes field keeps its
    historical `csv_b64` name (back-compat for existing callers) and the response now
    states which `format` it holds. Unknown values fall back to CSV, matching the web
    route."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import glossary as gloss_engine
    from app.kernel import glossary_module as gm

    raw = (format or "").strip().lower()
    if raw == gloss_engine.TBX_FORMAT:
        fmt = gloss_engine.TBX_FORMAT
    elif raw in (gloss_engine.JSON_FORMAT, "glossary.json"):
        fmt = gloss_engine.JSON_FORMAT
    else:
        fmt = gloss_engine.CSV_FORMAT
    from app.kernel import glossary_tbx

    dia = (dialect or "tbx-basic").strip().lower()
    if dia not in ("tbx-basic", "tbx-core"):
        dia = "tbx-basic"
    drops: list[str] = []
    with Session(engine) as s:
        g = _glossary_in_scope(s, gid, org_id, ident)
        if g is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            data = gm.export_bytes(
                s, gid, fmt=fmt, dialect=dia, strict_portable=bool(strict_portable), drops_out=drops
            )
        except glossary_tbx.EmptyGlossaryError as exc:
            # concept-format (TBX/JSON) export of an empty glossary refuses (§6.4/M8).
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    is_tbx = fmt == gloss_engine.TBX_FORMAT
    return JSONResponse(
        {
            "ok": True,
            "glossary_id": gid,
            "bytes": len(data),
            "format": fmt,
            "dialect": dia if is_tbx else None,
            "strict_portable": bool(strict_portable) if is_tbx else None,
            # M1: a portable / tbx-core TBX export names the los: field types it dropped
            # (empty for a full tbx-basic export; null for non-TBX formats).
            "dropped_fields": drops if is_tbx else None,
            "csv_b64": base64.b64encode(data).decode(),
        }
    )


@router.post("/api/glossaries/{gid}/approve")
async def api_glossary_module_approve(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(
        ident
    )  # approval -> can_tune_ai (web /glossaries approve; per the maintainer, no manager gate)
    if blocked:
        return blocked
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        g = gm.approve(s, gid, approver_email=ident["actor"])
        out = {"ok": True, **gm.to_dict(s, g)}
    audit.log(
        actor=ident["actor"],
        action="glossary.approve",
        target=f"glossary {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/unapprove")
async def api_glossary_module_unapprove(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # per the maintainer, no manager gate -- same as approve
    if blocked:
        return blocked
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        g = gm.unapprove(s, gid)
        out = {"ok": True, **gm.to_dict(s, g)}
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/pairs")
async def api_glossary_module_set_pairs(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            result = gm.set_pairs(s, gid, body.get("pairs") or [])
        except gm.GlossaryModuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        # A.4-8 (M-2): the widening counts land as NEW response-body keys (never in to_dict,
        # whose shape other surfaces pin); MCP is an HTTP proxy to /api so it inherits them.
        out = {
            "ok": True,
            "bindings_created": getattr(result, "bindings_created", 0),
            "dnt_bindings_extended": getattr(result, "dnt_bindings_extended", 0),
            **gm.to_dict(s, gm.get(s, gid)),
        }
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/terms")
async def api_glossary_module_add_term(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            entry = gm.add_term(
                s,
                gid,
                str(body.get("source_term") or ""),
                target_term=str(body.get("target_term") or ""),
                source_locale=str(body.get("source") or "").strip(),
                target_locale=str(body.get("target") or "").strip(),
                dnt=bool(body.get("dnt")),
                meta=body.get("meta") if isinstance(body.get("meta"), dict) else None,
            )
        except gm.GlossaryModuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if entry is None:
            return JSONResponse({"ok": False, "error": "source term is blank."}, status_code=400)
        out = {
            "ok": True,
            "glossary_id": gid,
            "source_term": entry.source_term,
            "target_term": entry.target_term,
            "dnt": entry.dnt,
            "meta": entry.meta or {},
        }
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/terms/{eid}/edit")
async def api_glossary_module_edit_term(gid: int, eid: int, request: Request):
    """Edit a term's target/DNT + TBX-ish metadata (web+CLI parity for the row editor).
    can_tune_ai-gated. `meta` (dict) REPLACES the term's metadata (cleaned/allowlisted);
    target_term/dnt update only when present in the body. Reverts approval (content
    edit). A term not owned by this glossary is a clean 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        entry = gm.edit_term(
            s,
            gid,
            eid,
            target_term=(str(body["target_term"]) if "target_term" in body else None),
            dnt=(bool(body["dnt"]) if "dnt" in body else None),
            meta=(body.get("meta") if isinstance(body.get("meta"), dict) else None),
            case_sensitive=(bool(body["case_sensitive"]) if "case_sensitive" in body else None),
        )
        if entry is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = {
            "ok": True,
            "glossary_id": gid,
            "entry_id": entry.id,
            "source_term": entry.source_term,
            "target_term": entry.target_term,
            "dnt": entry.dnt,
            "case_sensitive": entry.case_sensitive,
            "meta": entry.meta or {},
        }
    return JSONResponse(out)


_CONCEPT_FIELDS = (
    "external_id",
    "domain",
    "subdomain",
    "definition",
    "notes",
    "source",
    "homonym_group",
    "created_by",
)


_TERM_FIELDS = (
    "term",
    "locale",
    "status",
    "term_type",
    "audience",
    "part_of_speech",
    "usage_note",
    "context_example",
    "geographical_usage",
    "grammatical_gender",
    "grammatical_number",
    "source",
    "case_sensitive",
    "exact",
    "dnt",
)


# ============================================================================
# Concept model (SPEC-terminology-backend T1). The rich concept/term store that
# sits BENEATH the compat entry/sense rows. Every route is nested under
# `/concepts/` so it can never collide with the shipped compat-entry `/terms`
# routes above (H5: FastAPI route identity is the path TEMPLATE, so a bare
# `/terms/{tid}/edit` would silently duplicate `/terms/{eid}/edit` over a
# different id space). Reads mirror api_glossary_module_show (no write gate);
# writes are can_tune_ai-gated (content edits) exactly like the other glossary
# mutations. Scope is the SAME _glossary_in_scope seam (LAW-TERM-12 — no new
# authz). ConceptError (bad input / not found) maps to a clean 400, mirroring the
# GlossaryModuleError handling above. The wire shape is `glossary-concept/v1`.
# ============================================================================
def _concept_fields(body: dict) -> dict:
    """The concept-level fields a caller may set, filtered off the request body so a
    stray key can never collide with a positional service arg."""
    return {k: body[k] for k in _CONCEPT_FIELDS if k in body}


def _term_fields(body: dict) -> dict:
    """The term-level fields a caller may set (the concept-term `tdoc`)."""
    return {k: body[k] for k in _TERM_FIELDS if k in body}


@router.get("/api/glossaries/{gid}/concepts")
def api_glossary_concepts_list(gid: int, request: Request):
    """The glossary's concept/term store as a `glossary-concept/v1` document
    (concept-view read, additive to the flat `GET /api/glossaries/{gid}` term list,
    which is UNCHANGED). A read: org + R2-3 customer scoped, no write-role gate."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = gc.list_concepts(s, gid)
    return JSONResponse({"ok": True, **doc})


@router.post("/api/glossaries/{gid}/concepts")
async def api_glossary_concepts_create(gid: int, request: Request):
    """Create a concept (optionally with its terms). Body: `domain`, `subdomain`,
    `definition`, `notes`, `external_id`, `source`, `homonym_group`, `created_by`,
    and `terms` (a list of term docs, each `term` req plus `locale`/`status`/
    `audience`/... ). can_tune_ai; reverts approval; projects the compat entries."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        fields = _concept_fields(body)
        if isinstance(body.get("terms"), list):
            fields["terms"] = body["terms"]
        try:
            concept = gc.create_concept(s, gid, **fields)
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "glossary_id": gid, "concept_id": concept.id}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_create",
        target=f"glossary {gid} concept {out['concept_id']}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/concepts/{cid}/edit")
async def api_glossary_concepts_edit(gid: int, cid: int, request: Request):
    """Edit a concept's metadata (`external_id`, `domain`, `subdomain`, `definition`,
    `notes`, `source`). can_tune_ai; reverts approval. A concept not in this glossary
    is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            concept = gc.edit_concept(s, gid, cid, **_concept_fields(body))
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "glossary_id": gid, "concept_id": concept.id}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_edit",
        target=f"glossary {gid} concept {cid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/concepts/{cid}/delete")
async def api_glossary_concepts_delete(gid: int, cid: int, request: Request):
    """Delete a concept and its terms, dropping the compat projection it backed (the
    LAW-TERM-2b delete unit). can_tune_ai; reverts approval. A concept not in this
    glossary is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            gc.delete_concept(s, gid, cid)
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "glossary_id": gid, "concept_id": cid, "deleted": True}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_delete",
        target=f"glossary {gid} concept {cid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/concepts/{cid}/terms")
async def api_glossary_concepts_add_term(gid: int, cid: int, request: Request):
    """Add a term to a concept. Body: `term` (req; the matchable surface, stored
    verbatim) plus `locale`, `status`, `audience`, `term_type`, `part_of_speech`,
    `dnt`, ... . can_tune_ai; reverts approval. A concept not in this glossary is a
    clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            term = gc.add_concept_term(s, gid, cid, **_term_fields(body))
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "glossary_id": gid, "concept_id": cid, "term_id": term.id}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_term_add",
        target=f"glossary {gid} concept {cid} term {out['term_id']}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/concepts/{cid}/terms/{tid}/edit")
async def api_glossary_concepts_edit_term(gid: int, cid: int, tid: int, request: Request):
    """Edit a concept's term (any of the term fields; `term` stays verbatim).
    can_tune_ai; reverts approval. A term not in this concept is a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            term = gc.edit_concept_term(s, gid, cid, tid, **_term_fields(body))
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "glossary_id": gid, "concept_id": cid, "term_id": term.id}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_term_edit",
        target=f"glossary {gid} concept {cid} term {tid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/concepts/{cid}/terms/{tid}/delete")
async def api_glossary_concepts_delete_term(gid: int, cid: int, tid: int, request: Request):
    """Delete a concept's term (dropping the concept too if it goes termless — the
    LAW-TERM-2b delete-term unit). can_tune_ai; reverts approval. A term not in this
    concept is a clean 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            ok = gc.delete_concept_term(s, gid, cid, tid)
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not ok:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = {"ok": True, "glossary_id": gid, "concept_id": cid, "term_id": tid, "deleted": True}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_term_delete",
        target=f"glossary {gid} concept {cid} term {tid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/concepts/terms/audience")
async def api_glossary_concepts_set_audience(gid: int, request: Request):
    """Bulk-set the audience of a set of the glossary's terms (Q2 bulk action). Body:
    `term_ids` (a list of GlossaryTerm ids) and `audience` (a list or CSV subset of
    {human,mt,llm}; canonicalized). can_tune_ai; reverts approval. Terms outside this
    glossary are silently skipped; the response reports the count updated."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import glossary_concepts as gc

    with Session(engine) as s:
        # Confinement first: a bound caller's FOREIGN glossary must be indistinguishable
        # from missing (404) BEFORE any body validation reveals the resource exists
        # (verify_rename_delete_projects sweep).
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        term_ids = body.get("term_ids")
        if not isinstance(term_ids, list):
            return JSONResponse(
                {"ok": False, "error": "term_ids (a list) is required."}, status_code=400
            )
        try:
            n = gc.set_audience(s, gid, term_ids, body.get("audience"))
        except gc.ConceptError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "glossary_id": gid, "updated": n}
    audit.log(
        actor=ident["actor"],
        action="glossary.concept_set_audience",
        target=f"glossary {gid} ({n} terms)",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/import")
async def api_glossary_module_import(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    source, target = str(body.get("source") or ""), str(body.get("target") or "")
    # `csv_b64` carries a glossary CSV, a native TBX termbase, or a concept-aware
    # `.glossary.json` (`glossary-concept/v1`, D2); `filename` is the optional
    # extension hint (see api_glossary_import above). `format` (csv|tbx|json) forces
    # the parser when there is no filename to sniff on — an explicit filename still
    # wins (extension-first, matching detect_format). `strict=true` (Decision D, T2)
    # turns the first TBX-Basic conformance finding into a refusal (no rows written); it
    # is inert for CSV/JSON imports.
    from app.kernel import glossary as gloss_engine
    from app.kernel import glossary_module as gm

    data = _b64(body.get("csv_b64"))
    filename = str(body.get("filename") or "")
    strict = bool(body.get("strict"))
    # `concepts=true` is RESERVED (Terminology T2 scope decision): the concept-preserving TBX
    # import is deferred to the terminology authority train (T1b), so this option is refused
    # (writing nothing) in the engine before any parse. The default TBX import is the flat entry
    # path. Inert for CSV / .glossary.json.
    concepts = bool(body.get("concepts"))
    fmt_req = str(body.get("format") or "").strip().lower()
    if fmt_req and not filename:
        filename = {
            "csv": "import.csv",
            gloss_engine.TBX_FORMAT: "import.tbx",
            gloss_engine.JSON_FORMAT: "import.glossary.json",
            "glossary.json": "import.glossary.json",
        }.get(fmt_req, filename)
    if data is None or not source or not target:
        return JSONResponse(
            {"ok": False, "error": "csv_b64, source and target are required."}, status_code=400
        )

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            res = await deps.offload_parse(
                gm.import_bytes,
                s,
                gid,
                data,
                source,
                target,
                filename=filename,
                strict=strict,
                concepts=concepts,
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except Exception as exc:
            return glossary_import_error_response(exc)
    # `projected_entries` (round-1 H2) rides in the response body for the concept arm so an
    # API caller sees how many entries the termbase actually enforces (0 = refused above).
    return JSONResponse({"ok": True, "glossary_id": gid, **res})


@router.post("/api/glossaries/{gid}/delete")
async def api_glossary_module_delete(gid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        gm.delete(s, gid)
    audit.log(
        actor=ident["actor"],
        action="glossary.delete",
        target=f"glossary {gid}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "glossary_id": gid})
