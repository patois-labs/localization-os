"""/api routes: glossaries 2

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import (
    audit,
)
from app.web import deps
from app.web.api_cli_common import _NOT_FOUND, _ai_block, _gate, _glossary_in_scope, _int_id, _json

router = APIRouter()


# --- term extraction + candidate review (feat/term-extraction) ---------------
# Web+CLI+API parity for the term-extraction loop. Content-edit gate (can_tune_ai),
# same as the other glossary mutations. Read (candidates list) needs no write gate,
# mirroring api_glossary_module_show. Follows the module-glossary sibling posture.
@router.post("/api/glossaries/{gid}/extract")
async def api_glossary_module_extract(gid: int, request: Request):
    """Extract candidate terms from a document into the glossary's review queue (nothing
    auto-added). Body: `document_id` (req; in the glossary's scope), `target`/`locale`,
    `source`. Spend-capped: a blocked run is a clean 402-style refusal with no
    candidates."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import term_extraction as tex

    with Session(engine) as s:
        g = _glossary_in_scope(s, gid, org_id, ident)
        if g is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        raw_doc = body.get("document_id")
        doc_int = _int_id(raw_doc)
        if doc_int is None:
            # L3: distinguish absent from present-but-unparseable (a Unicode digit,
            # a negative, or an out-of-range value) so the message isn't misleading.
            msg = (
                "document_id is invalid."
                if str(raw_doc or "").strip()
                else "document_id is required."
            )
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        doc = tex.visible_document(s, g, doc_int, org_id=org_id, user=ident.get("user"))
        if doc is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # Capture the id INSIDE the session: the audit line below runs after the
        # session closes, and reading doc.id off the detached instance raised
        # DetachedInstanceError -- a 500 on every SUCCESSFUL extraction (the
        # candidates were already committed), found by the MCP wave-2 suite as
        # the first end-to-end caller of this route.
        doc_id = doc.id
        # Spend-capped multi-call LLM extraction: off the event loop (#495).
        try:
            summary = await deps.offload_parse(
                tex.extract,
                s,
                g,
                str(body.get("target") or body.get("locale") or "").strip(),
                document=doc,
                source_locale=str(body.get("source") or "").strip(),
            )
        except tex.ExtractionEngineUnavailable as exc:
            # #761: no API key on the default engine is a setup problem, not a server fault.
            return JSONResponse(
                {"ok": False, "error": str(exc), "code": "engine_unavailable"}, status_code=400
            )
        if summary.get("blocked"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "spend cap reached - extraction refused (no candidates created).",
                },
                status_code=400,
            )
    audit.log(
        actor=ident["actor"],
        action="glossary.extract",
        target=f"glossary {gid} <- doc {doc_id}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "glossary_id": gid, **summary})


@router.get("/api/glossaries/{gid}/candidates")
def api_glossary_module_candidates(gid: int, request: Request):
    """List term candidates for a glossary (default status=pending). A read — org + R2-3
    scoped via _glossary_in_scope, no write gate (mirrors api_glossary_module_show)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    status = (request.query_params.get("status") or "pending").strip()
    from app.kernel import term_extraction as tex

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        cands = tex.list_candidates(s, gid, status=status)
        # #478: a snippet copied from a project the caller cannot see is blanked.
        hidden = tex.hidden_source_document_ids(s, cands, ident.get("user"))
        payload = [
            {
                "id": c.id,
                "source_term": c.source_term,
                "proposed_target": c.proposed_target,
                "source_locale": c.source_locale,
                "target_locale": c.target_locale,
                "frequency": c.frequency,
                "status": c.status,
                "context_snippet": "" if c.source_document_id in hidden else c.context_snippet,
                "source_document_id": (
                    None if c.source_document_id in hidden else c.source_document_id
                ),
            }
            for c in cands
        ]
    return JSONResponse({"ok": True, "glossary_id": gid, "status": status, "candidates": payload})


@router.post("/api/glossaries/{gid}/candidates/{cid}/accept")
async def api_glossary_module_candidate_accept(gid: int, cid: int, request: Request):
    """Accept a candidate into the glossary as a term. Body: optional `target_term`
    override. can_tune_ai-gated; a non-pending/foreign candidate is a clean 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    override = body.get("target_term")
    from app.kernel import term_extraction as tex

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        entry = tex.accept(
            s,
            gid,
            cid,
            decided_by=ident["actor"],
            target_term=(str(override) if override is not None else None),
            user=ident.get("user"),  # #541: a hidden project's snippet is never copied over
        )
        if entry is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = {
            "ok": True,
            "glossary_id": gid,
            "candidate_id": cid,
            "source_term": entry.source_term,
            "target_term": entry.target_term,
        }
    audit.log(
        actor=ident["actor"],
        action="glossary.candidate_accept",
        target=f"glossary {gid} candidate {cid}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/glossaries/{gid}/candidates/{cid}/drop")
async def api_glossary_module_candidate_drop(gid: int, cid: int, request: Request):
    """Drop a candidate (persists so it is never re-proposed). can_tune_ai-gated; a
    non-pending/foreign candidate is a clean 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    from app.kernel import term_extraction as tex

    with Session(engine) as s:
        if _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not tex.drop(s, gid, cid, decided_by=ident["actor"]):
            return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident["actor"],
        action="glossary.candidate_drop",
        target=f"glossary {gid} candidate {cid}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "glossary_id": gid, "candidate_id": cid})
