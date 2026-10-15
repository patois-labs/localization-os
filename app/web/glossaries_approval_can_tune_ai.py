"""glossaries: approval (can_tune_ai)

Documented in: docs/architecture/tm-and-glossary.md
"""

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
    WorkerBusy,
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

router = APIRouter()


def _manage_gate(request):
    """Approval gate (can_tune_ai: admin or linguist). Returns (me, blocked).

    Per the maintainer: glossary approval should not require manager sign-off -- linguists
    manage the full glossary lifecycle (content edits + approval) without a
    manager gate. Previously gated on can_manage (>= manager); widened to
    can_tune_ai, matching the content-edit gate (_ai_block) elsewhere in this
    module."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_tune_ai(me):
        return me, _error_page(
            request, 403, "Approving a glossary is limited to linguists and admins."
        )
    return me, None


def _customers(request, session):
    """Org-scoped customer list for the create-form scope dropdown; a customer-bound
    caller only ever sees its OWN customer (R2-3)."""
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    # SPEC-customer-archive: scope PICKER — archived customers filtered out.
    q = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    my_cust = access.current_customer_id(me)
    if my_cust is not None:
        q = q.where(Customer.id == my_cust)
    return session.exec(q).all()


def _valid_customer_id(session, request, raw):
    """A submitted customer_id: None if blank (global -- fine), the id if it names an
    ACTIVE customer the caller may scope to (fine), else RAISE (T2-02).

    Pre-fix this fell back to None (silently global) for ANY not-selectable id,
    which meant an ARCHIVED customer_id (a customer that briefly existed as a valid
    selection, then got archived) silently and invisibly stripped the caller's scope
    choice instead of refusing the request. Mirrors content.project_create's
    ArchivedCustomerError contract: a supplied-but-invalid id is a clean 400, never
    a silent scope change.

    NON-DISCLOSURE (adversarial-review fix): the archived-specific refusal is gated
    behind a SAME-ORG check (customers.selectable, which org-checks) so a foreign-org
    id -- archived, active, or nonexistent alike -- ALWAYS falls through to the same
    generic "not available in this scope" response. Without the gate, a forged
    cross-org POST naming an org-B ARCHIVED customer would leak its existence AND its
    name (via ?err=), an oracle a foreign-active/nonexistent id never gave. The
    archived message echoes the ID ONLY (never cust.name), matching
    content.project_create's ArchivedCustomerError contract."""
    raw = (raw or "").strip()
    if not raw:
        return None
    # coerce_id keeps the "not a valid id" contract for the malformed shapes too (a
    # Unicode digit int() rejects, or a value SQLite overflows at the bind) -- a clean
    # GlossaryModuleError, never a raw ValueError/OverflowError 500 at int()/session.get.
    cid = ids.coerce_id(raw)
    if cid is None:
        raise gm.GlossaryModuleError(f"customer_id {raw!r} is not a valid id.")
    ok = {c.id for c in _customers(request, session)}
    if cid in ok:
        return cid
    from app.kernel import customers as customer_service

    # selectable() returns the customer ONLY if it exists, is same-org, AND is active;
    # None otherwise. We already know cid is not selectable (not in `ok`), so we only
    # need to distinguish a SAME-ORG archived customer (a clean, id-only archived
    # refusal) from everything else (foreign-org / nonexistent / active-but-unlisted
    # -> the identical generic non-disclosing response). is_active(None) is False, so
    # an org check is the discriminator: only a same-org, existing-but-archived row
    # reaches the archived branch; a foreign-org id never does.
    me = security.session_user(request)
    cust = session.get(Customer, cid)
    if (
        cust is not None
        and access.same_org(me, getattr(cust, "org_id", None))
        and not customer_service.is_active(cust)
    ):
        raise gm.GlossaryModuleError(f"customer {cid} is archived and cannot be selected.")
    raise gm.GlossaryModuleError(f"customer {cid} is not available in this scope.")


def _visible_or_404(request, session, glossary_id):
    """Fetch a glossary in the caller's org + customer scope, or a 404 page."""
    g = gm.visible(
        session, glossary_id, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
    )
    if g is None:
        return None, _error_page(request, 404, "Glossary not found.")
    return g, None


# --- list + create -----------------------------------------------------------
def _selected_customer(request, customers, my_cust):
    """Resolve the customer-first list page's active customer:
      * a customer-BOUND caller is FORCED to its own customer (?customer= ignored);
      * else the ?customer= param when it names a visible customer;
      * else auto-select when EXACTLY ONE customer is visible;
      * else None -> the page prompts the staff caller to pick one.
    Server-side + no-JS-safe (a plain GET form), mirroring the /tm picker pattern."""
    customer_ids = {c.id for c in customers}
    if my_cust is not None:
        return my_cust
    n = ids.coerce_id(request.query_params.get("customer"))
    if n is not None and n in customer_ids:
        return n
    if len(customers) == 1:
        return customers[0].id
    return None


@router.get("/glossaries")
def glossaries_index(request: Request, session: Session = Depends(get_session)):
    me = security.session_user(request)
    org = _my_org_id(request)
    my_cust = _my_customer_id(request)
    can_manage = (not security.auth_required()) or security.can_manage(me)
    customers = _customers(request, session)
    selected = _selected_customer(request, customers, my_cust)
    rows = []
    if selected is not None:
        rows = [
            {"g": g, "summary": gm.to_dict(session, g)}
            for g in gm.list_for_customer(session, org, selected)
        ]
    # Legacy unassigned (customer_id NULL) — the "needs a customer" section, gated to
    # can_manage (a customer-bound caller never sees it).
    unassigned = []
    if can_manage and my_cust is None:
        unassigned = [
            {"g": g, "summary": gm.to_dict(session, g)} for g in gm.list_unassigned(session, org)
        ]
    sel_cust = session.get(Customer, selected) if selected is not None else None
    return templates.TemplateResponse(
        request,
        "glossaries.html",
        {
            "request": request,
            "rows": rows,
            "unassigned": unassigned,
            "customers": customers,
            "selected_customer_id": selected,
            "selected_customer": sel_cust,
            "customer_bound": my_cust is not None,
            "can_edit": (not security.auth_required()) or security.can_tune_ai(me),
            "can_approve": (not security.auth_required()) or security.can_tune_ai(me),
            "can_manage": can_manage,
            "content_types": ctsvc.list_types(session, org_id=org, customer_id=my_cust),
            "locales": languages_catalog.enabled_locales(),
            "label_for": languages_catalog.label_for,
        },
    )


# --- detail ------------------------------------------------------------------
@router.get("/glossaries/{glossary_id}")
def glossary_detail(glossary_id: int, request: Request, session: Session = Depends(get_session)):
    g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    can_manage = (not security.auth_required()) or security.can_manage(me)
    # Assign-banner data: only when the glossary is LEGACY unassigned AND the caller may
    # assign it (can_manage). The select offers the caller's org-scoped active customers.
    assign_customers = (
        _customers(request, session) if (g.customer_id is None and can_manage) else []
    )
    cust = session.get(Customer, g.customer_id) if g.customer_id else None
    try:
        gloss_engine.parse_cursor(request.query_params.get("glossary_cursor"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    entry_page = gm.page_entries(
        session, glossary_id, cursor=request.query_params.get("glossary_cursor"), limit=200
    )
    return templates.TemplateResponse(
        request,
        "glossary_detail.html",
        {
            "request": request,
            "g": g,
            "customer": cust,
            "entries": entry_page["entries"],
            "glossary_total": gm.entry_count(session, glossary_id),
            "glossary_next_cursor": entry_page["next_cursor"],
            # SPEC-terminology-backend §7.2: the concept/term store for the concept editor
            # (the editor view carries DB ids the edit/delete forms POST).
            "concepts": gc.concepts_for_editor(session, glossary_id),
            # LT-2: the concept EDITOR write controls are gated on the terminology_concept_editor
            # feature (default OFF this release); when off the concept card renders read-only.
            "concept_editor_enabled": gc.concept_editor_enabled(),
            "pairs": gm.pairs_for(session, glossary_id),
            "pairs_seen": _pairs_signature(gm.pairs_for(session, glossary_id)),
            # Term-extraction review grid (feat/term-extraction): the pending candidate
            # queue + the visible-scope document picker for launching an extraction.
            # #478: snippets copied from a project the viewer cannot see are blanked.
            "candidates": tex.redact_hidden_snippets(
                session, tex.list_candidates(session, glossary_id), security.session_user(request)
            ),
            "candidate_counts": tex.counts(session, glossary_id),
            "extract_documents": tex.documents_for_glossary(
                session, g, org_id=_my_org_id(request), user=security.session_user(request)
            ),
            # The content types this glossary may attach: GLOBAL + its own customer's
            # (the attachable set — content.options_for_project's glossary analogue), so
            # the dropdown can never offer a foreign type.
            "content_types": ctsvc.options_for_project(
                session, org_id=_my_org_id(request), customer_id=g.customer_id
            ),
            "can_edit": (not security.auth_required()) or security.can_tune_ai(me),
            "can_approve": (not security.auth_required()) or security.can_tune_ai(me),
            "can_manage": can_manage,
            "assign_customers": assign_customers,
            "locales": languages_catalog.enabled_locales(),
            "label_for": languages_catalog.label_for,
        },
    )


@router.get("/glossaries/{glossary_id}/export")
def glossary_export(
    glossary_id: int,
    request: Request,
    format: str = "csv",
    dialect: str = "tbx-basic",
    strict_portable: bool = False,
    session: Session = Depends(get_session),
):
    """Download a module glossary as CSV, or as a native TBX v3 termbase with
    `?format=tbx` -- round-trips /glossaries/{id}/import, mirroring the legacy
    project glossary's /projects/{id}/glossary/export. A read (same visibility gate
    as the detail page, no can_edit/can_manage required). CSV remains the LOSSLESS
    format (TBX carries no senses; DNT round-trips via a private termNote when a
    term has a target). Web+CLI parity with
    `glossary module export <id> <out.csv|.tbx>`; /api GET
    /api/glossaries/{id}/export?format=tbx. An unknown format falls back to CSV."""
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    req = (format or "").strip().lower()
    if req == gloss_engine.TBX_FORMAT:
        fmt = gloss_engine.TBX_FORMAT
    elif req in (gloss_engine.JSON_FORMAT, "glossary.json", ".glossary.json"):
        # SPEC-terminology-backend D2: the .glossary.json no-caveat lossless export.
        fmt = gloss_engine.JSON_FORMAT
    else:
        fmt = gloss_engine.CSV_FORMAT
    # TBX dialect controls: default TBX-Basic v3 (DCA) with the los: private ns;
    # ?dialect=tbx-core or ?strict_portable=1 drop los: (§6.4/§6.5).
    dia = (dialect or "tbx-basic").strip().lower()
    if dia not in ("tbx-basic", "tbx-core"):
        dia = "tbx-basic"
    drops: list[str] = []
    try:
        data = gm.export_bytes(
            session,
            glossary_id,
            fmt=fmt,
            dialect=dia,
            strict_portable=bool(strict_portable),
            drops_out=drops,
        )
    except glossary_tbx.EmptyGlossaryError:
        # A concept-format export of an empty glossary refuses (§6.4/M8); CSV never reaches
        # here (it returns a valid header-only file). Surface a "nothing to export" flash.
        return redirect_with_error(f"/glossaries/{glossary_id}", "empty_export")
    ext = {
        gloss_engine.TBX_FORMAT: ".tbx",
        gloss_engine.JSON_FORMAT: ".glossary.json",
        gloss_engine.CSV_FORMAT: ".csv",
    }[fmt]
    media = {
        gloss_engine.TBX_FORMAT: glossary_tbx.TBX_MEDIA_TYPE,
        gloss_engine.JSON_FORMAT: "application/json",
        gloss_engine.CSV_FORMAT: "text/csv",
    }[fmt]
    headers = {"Content-Disposition": f'attachment; filename="glossary_{glossary_id}{ext}"'}
    if drops:
        # M1: a portable / tbx-core TBX download names the los: field types it dropped, so a
        # web caller (and the browser network pane) can see what a stock-TBX-Basic file loses.
        headers["X-Loc-Dropped-Fields"] = ", ".join(drops)
    return Response(content=data, media_type=media, headers=headers)


@router.post("/glossaries/{glossary_id}/content-type")
def glossary_set_content_type(
    glossary_id: int,
    request: Request,
    content_type_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Set or clear a glossary's content type (SPEC-content-types §3). Blank = clear
    to General. A bad/foreign id -> clean redirect with an error (never a 500).
    Does NOT revert an approved glossary to draft (scoping metadata, not content)."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gm.set_content_type(session, glossary_id, content_type_id)
    except gm.GlossaryModuleError as exc:
        return redirect_with_error(f"/glossaries/{glossary_id}", str(exc))
    audit.log(
        actor=_actor_email(request),
        action="glossary.content_type_set",
        target=f"glossary {glossary_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


@router.post("/glossaries/{glossary_id}/rename")
def glossary_rename(
    glossary_id: int,
    request: Request,
    name: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gm.rename(session, glossary_id, name)
    except gm.GlossaryModuleError as exc:
        return redirect_with_error(f"/glossaries/{glossary_id}", str(exc))
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


@router.post("/glossaries/{glossary_id}/delete")
def glossary_delete(glossary_id: int, request: Request, session: Session = Depends(get_session)):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    gm.delete(session, glossary_id)
    audit.log(
        actor=_actor_email(request),
        action="glossary.delete",
        target=f"glossary {glossary_id}",
        ip=_ip(request),
    )
    return RedirectResponse("/glossaries", status_code=303)


# --- approval (can_tune_ai) ---------------------------------------------------
@router.post("/glossaries/{glossary_id}/approve")
def glossary_approve(glossary_id: int, request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manage_gate(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    gm.approve(session, glossary_id, approver_email=_actor_email(request))
    audit.log(
        actor=_actor_email(request),
        action="glossary.approve",
        target=f"glossary {glossary_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


@router.post("/glossaries/{glossary_id}/unapprove")
def glossary_unapprove(glossary_id: int, request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manage_gate(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    gm.unapprove(session, glossary_id)
    audit.log(
        actor=_actor_email(request),
        action="glossary.unapprove",
        target=f"glossary {glossary_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


# --- language-pair association ----------------------------------------------
def _pairs_signature(pairs) -> str:
    """Order-free fingerprint of a glossary's pair list (the add/remove forms carry it back)."""

    def _loc(value) -> str:
        return (value or "").strip().lower()

    return "|".join(sorted(f"{_loc(p['source_locale'])}>{_loc(p['target_locale'])}" for p in pairs))


@router.post("/glossaries/{glossary_id}/pairs")
def glossary_set_pairs(
    glossary_id: int,
    request: Request,
    source_locale: list[str] = Form(default=[]),
    target_locale: list[str] = Form(default=[]),
    pairs_seen: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    pairs = [
        (s, t)
        for s, t in zip(source_locale, target_locale, strict=False)
        if (s or "").strip() and (t or "").strip()
    ]
    # Review: the add/remove forms resubmit the whole list they were rendered with. When the
    # list changed since (another tab, another user), refuse instead of silently undoing that
    # change. API and CLI callers send no pairs_seen and keep the plain replace-the-list rule.
    if pairs_seen is not None and pairs_seen != _pairs_signature(
        gm.pairs_for(session, glossary_id)
    ):
        return _redir(
            glossary_id,
            "The language pairs changed since this page was loaded, so nothing was saved. "
            "Check the list below and try again.",
        )
    try:
        result = gm.set_pairs(session, glossary_id, pairs)
    except gm.GlossaryModuleError as exc:
        return _redir(glossary_id, exc)
    # A.4-8 (M-2): surface the widening counts through the ?msg= success channel.
    created = getattr(result, "bindings_created", 0)
    dnt_ext = getattr(result, "dnt_bindings_extended", 0)
    if created or dnt_ext:
        return _redir(
            glossary_id,
            msg=f"pairs saved; {created} binding(s) created, {dnt_ext} DNT binding(s) extended",
        )
    return _redir(glossary_id, msg="pairs saved")


# --- term editing ------------------------------------------------------------
def _dnt_bool(raw) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _meta_form(part_of_speech, definition, usage_example, subject_field, notes) -> dict:
    """Assemble a term-metadata dict from the add/edit form fields (the engine
    sanitizes + allowlists; blanks are dropped there)."""
    return {
        "part_of_speech": part_of_speech,
        "definition": definition,
        "usage_example": usage_example,
        "subject_field": subject_field,
        "notes": notes,
    }


@router.post("/glossaries/{glossary_id}/terms/add")
def glossary_add_term(
    glossary_id: int,
    request: Request,
    source_term: str = Form(...),
    target_term: str = Form(""),
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    dnt: str = Form(""),
    part_of_speech: str = Form(""),
    definition: str = Form(""),
    usage_example: str = Form(""),
    subject_field: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        entry = gm.add_term(
            session,
            glossary_id,
            source_term,
            target_term=target_term,
            source_locale=(source_locale or "").strip(),
            target_locale=(target_locale or "").strip(),
            dnt=_dnt_bool(dnt),
            meta=_meta_form(part_of_speech, definition, usage_example, subject_field, notes),
        )
    except gm.GlossaryModuleError as exc:
        # T2-03: an unknown/garbage locale code (forged POST -- the dropdown only
        # ever offers catalog locales) is a clean redirect, never a 500.
        return redirect_with_error(f"/glossaries/{glossary_id}", str(exc))
    if entry is None:
        return redirect_with_error(f"/glossaries/{glossary_id}", "blank_term")
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


@router.post("/glossaries/{glossary_id}/terms/{entry_id}/edit")
def glossary_edit_term(
    glossary_id: int,
    entry_id: int,
    request: Request,
    target_term: str = Form(""),
    dnt: str = Form(""),
    case_sensitive: str = Form(""),
    cs_present: str = Form(""),
    part_of_speech: str = Form(""),
    definition: str = Form(""),
    usage_example: str = Form(""),
    subject_field: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    """Edit a term's target/DNT/case-sensitivity + TBX-ish metadata (the expandable per-term row
    editor). Source term + locales are the upsert key and are not editable here."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    # case_sensitive is a per-pair fact (A.3, M-2); a hidden `cs_present=1` companion makes the
    # unchecked box mean False rather than "unchanged".
    cs = _dnt_bool(case_sensitive) if cs_present else None
    updated = gm.edit_term(
        session,
        glossary_id,
        entry_id,
        target_term=target_term,
        dnt=_dnt_bool(dnt),
        case_sensitive=cs,
        meta=_meta_form(part_of_speech, definition, usage_example, subject_field, notes),
    )
    if updated is None:
        return redirect_with_error(f"/glossaries/{glossary_id}", "unknown_term")
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


@router.post("/glossaries/{glossary_id}/terms/{entry_id}/delete")
def glossary_remove_term(
    glossary_id: int, entry_id: int, request: Request, session: Session = Depends(get_session)
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    gm.remove_term(session, glossary_id, entry_id)
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


@router.post("/glossaries/{glossary_id}/terms/{entry_id}/sense")
def glossary_add_sense(
    glossary_id: int,
    entry_id: int,
    request: Request,
    sense_label: str = Form(...),
    target_term: str = Form(""),
    context_cue: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gm.add_sense(session, glossary_id, entry_id, sense_label, target_term, context_cue)
    except gm.GlossaryModuleError as exc:
        return redirect_with_error(f"/glossaries/{glossary_id}", str(exc))
    return RedirectResponse(f"/glossaries/{glossary_id}", status_code=303)


# --- concept editor (SPEC-terminology-backend §7.2) --------------------------
# The concept/term store editor. Nested under /concepts/ so the term routes never
# collide with the compat-entry grid routes (/terms/{eid}/*, H5). Every route reuses
# the same visibility + AI gates as the term grid and delegates to the shared
# glossary_concepts service (web+CLI+/api parity). A term delete runs the LAW-TERM-2b
# delete graph in the service.
def _redir(glossary_id, err=None, msg=None):
    """Redirect back to the glossary. ``err`` renders the error banner; ``msg`` renders a
    SUCCESS banner (A.3 r4-21 -- the counts channel for set_pairs widening / term propagation)."""
    from urllib.parse import quote_plus

    if err:
        return redirect_with_error(f"/glossaries/{glossary_id}", str(err))
    q = f"?msg={quote_plus(str(msg))}" if msg else ""
    return RedirectResponse(f"/glossaries/{glossary_id}{q}", status_code=303)


@router.post("/glossaries/{glossary_id}/concepts")
def glossary_add_concept(
    glossary_id: int,
    request: Request,
    definition: str = Form(""),
    domain: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gc.create_concept(session, glossary_id, definition=definition, domain=domain, notes=notes)
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


@router.post("/glossaries/{glossary_id}/concepts/{concept_id}/edit")
def glossary_edit_concept(
    glossary_id: int,
    concept_id: int,
    request: Request,
    definition: str = Form(""),
    domain: str = Form(""),
    notes: str = Form(""),
    homonym_group: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        # R4-6: `homonym_group` is exposed here so the web operator can group two concepts
        # that share a source surface (the remedy every collision refusal prescribes).
        gc.edit_concept(
            session,
            glossary_id,
            concept_id,
            definition=definition,
            domain=domain,
            notes=notes,
            homonym_group=homonym_group,
        )
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


@router.post("/glossaries/{glossary_id}/concepts/{concept_id}/delete")
def glossary_delete_concept(
    glossary_id: int, concept_id: int, request: Request, session: Session = Depends(get_session)
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gc.delete_concept(session, glossary_id, concept_id)
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


@router.post("/glossaries/{glossary_id}/concepts/{concept_id}/terms/add")
def glossary_add_concept_term(
    glossary_id: int,
    concept_id: int,
    request: Request,
    locale: str = Form(""),
    term: str = Form(""),
    status: str = Form("preferred"),
    term_type: str = Form(""),
    audience: str = Form("human,mt,llm"),
    part_of_speech: str = Form(""),
    dnt: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    # Confinement first: `term` is validated in the handler (Form("") not Form(...)) so a
    # bound caller's FOREIGN glossary 404s BEFORE any body-validation error reveals it
    # exists (verify_rename_delete_projects sweep).
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    if not term.strip():
        return _redir(glossary_id, "Enter the term text, then add the term again.")
    try:
        gc.add_concept_term(
            session,
            glossary_id,
            concept_id,
            locale=locale,
            term=term,
            status=status,
            term_type=term_type,
            audience=audience,
            part_of_speech=part_of_speech,
            dnt=_dnt_bool(dnt),
        )
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


@router.post("/glossaries/{glossary_id}/concepts/{concept_id}/terms/{term_id}/edit")
def glossary_edit_concept_term(
    glossary_id: int,
    concept_id: int,
    term_id: int,
    request: Request,
    term: str = Form(""),
    status: str = Form(""),
    term_type: str = Form(""),
    audience: str = Form(""),
    dnt: str = Form(""),
    dnt_present: str = Form(""),
    case_sensitive: str = Form(""),
    cs_present: str = Form(""),
    locale: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    fields = {"term_type": term_type}
    if term:
        fields["term"] = term
    if status:
        fields["status"] = status
    if audience:
        fields["audience"] = audience
    if locale:
        fields["locale"] = locale
    # R7-I (M-3): DNT is a TRISTATE. A hidden `dnt_present=1` companion makes an UNCHECKED box
    # (which posts no `dnt`) mean "set to False" -- without it DNT could only ever be turned ON.
    # A bare truthy `dnt` still turns it ON (backward compatible); both absent = leave unchanged.
    if dnt_present or dnt:
        fields["dnt"] = _dnt_bool(dnt)
    if cs_present:
        fields["case_sensitive"] = _dnt_bool(case_sensitive)
    try:
        gc.edit_concept_term(session, glossary_id, concept_id, term_id, **fields)
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


@router.post("/glossaries/{glossary_id}/concepts/{concept_id}/terms/{term_id}/delete")
def glossary_delete_concept_term(
    glossary_id: int,
    concept_id: int,
    term_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gc.delete_concept_term(session, glossary_id, concept_id, term_id)
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


@router.post("/glossaries/{glossary_id}/concepts/terms/audience")
def glossary_set_audience(
    glossary_id: int,
    request: Request,
    term_id: list[int] = Form(default=[]),
    audience: str = Form("human,mt,llm"),
    session: Session = Depends(get_session),
):
    """Q2 bulk 'mark audience' action over a set of the glossary's terms."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    try:
        gc.set_audience(session, glossary_id, term_id, audience)
    except gc.ConceptError as exc:
        return _redir(glossary_id, exc)
    return _redir(glossary_id)


# --- CSV import + LLM builder -----------------------------------------------
@router.post("/glossaries/{glossary_id}/import")
async def glossary_import(
    glossary_id: int,
    request: Request,
    source_locale: str = Form(...),
    target_locale: str = Form(...),
    file: UploadFile = File(...),
    strict: bool = Form(False),
    concepts: bool = Form(False),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    data = await read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return redirect_with_error(f"/glossaries/{glossary_id}", "toolarge")
    # CSV or native TBX — format from the uploaded FILENAME (.tbx), content sniff
    # as the fallback (glossary.detect_format).
    # Capture approval state BEFORE the import so the flash can say, explicitly,
    # whether THIS import is the one that reverted it -- gm.import_bytes commits
    # inside touch_draft(), which expires `g` (default SQLAlchemy expire_on_commit),
    # so re-reading g.status below reflects the post-import DB state for free.
    was_approved = g.status == gm.GlossaryStatus.APPROVED
    try:
        res = await offload_parse(
            gm.import_bytes,
            session,
            glossary_id,
            data,
            source_locale,
            target_locale,
            filename=(file.filename or ""),
            strict=strict,
            concepts=concepts,
        )
    except (
        gc.ConceptError,
        glossary_tbx.TbxError,
        gloss_engine.GlossaryCsvHeaderError,
        gloss_engine.GlossaryImportIncomplete,
    ) as exc:
        # R2-6 + T2: surface the concept-importer / TBX validator's user-facing message
        # (a .glossary.json or TBX-Basic refusal, incl. a strict-conformance failure), not the
        # generic "import failed", so the web user sees the same guidance as /api + CLI.
        return redirect_with_error(f"/glossaries/{glossary_id}", str(exc))
    except WorkerBusy:
        raise  # the app-wide handler answers a retryable 503 (nothing was imported)
    except Exception:
        return redirect_with_error(f"/glossaries/{glossary_id}", "import")
    reverted = was_approved and g.status != gm.GlossaryStatus.APPROVED
    # .get() so a .glossary.json import (counters concepts/terms/dropped_refs/
    # truncated_fields, no entries/senses key) does not KeyError.
    qs = (
        f"gl_entries={res.get('entries', res.get('concepts', 0))}"
        f"&gl_senses={res.get('senses', res.get('terms', 0))}"
        f"&gl_skipped={res.get('skipped', 0)}&gl_changed={res.get('changed', 0)}"
    )
    if res.get("concurrent_deleted"):
        qs += f"&gl_deleted={int(res['concurrent_deleted'])}"  # #615: deleted mid-import
    if res.get("id_conflicts"):
        qs += f"&gl_idconf={res['id_conflicts']}"  # R4-9: foreign id collisions imported as new
    if "projected_entries" in res:
        # H2: the .glossary.json concept import reports how many entries THIS import's terms
        # enforce for the pair(s) -- a re-import reports the subset its own terms cover, not a
        # whole-glossary total (round-3 M1 / round-4 M2).
        qs += f"&gl_projected={res['projected_entries']}"
    if res.get("bindings_skipped"):
        # L-1: a bindings-bearing concept restored into a WIDER glossary is not auto-widened into
        # a configured pair its bindings never named (M-7/LT3-1); surface the count.
        qs += f"&gl_bindings_skipped={res['bindings_skipped']}"
    if reverted:
        qs += "&gl_reverted=1"
    qs += glossary_tbx.report_query(res)
    return RedirectResponse(f"/glossaries/{glossary_id}?{qs}", status_code=303)


@router.post("/glossaries/{glossary_id}/build")
async def glossary_build(
    glossary_id: int,
    request: Request,
    text: str = Form(""),
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    session: Session = Depends(get_session),
):
    """LLM builder (paste text): propose candidate terms and land them PENDING in the
    review grid — NO silent auto-add (the fixed wart). Routes through the SAME
    two-stage candidate pipeline as the document flow; a reviewer then accepts/drops.
    Offline mock proposes deterministically; a real key does the extraction."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    g, blocked = _visible_or_404(request, session, glossary_id)
    if blocked:
        return blocked
    # LLM extraction: off the event loop (#495).
    summary = await offload_parse(
        tex.extract,
        session,
        g,
        (target_locale or "").strip(),
        text=text or "",
        source_locale=(source_locale or "").strip(),
    )
    if summary.get("blocked"):
        return redirect_with_error(f"/glossaries/{glossary_id}", "spend_cap")
    audit.log(
        actor=_actor_email(request),
        action="glossary.extract",
        target=f"glossary {glossary_id} (paste)",
        ip=_ip(request),
    )
    return RedirectResponse(
        f"/glossaries/{glossary_id}?gl_proposed={summary['proposed']}"
        f"{_degraded_qs(summary)}#candidates",
        status_code=303,
    )


# --- term extraction + candidate review (feat/term-extraction) ---------------
def _degraded_qs(summary) -> str:
    """A `&gl_degraded=<steps>` query fragment when a run degraded mid-pipeline (the
    spend cap tripped AFTER stage-1 recall, so prune and/or translation were skipped).
    Empty when the run was clean, so the detail page only shows the honesty notice when
    it actually applies."""
    steps = summary.get("degraded_steps") or []
    # Comma-joined (a comma survives query-string decoding intact, unlike '+', which
    # decodes to a space); the template splits it back on comma.
    return f"&gl_degraded={','.join(steps)}" if steps else ""
