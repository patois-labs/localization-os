"""MT formality: the web form and the /api routes (#617 item 7, ADR 0062).

One service (app/services/formality.py) behind every door, so validation and resolution cannot
drift between them:

  * web `router`: POST /formality (the small forms on the customer, project and document pages;
    a session user, CSRF-protected like every web POST);
  * `api_router`: GET /api/formality/registers (the catalog), GET /api/formality (a scope's
    entries plus the register in force per target language) and POST /api/formality (set or
    clear one entry). Token + docs scope; no session is read here.

Isolation order: the org / customer / project ring first (a foreign id answers like a missing one),
then the role gate (`formality.can_set`: a customer default needs admin or manager, a project or
document entry the customer-configuration role of admin, manager or linguist), then input
validation. An unknown register for that language is a 400.

Documented in: docs/architecture/style-guides.md
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import engine, get_session
from app.kernel import formality, languages_catalog, security
from app.models import Customer, Project
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _customer_in_org,
    _gate,
    _int_id,
    _json,
    _project_in_org,
    _role_block,
    _visible_document,
)

router = APIRouter()
api_router = APIRouter()


# --------------------------------------------------------------------------------------------
# /api
# --------------------------------------------------------------------------------------------
@api_router.get("/api/formality/registers")
def api_formality_registers(request: Request, target_locale: str = ""):
    """The register catalog. With `target_locale`: the values valid for that language (the generic
    scale first, then its language-specific registers). Without: the whole catalog."""
    _ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    loc = (target_locale or "").strip()
    if loc:
        if not languages_catalog.is_valid(loc):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "unknown locale code: " + loc + " (not in the language catalog).",
                },
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": True,
                "target_locale": languages_catalog.canonical(loc),
                "registers": [
                    {"value": r["value"], "label": r["label"], "kind": r["kind"]}
                    for r in formality.registers_for(loc)
                ],
            }
        )
    return JSONResponse(
        {"ok": True, "generic": list(formality.GENERIC), "languages": formality.catalog()}
    )


def _load_scope(s: Session, scope: str, raw_id, org_id, ident):
    """(obj, project, customer) for a scope id inside the caller's ring, else None (the 404)."""
    sid = _int_id(raw_id)
    if sid is None:
        return None
    if scope == "customer":
        cust = _customer_in_org(s, sid, org_id, ident)
        return (cust, None, cust) if cust is not None else None
    if scope == "project":
        proj = _project_in_org(s, sid, org_id, ident)
        if proj is None:
            return None
        cust = s.get(Customer, proj.customer_id) if proj.customer_id is not None else None
        return proj, proj, cust
    doc = _visible_document(s, sid)
    if doc is None:
        return None
    proj = _project_in_org(s, doc.project_id, org_id, ident)
    if proj is None:
        return None
    cust = s.get(Customer, proj.customer_id) if proj.customer_id is not None else None
    return doc, proj, cust


def _scope_error(scope):
    if scope in formality.SCOPES:
        return None
    return JSONResponse(
        {"ok": False, "error": "scope must be one of: " + ", ".join(formality.SCOPES) + "."},
        status_code=400,
    )


@api_router.get("/api/formality")
def api_formality_get(request: Request, scope: str = "", id: str = ""):
    """A scope's formality entries and, per target language, the register in force.

    Query: `scope` (customer | project | document) and `id`. Each row: `target_locale`, `current`
    (this level's own entry or ""), `resolved` (the register that applies after the whole chain:
    document, project, customer), `inherited` (what applies if this level were cleared) and the
    `options` valid for that language."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    bad = _scope_error(scope)
    if bad:
        return bad
    with Session(engine) as s:
        loaded = _load_scope(s, scope, id, org_id, ident)
        if loaded is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        obj, proj, cust = loaded
        data = formality.view(s, scope, obj, project=proj, customer=cust)
    return JSONResponse({"ok": True, **data})


@api_router.post("/api/formality")
async def api_formality_set(request: Request):
    """Set or clear one entry: `{scope, id, target_locale, register}`.

    `register` is formal | informal | auto or a language-specific register (see
    /api/formality/registers); empty / null / "inherit" clears the entry so the next level
    applies. An unknown register for that language is a 400. Customer scope: admin or manager;
    project and document scope: admin, manager or linguist. Audited as formality.set /
    formality.clear."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    scope = str(body.get("scope") or "").strip().lower()
    bad = _scope_error(scope)
    if bad:
        return bad
    with Session(engine) as s:
        loaded = _load_scope(s, scope, body.get("id"), org_id, ident)
        if loaded is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # 10-08 (ADR 0062): a customer default needs admin or manager; a project or
        # document entry the customer-configuration role. After the ring, before validation.
        blocked = _role_block(ident, lambda u: formality.can_set(u, scope))
        if blocked:
            return blocked
        obj, proj, cust = loaded
        raw = body.get("register")
        if raw is not None and not isinstance(raw, str):
            return JSONResponse(
                {"ok": False, "error": "register must be a string (or null to clear)."},
                status_code=400,
            )
        try:
            res = formality.write(
                s,
                scope,
                obj,
                body.get("target_locale"),
                raw,
                project=proj,
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        except formality.FormalityError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        data = formality.view(s, scope, obj, project=proj, customer=cust)
    return JSONResponse({"ok": True, **res, "rows": data["rows"]})


# --------------------------------------------------------------------------------------------
# web form
# --------------------------------------------------------------------------------------------
def _back(scope: str, obj_id: int) -> str:
    if scope == "customer":
        return f"/customers/{obj_id}"
    if scope == "project":
        return f"/projects/{obj_id}"
    return f"/documents/{obj_id}"


@router.post("/formality")
def formality_set(
    request: Request,
    scope: str = Form(""),
    id: str = Form(""),
    target_locale: str = Form(""),
    register_value: str = Form("", alias="register"),
    session: Session = Depends(get_session),
):
    """The page forms' POST: set or clear one entry, then back to the page's formality panel.
    Role per `formality.can_set` (customer: admin or manager; project, document: admin, manager or
    linguist); org / customer / project ring first (404), like the sibling settings routes."""
    scope = (scope or "").strip().lower()
    sid = _int_id(id)
    if scope not in formality.SCOPES or sid is None:
        return deps._error_page(request, 404, "Not found.")
    obj = proj = None
    if scope == "customer":
        obj = session.get(Customer, sid)
        if obj is None:
            return deps._missing_customer_or(request, deps._error_page(request, 404, "Not found."))
        blocked = deps._org_block(request, getattr(obj, "org_id", None)) or deps._customer_block(
            request, sid
        )
        if blocked:
            return blocked
    else:
        if scope == "project":
            obj = proj = session.get(Project, sid)
        else:
            obj = deps._visible_doc(session, sid)
            proj = session.get(Project, obj.project_id) if obj is not None else None
        if obj is None or proj is None:
            return deps._missing_block(request, session)
        blocked = deps._visibility_block(request, session, proj)
        if blocked:
            return blocked
    me = security.session_user(request)
    if security.auth_required() and not formality.can_set(me, scope):
        msg = (
            "Only an admin or manager can change a customer's formality default."
            if scope == "customer"
            else "Only an admin, manager or linguist can change the formality."
        )
        return deps._error_page(request, 403, msg)
    back = _back(scope, sid)
    try:
        formality.write(
            session,
            scope,
            obj,
            target_locale,
            register_value,
            project=proj,
            actor=deps._actor_email(request),
            ip=deps._ip(request),
        )
    except formality.FormalityError as exc:
        return RedirectResponse(f"{back}?err=" + quote(str(exc)) + "#formality", status_code=303)
    return RedirectResponse(f"{back}?saved=1#formality", status_code=303)
