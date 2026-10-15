"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    access,
    apikeys,
    audit,
    content,
    lifecycle,
    lqa_safety,
    onboarding,
    output_guard,
    quality,
    refusal,
    security,
    segment_review,
)
from app.kernel import ids as ids
from app.models import (
    Project,
    Segment,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _REVIEW_FORBIDDEN,
    _admin_block,
    _ai_block,
    _b64,
    _content_type_in_scope,
    _creator,
    _cust_of,
    _customer_in_org,
    _gate,
    _int_id,
    _json,
    _lqa_finding_api_error,
    _lqa_finding_dict,
    _lqa_finding_review_block,
    _project_in_org,
    _role_block,
    _visible_document,
    _write_block,
)

router = APIRouter()


def _review_block(ident):
    """can_review parity: >= reviewer (linguist shares reviewer's RANK). Blocks viewer
    AND translator. Gates the EXPLICIT TM unlock, mirroring the web
    documents.segment_unlock reviewer+ gate EXACTLY (same predicate, same refusal body)
    so a per-user key can no longer clear a hard TM lock from a translator/viewer role."""
    return _role_block(ident, security.can_review, body=_REVIEW_FORBIDDEN)


# ============================================================================
# Account (self-service). The lsu_ token IS the user, so these are inherently
# self-scoped — no org/customer ring applies (a user always sees/edits their OWN
# account). Mirrors the web /account self-service (available
# to any authenticated role), and is the remote arm of the `account` CLI group.
# ============================================================================
def _onboarding_state(u) -> dict:
    """The read-only onboarding-flag view shared by GET /api/account and the
    reset response (SPEC-onboarding-tour Phase 3 — parity-of-state; the web
    checklist UX itself stays web-only, but the flag is readable/resettable
    here). `variant`/`eligible` come straight from services.onboarding, the
    single source of truth the dashboard route also reads, so this can't drift."""
    return {
        "dismissed_at": u.onboarding_dismissed_at.isoformat()
        if u.onboarding_dismissed_at
        else None,
        "eligible": onboarding.is_eligible(u),
        "variant": onboarding.variant(u),
    }


def _org_scope_fields(u) -> dict:
    """Additive, multi-org only (#557): lets a relying app (Vendor Studio) scope the caller's
    role to one org. Single-tenant installs keep the exact pre-existing key set (pinned by
    verify_onboarding)."""
    if not access.multi_org_active():
        return {}
    return {"multi_org": True, "org_id": access.current_org_id(u)}


@router.get("/api/account")
def api_account_get(request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    u = _creator(ident)  # ident["user"] — the authenticated caller
    payload = {
        "ok": True,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "ui_locale": u.ui_locale or "",
        # ADR 0063: light | dark | system (Match my computer); set with POST /api/account/theme.
        "ui_theme": security.normalize_theme(getattr(u, "ui_theme", "") or ""),
        "onboarding": _onboarding_state(u),
        # #751: the CALLING key's own grants, sorted ("api" is always implied), so an
        # agent (MCP whoami, tools/list filtering) can tell what it may do. Own key only:
        # no id, prefix or name, and never another key (GET /api/apikeys stays exempt).
        "scopes": sorted(apikeys.scope_set(apikeys.normalize_scopes(ident.get("scopes", "")))),
    }
    payload.update(_org_scope_fields(u))
    return JSONResponse(payload)


@router.post("/api/account/onboarding-reset")
def api_account_reset_onboarding(request: Request):
    """SPEC-onboarding-tour Phase 3: self-service escape hatch — clear the
    caller's OWN onboarding_dismissed_at so the checklist is tour-eligible
    again (NULL -> eligible; the web `?tour=1` re-summon stays the transient,
    non-clearing alternative — see onboarding.dashboard_panel). Same
    self-service posture as GET /api/account: the lsu_ token IS the
    account, there is no `--user`-style target here at all."""
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    u = _creator(ident)
    onboarding.reset(u.id)
    audit.log(actor=ident["actor"], action="account.reset_onboarding", detail="")
    u.onboarding_dismissed_at = None  # reflect the write in this response's snapshot
    return JSONResponse({"ok": True, "onboarding": _onboarding_state(u)})


_SEGMENT_SURFACED = frozenset(
    {
        "qe",
        # QE auto-fix (Train B): the rounds behind a QE verdict ("auto-fixed in N rounds" /
        # "auto-fix tried N rounds"), narrowed to {type, severity, message} like the rest.
        "qe_autofix",
        quality.DNT_FLAG_TYPE,
        quality.GLOSSARY_FLAG_TYPE,
        quality.TAG_FLAG_TYPE,
        quality.SOURCE_FALLBACK_FLAG_TYPE,
        lqa_safety.FIX_REVERTED_FLAG_TYPE,
        lqa_safety.FIX_REGRESSION_FLAG_TYPE,
        quality.NUMBER_FLAG_TYPE,
        quality.PLACEHOLDER_FLAG_TYPE,
        quality.WHITESPACE_FLAG_TYPE,
        # Decisions 11 / 14 / 16: ambiguous date, line breaks differ, glued words.
        quality.REVIEW_HOLD_FLAG_TYPE,
        # #664: the minor note that an echoed prompt label ("Source:") was stripped.
        quality.LABEL_ECHO_FLAG_TYPE,
        quality.STEP_ERROR_FLAG_TYPE,
        refusal.FLAG_TYPE,
        refusal.NONTRANSLATION_FLAG_TYPE,
        *output_guard.OUTPUT_GUARD_FLAG_TYPES,
    }
)


# ---------------------------------------------------------------------------
# QA-6 (#36): per-segment status + qa_flags on the headless surfaces.
#
# The counts-only `{segments, translated}` shape hid the per-segment review state:
# a headless operator saw "2/2 translated" while BOTH segments sat quarantined in
# NEEDS_REVIEW with a dnt/glossary/refusal qa_flag, because the web editor renders
# those chips but the CLI / GET .../segments never surfaced them. These helpers add
# the missing per-segment payload; the endpoint + `doc show` attach it OPT-IN behind
# a `?detail=segments` flag (`doc show --detail`).
#
# CHOICE — flag vs flat extension: the batch `?detail=steps` precedent
# (api_batches_show) gates its unbounded per-member matrix behind a flag so a plain
# status check stays light. The per-segment array here is likewise unbounded (a doc
# can hold thousands of segments), so we follow that precedent: WITHOUT the flag the
# light `{segments, translated}` shape is byte-identical for existing consumers; WITH
# it each locale additionally carries `segment_details`. Documented in docs/CLI.md.
#
# _SEGMENT_SURFACED is the headless twin of the editor grid's _SURFACED set
# (app/web/documents.py): the same qa_flag families a linguist sees as chips (structural
# QE, the DNT/glossary write-seam adherence gates, the refusal / language-mismatch
# quarantine, and the number/placeholder/whitespace text-mechanics gates). ONE DELIBERATE
# ASYMMETRY (QW-B): the QA-1 `whitespace` family is here but NOT in the editor's _SURFACED,
# because the editor RECOMPUTES whitespace live via qa.check_segment, so surfacing the
# persisted twin there would render an identical duplicate badge; a headless caller does not
# recompute, so it needs the persisted flag. placeholder_integrity IS in both (distinct from
# the live "tags" badge, like number_integrity).
# FULL_TEXT source-fallback attribution (GitHub #42): a
# paragraph reverted to SOURCE leaves a structurally
# perfect body, so this flag is the ONLY signal that part
# of the document is untranslated. Mirror the editor grid
# (app/web/documents.py _SURFACED) so a headless caller
# sees the same reason a linguist would see as a chip.
# LQA safety floor (C): a step edit that was REVERTED for
# breaking a term, and the FULL_TEXT attribution flag. Both
# route the segment to NEEDS_REVIEW, so a headless operator
# must see WHY -- the QA-6 (#36) rule.
# Number-integrity gate: a MAJOR, value-changing numeric
# divergence routes the segment to NEEDS_REVIEW and makes
# it TM-ineligible. It persisted but was filtered out of
# every surface (headless qa_flags came back [], the
# summary line returned None, no editor chip) -- a
# quarantine with no stated reason, the exact defect
# QA-6 (#36) exists to prevent.
# LC-7 real-placeholder integrity and QA-1 whitespace
# hygiene: both persist a write-seam qa_flag and route the
# segment to NEEDS_REVIEW, so a headless caller must see the
# reason (the QA-6 rule) rather than a needs_review segment
# with an empty qa_flags array.
# Per-segment STEP FAILURE (GitHub #45): the segment kept
# its PRE-STEP text because the step meant to improve it
# errored for it alone, and it is routed to NEEDS_REVIEW
# for that reason. Without this the headless caller sees a
# needs_review segment with an EMPTY qa_flags array and no
# summary bucket -- a quarantine with no stated reason,
# the exact defect QA-6 (#36) exists to prevent.
def _segment_detail_row(seg, *, session=None, drift=None) -> dict:
    """One segment's headless review payload: id / order / status / whether a target
    is present, plus the SURFACED qa_flags. QE flags retain sanitized history and decision fields so
    the shared summary can distinguish informational badges from review findings.
    Shared by GET /api/documents/{id}/segments?detail=segments AND the in-process
    `doc show --detail` local arm, so the two headless surfaces cannot drift."""
    from app.kernel import qe_autofix

    flags = [
        {
            "type": f.get("type"),
            "severity": f.get("severity"),
            "message": f.get("message"),
            **(
                {"passed": f.get("passed"), "skipped": f.get("skipped")}
                if f.get("type") == "qe"
                else {}
            ),
            **({"outcome": f.get("outcome")} if f.get("type") == "qe_autofix" else {}),
            **(
                qe_autofix.public_flag(f, session=session)
                if f.get("type") in ("qe", "qe_autofix")
                else {}
            ),
        }
        for f in (seg.qa_flags or [])
        if isinstance(f, dict) and f.get("type") in _SEGMENT_SURFACED
    ]
    row = {
        "id": seg.id,
        "order": seg.order_index,
        "status": seg.status,
        "target_present": bool((seg.target_text or "").strip()),
        "qa_flags": flags,
    }
    # TM-matching lock surfacing (PROPOSAL-tm-matching-workflow-step §8): attached ONLY
    # when the segment is locked, so an unlocked segment's payload stays byte-identical
    # (the established "key present only when non-default" shape). Shared by GET
    # /api/documents/{id}/segments and the CLI `doc show --detail` arm -> parity for free.
    from app.kernel import tm_lock

    lock = tm_lock.lock_summary(seg)
    if lock:
        # #617 item 5: the locked TM entry was edited after the fill (key present only then).
        if drift and seg.id in drift:
            lock["tm_changed"] = True
            lock["tm_changed_at"] = drift[seg.id]["changed_at"]
        row["lock"] = lock
    return row


@router.get("/api/documents/{doc_id}/segments")
def api_document_segments(doc_id: int, request: Request):
    """Per-locale segment + translated counts (mirrors `doc show`).

    `?detail=segments` (QA-6) additionally attaches a per-segment `segment_details`
    array to each locale (id / order / status / target_present / qa_flags[]), so a
    headless operator can see WHICH segments were quarantined for review and why — the
    counts alone read "2/2 translated" even when both are needs_review. The flag is
    OPT-IN, mirroring the batch `?detail=steps` precedent: without it the light
    `{segments, translated}` shape is byte-identical for existing consumers."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import orchestrator, tm_lock

    detail = request.query_params.get("detail") == "segments"
    with Session(engine) as s:
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        by_locale: dict = {}
        for sg in segs:
            by_locale.setdefault(sg.target_locale, []).append(sg)
        locales = {}
        for loc in sorted(by_locale):
            group = sorted(by_locale[loc], key=lambda x: x.order_index)
            translated = sum(1 for x in group if (x.target_text or "").strip())
            entry = {"segments": len(group), "translated": translated}
            # #617: a run parked on a human_review step (key absent otherwise, so every
            # other payload is byte-identical). `pending_mt` rides on the detail rows.
            waiting = orchestrator.waiting_human_review(s, doc_id, loc)
            if waiting:
                entry["awaiting_review"] = {
                    "run_id": waiting["run_id"],
                    "step": waiting["step_name"],
                }
            if detail:
                _drift = tm_lock.drifted(s, group)
                entry["segment_details"] = [
                    _segment_detail_row(x, session=s, drift=_drift) for x in group
                ]
                if waiting and len(waiting["segments"]) == len(group):
                    for row, text in zip(
                        entry["segment_details"], waiting["segments"], strict=True
                    ):
                        row["pending_mt"] = text
            locales[loc] = entry
        out = {
            "ok": True,
            "id": doc.id,
            "filename": doc.filename,
            "fmt": doc.fmt,
            "mode": doc.mode,
            # #563: the derived lifecycle (new, in_translation, in_review, done), not the
            # stored column that never moves off "new".
            "status": lifecycle.document_status(s, doc),
            "locales": locales,
        }
        # VPAT 504.3 prompt, headless: present ONLY for a document whose format carries
        # images (key absent otherwise, so existing payloads are byte-identical).
        from app.filters import a11y as _a11y

        _summary = _a11y.a11y_summary(doc.skeleton)
        if _summary is not None:
            out["a11y_images"] = _summary
        if (doc.skeleton or {}).get("file_warnings"):
            out["file_warnings"] = doc.skeleton["file_warnings"]
    return JSONResponse(out)


@router.post("/api/documents/{doc_id}/apply-tm")
async def api_document_apply_tm(doc_id: int, request: Request):
    """BUG-08: remote-CLI surface for `doc apply-tm` (previously host-only — the CLI
    refused --remote outright and the only server side was the session-cookie web
    form POST /documents/{id}/apply-tm). can_write-gated (a content mutation, same
    RBAC posture as api_workflows_run / the other doc-scoped write routes in this
    file) and org+customer scoped via _project_in_org, mirroring every neighboring
    doc-scoped route above. Body: {"locale": "..."}. Calls the SAME
    content.apply_tm_to_document() the web route and the in-process CLI arm call —
    no duplicated matching/QA logic here, just the /api shell.

    ORDERING (adversarial-review fix, verify_rename_delete_projects' by-id sweep):
    SCOPE FIRST, validate second. Rejecting a blank `locale` with a 400 before
    resolving the document made the route an EXISTENCE ORACLE — a customer-bound
    caller probing a FOREIGN customer's doc id got the same 400 as an in-scope one,
    so a 404-vs-400 difference revealed which ids exist outside its ring. A param
    error must only ever be reachable by a caller already inside the ring; everything
    else is the byte-identical 404 every neighboring doc-scoped route returns."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # uniform 403 regardless of doc id — leaks nothing
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        locale = str(body.get("locale") or "").strip()
        if not locale:
            return JSONResponse({"ok": False, "error": "locale is required."}, status_code=400)
        project = s.get(Project, doc.project_id)
        try:
            applied = content.apply_tm_to_document(s, doc, project, locale)
        except content.InvalidLocaleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except segment_review.AwaitingReviewError as exc:
            return JSONResponse(
                {"ok": False, "code": exc.code, "error": str(exc), "run_id": exc.run_id},
                status_code=exc.status,
            )
    return JSONResponse({"ok": True, "doc_id": doc_id, "locale": locale, "applied": applied})


# ============================================================================
# R1a content-parity: document rename/delete + segment unlock (SPEC-full-remote-
# parity Wave R1). RBAC mirrors the web routes exactly (documents.py): rename is
# can_write, delete is is_admin + typed confirm, unlock is visibility-scoped only
# (the web segment_unlock's deliberate gate — reviewer+ is the maintainer's call, not
# invented here). Every id funnels through _visible_document / _project_in_org so a
# foreign-org (or, for a bound caller, foreign-customer) id is a 404, never a 403.
# ============================================================================
@router.post("/api/documents/{doc_id}/rename")
async def api_document_rename(doc_id: int, request: Request):
    """Rename a document's filename (mirrors POST /documents/{id}/rename). can_write.
    SCOPE FIRST, validate second (the existence-oracle lesson, api_document_apply_tm):
    a blank name 400s only for a caller already inside the ring. Blob storage is
    id-keyed, so the upload is untouched — only the display name changes."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            doc = content.rename_document(
                s,
                doc_id,
                str(body.get("filename") or ""),
                actor=ident["actor"],
                ip=deps._ip(request),
                org_id=org_id,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "doc_id": doc_id, "filename": doc.filename})


@router.post("/api/documents/{doc_id}/delete")
async def api_document_delete(doc_id: int, request: Request):
    """Delete a document + its blob + segments (mirrors POST /documents/{id}/delete).
    ADMIN-only (every new delete is strictly is_admin). RING BEFORE ROLE (the permanent
    route-sweep finding, api_projects_delete): a foreign id 404s before the admin gate
    runs. Does NOT wipe the project's shared TM. Boolean privacy_erasure opts into
    deleting Lab snapshots too. A typed `confirm` echoing the exact filename is required,
    so a script cannot delete a document it never named."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        if str(body.get("confirm") or "") != doc.filename:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "confirm must equal the exact document filename to delete.",
                    "filename": doc.filename,
                },
                status_code=400,
            )
        counts = content.delete_document(
            s,
            doc_id,
            actor=ident["actor"],
            ip=deps._ip(request),
            org_id=org_id,
            privacy_erasure=body.get("privacy_erasure") is True,
        )
    return JSONResponse(
        {
            "ok": True,
            "doc_id": doc_id,
            "deleted": True,
            "cascade": counts,
            "warning": counts.get("warning", ""),
        }
    )


@router.post("/api/segments/{sid}/unlock")
async def api_segment_unlock(sid: int, request: Request):
    """Explicitly clear a segment's TM lock (mirrors POST /segments/{id}/unlock). Gated
    EXACTLY like the web route: REVIEWER+ role (`_review_block` — the web middleware's
    can_write gate the /api-exemption bypasses is not enough; clearing a hard TM lock is
    a reviewer+ action, models.Segment lock_hard note) AND project-visibility. The role
    gate is UNIFORM — it runs before the segment lookup, so a viewer/translator key gets a
    role 403 that leaks nothing about the id (mirrors the sibling api_segment_approve).
    The segment funnels through its document's project via _project_in_org, so a
    foreign-org/customer segment id 404s. A refused role writes NO `tm.unlock` row.
    Idempotent: `unlocked` is False when the segment was not locked."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _review_block(ident)  # reviewer+ — mirrors the web segment_unlock gate
    if blocked:
        return blocked
    from app.kernel import tm_lock

    with Session(engine) as s:
        seg = s.get(Segment, sid)
        if seg is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, seg.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            cleared = tm_lock.unlock_segment(s, seg, actor=ident["actor"], ip=deps._ip(request))
        except segment_review.AwaitingReviewError as exc:
            return JSONResponse(
                {"ok": False, "code": exc.code, "error": str(exc), "run_id": exc.run_id},
                status_code=exc.status,
            )
    return JSONResponse({"ok": True, "segment_id": sid, "unlocked": cleared})


@router.post("/api/segments/{sid}/approve")
async def api_segment_approve(sid: int, request: Request):
    """Approve a reviewed/edited segment and (under the refusal/language guards) write
    its pair to Translation Memory -- the /api twin of the web CAT `POST
    /segments/{id}/approve` (chip task_9c665111 / AUDIT-phase2 FINDING 4: this action
    existed only on the web surface). Runs the IDENTICAL guards via the shared
    segment_review seam, so a hedge / non-translation is still WITHHELD from shared TM
    on every surface.

    RBAC (review H1): approve is a MUTATING action. The web route is can_write-gated by
    the global middleware (app/main.py) -- but that middleware EXEMPTS /api as
    self-governed, so this route must self-apply can_write. It does (`_write_block`),
    mirroring the sibling write route POST /api/documents/{doc_id}/apply-tm; a
    viewer-role key is 403'd, not silently allowed to seed TM. Tenancy: project-
    visibility via _project_in_org (foreign-org/customer segment id 404s). Body-less.

    KEY-PROJECT ASYMMETRY (review I1, safe direction): a key project's value segment
    lives in a hidden `fmt="keys"` document that `_visible_document` excludes, so this
    route (and `doc approve-segment`) 404 it -- approving a KEY value to TM is web-only.
    Deliberate for now: key-value TM write-back has never been an /api story (the key
    grid is its home). Documented rather than silently divergent.

    PLACEHOLDER GATE (Issue #562): a target that drops or adds a real placeholder
    ({count}, %s) is approved but WITHHELD from TM (`tm_written:false`, a `tm_skipped`
    reason and `tm_skipped_code:"placeholder_integrity"`). An optional JSON body
    `{"acknowledge_placeholder_loss": true}` (a real boolean) writes it anyway."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # H1: approve is a can_write action (mirrors the web
    if blocked:  # middleware gate the /api-exemption bypasses)
        return blocked
    from app.kernel import segment_review

    raw_ack = (await _json(request)).get("acknowledge_placeholder_loss")
    if raw_ack is not None and not isinstance(raw_ack, bool):
        return JSONResponse(
            {"ok": False, "error": "acknowledge_placeholder_loss must be a JSON boolean."},
            status_code=400,
        )
    ack = bool(raw_ack)
    raw_prop = (await _json(request)).get("propagate")
    if raw_prop is not None and not isinstance(raw_prop, bool):
        return JSONResponse(
            {"ok": False, "error": "propagate must be a JSON boolean."}, status_code=400
        )
    prop = raw_prop is not False

    with Session(engine) as s:
        seg = s.get(Segment, sid)
        if seg is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, seg.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        project = s.get(Project, doc.project_id)
        try:
            # M6: audit is logged inside the seam (actor+ip passed) so every surface
            # leaves the same segment.approve trail.
            out = segment_review.approve_and_write_tm(
                s,
                seg,
                project,
                doc,
                actor=ident["actor"],
                ip=deps._ip(request),
                acknowledge_placeholder_loss=ack,
                propagate=prop,
            )
        except segment_review.SegmentApproveError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    body = {"ok": True, "segment_id": sid, "status": out["status"], "tm_written": out["tm_written"]}
    if out["tm_skipped"]:
        body["tm_skipped"] = out["tm_skipped"]
    if out.get("tm_skipped_code"):
        body["tm_skipped_code"] = out["tm_skipped_code"]
    # Issue #617 item 6: the repeats this approval was applied to, and any it refused.
    body["propagated"] = out["propagated"]
    body["propagation_refused"] = out["propagation_refused"]
    if out.get("propagation_skipped"):
        body["propagation_skipped"] = out["propagation_skipped"]
    return JSONResponse(body)


@router.get("/api/content-types")
def api_content_types_list(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import content_types as ctsvc

    with Session(engine) as s:
        types = ctsvc.list_types(s, org_id=org_id, customer_id=_cust_of(ident))
        payload = [ctsvc.to_dict(s, t) for t in types]
    return JSONResponse({"ok": True, "content_types": payload})


@router.post("/api/content-types")
async def api_content_types_create(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # ContentType CRUD -> can_tune_ai (web parity)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import content_types as ctsvc

    with Session(engine) as s:
        # R2-3: a customer-bound caller is FORCED to its own customer (a foreign /
        # blank customer_id can never create a global or foreign-customer type). A
        # staff caller may pass a customer_id (validated against its org) or blank
        # (global). A foreign customer_id from a staff caller falls through to
        # attachable-less create — so validate it belongs to the caller's org.
        bound = _cust_of(ident)
        if bound is not None:
            cust_id = bound
        else:
            cust_id = body.get("customer_id")
            raw = str(cust_id).strip() if cust_id is not None else ""
            if raw:
                cid_val = _int_id(raw)
                if cid_val is None or _customer_in_org(s, cid_val, org_id, ident) is None:
                    return JSONResponse(
                        {"ok": False, "error": "unknown customer."}, status_code=400
                    )
                cust_id = cid_val
        try:
            # RAW name so the service refuses a non-string (BUG-A13); clean_name also
            # caps length (BUG-A12).
            c = ctsvc.create(
                s,
                body.get("name"),
                description=str(body.get("description") or ""),
                org_id=org_id,
                customer_id=cust_id,
                created_by=ident["actor"],
            )
        except ids.InvalidNameError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except ctsvc.ContentTypeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **ctsvc.to_dict(s, c)}
    audit.log(
        actor=ident["actor"],
        action="content_type.create",
        target=f"content_type {out['id']}",
        ip=deps._ip(request),
        customer_id=out.get("customer_id"),
    )
    return JSONResponse(out)


@router.post("/api/content-types/{cid}/rename")
async def api_content_types_rename(cid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import content_types as ctsvc

    with Session(engine) as s:
        existing = _content_type_in_scope(s, cid, org_id, ident)
        if existing is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        old_name = existing.name
        try:
            c = ctsvc.rename(s, cid, str(body.get("name") or ""))
        except ctsvc.ContentTypeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, **ctsvc.to_dict(s, c)}
        new_name = c.name
    # Universal rename rule: record old -> new in the audit detail (was missing).
    audit.log(
        actor=ident["actor"],
        action="content_type.rename",
        target=f"content_type {cid}",
        ip=deps._ip(request),
        detail=f"{old_name!r} -> {new_name!r}",
    )
    return JSONResponse(out)


@router.post("/api/content-types/{cid}/delete")
async def api_content_types_delete(cid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    from app.kernel import content_types as ctsvc

    with Session(engine) as s:
        if _content_type_in_scope(s, cid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            ctsvc.delete(s, cid)  # FAILS CLOSED while referenced
        except ctsvc.ContentTypeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident["actor"],
        action="content_type.delete",
        target=f"content_type {cid}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "content_type_id": cid, "deleted": True})


# ============================================================================
# Segmentation profiles — rename parity (P6, item 8). RBAC mirrors the web
# /settings segprofile routes: every mutation is admin-gated (is_admin), not a
# lesser role. Org-scoped exactly like the web (_org_block); a foreign org's
# profile is indistinguishable from a missing one (404).
# ============================================================================
@router.post("/api/segprofiles/{pid}/rename")
async def api_segprofiles_rename(pid: int, request: Request):
    """Remote-CLI/web parity for renaming a segmentation profile — wires the
    existing service update_profile(name=...) path. Admin-only (web segprofile
    routes are all _admin_block). Keeps the service dup-check; audits old -> new."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _role_block(ident, security.is_admin)  # web parity: admin-only
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import segmentation_profiles as segprof

    prof = segprof.get_profile(pid, org_id=org_id)
    if prof is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    old_name = prof.name
    try:
        prof = segprof.update_profile(pid, name=str(body.get("name") or "").strip(), org_id=org_id)
    except segprof.SegProfileError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if prof is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident["actor"],
        action="segprofile.rename",
        target=f"profile {pid}",
        ip=deps._ip(request),
        detail=f"{old_name!r} -> {prof.name!r}",
    )
    return JSONResponse({"ok": True, "profile": segprof.profile_row(prof)})


# --- segmentation profiles: list/show/create/clone/delete/set-default/assign --
# RBAC: is_admin, matching the EXISTING /api/segprofiles/{pid}/rename sibling and the
# web /settings/segprofiles routes (all _admin_block). See the final report's spec
# deviation note: the SPEC table cell says can_tune_ai, but every sibling + the web
# surface gate is_admin, so shipping can_tune_ai here would let a linguist create/delete
# profiles their web session cannot — a privilege escalation and a web-parity break.
# Org-scoped (a foreign org's profile is 404); a customer id funnels through
# _customer_in_org. A customer-bound key is never admin, so it never reaches these.
@router.get("/api/segprofiles")
def api_segprofiles_list(request: Request):
    """List segmentation profiles (mirrors `segprofile list`). A read — org scoped."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import segmentation_profiles as segprof

    # #477: a customer-bound key sees only its own customer's profiles plus globals.
    rows = [
        segprof.profile_row(p)
        for p in segprof.list_profiles(org_id=org_id)
        if not access.customer_ring_blocks(_cust_of(ident), getattr(p, "customer_id", None))
    ]
    return JSONResponse({"ok": True, "segprofiles": rows})


@router.get("/api/segprofiles/{pid}")
def api_segprofiles_show(pid: int, request: Request):
    """Show one segmentation profile's metadata (mirrors `segprofile show`; the SRX blob
    never echoes). Org bound via get_profile(org_id=...) — a foreign org's is 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import segmentation_profiles as segprof

    prof = segprof.get_profile(pid, org_id=org_id)
    # #477: another customer's profile is a 404 for a customer-bound key, like missing.
    if prof is None or access.customer_ring_blocks(
        _cust_of(ident), getattr(prof, "customer_id", None)
    ):
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "segprofile": segprof.profile_row(prof)})


@router.post("/api/segprofiles")
async def api_segprofiles_create(request: Request):
    """Create a segmentation profile (mirrors `segprofile create`). is_admin. Body: name,
    srx_b64 (optional base64 .srx; omit for the built-in rules), customer_id (optional;
    validated in-org), description, system_default. A customer_id funnels through
    _customer_in_org (foreign/cross-org = 404) BEFORE the create."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _role_block(ident, security.is_admin)  # web /settings parity: admin-only
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import segmentation_profiles as segprof

    srx_blob = None
    if body.get("srx_b64"):
        raw = _b64(body.get("srx_b64"))
        if raw is None:
            return JSONResponse(
                {"ok": False, "error": "srx_b64 is not valid base64."}, status_code=400
            )
        srx_blob = raw.decode("utf-8", errors="replace")
    cust_id = None
    if body.get("customer_id") is not None and str(body.get("customer_id")).strip():
        cid = _int_id(body.get("customer_id"))
        with Session(engine) as s:
            if cid is None or _customer_in_org(s, cid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        cust_id = cid
    try:
        prof = segprof.create_profile(
            str(body.get("name") or ""),
            srx_blob=srx_blob,
            customer_id=cust_id,
            description=str(body.get("description") or ""),
            is_system_default=bool(body.get("system_default")),
            actor=ident["actor"],
            org_id=org_id,
        )
    except segprof.SegProfileError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident["actor"],
        action="segprofile.create",
        target=f"profile {prof.id} ({prof.name})",
        ip=deps._ip(request),
        customer_id=prof.customer_id,
    )
    return JSONResponse({"ok": True, "segprofile": segprof.profile_row(prof)})


@router.post("/api/segprofiles/{pid}/clone")
async def api_segprofiles_clone(pid: int, request: Request):
    """Clone a profile's ruleset under a new name (mirrors `segprofile clone`). is_admin.
    Body: new_name, customer_id (optional re-scope; '' globalizes). Org bound via
    get_profile — a foreign source is 404, resolved BEFORE the create."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _role_block(ident, security.is_admin)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import segmentation_profiles as segprof

    if segprof.get_profile(pid, org_id=org_id) is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    cust = body.get("customer_id")
    if cust is not None and str(cust).strip():
        cid = _int_id(cust)
        with Session(engine) as s:
            if cid is None or _customer_in_org(s, cid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        cust = cid
    try:
        prof = segprof.clone_profile(
            pid,
            str(body.get("new_name") or ""),
            customer_id=cust,
            actor=ident["actor"],
            org_id=org_id,
        )
    except segprof.SegProfileError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident["actor"],
        action="segprofile.clone",
        target=f"profile {prof.id} (from {pid})",
        ip=deps._ip(request),
        customer_id=prof.customer_id,
    )
    return JSONResponse({"ok": True, "segprofile": segprof.profile_row(prof)})


@router.post("/api/segprofiles/{pid}/set-default")
async def api_segprofiles_set_default(pid: int, request: Request):
    """Set or clear the system-default segmentation profile (mirrors `segprofile
    set-default`). is_admin. Body: clear (bool). Org bound via get_profile."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _role_block(ident, security.is_admin)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import segmentation_profiles as segprof

    if segprof.get_profile(pid, org_id=org_id) is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    try:
        prof = segprof.update_profile(
            pid, is_system_default=(not bool(body.get("clear"))), org_id=org_id
        )
    except segprof.SegProfileError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if prof is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident["actor"],
        action="segprofile.set_default",
        target=f"profile {pid}",
        detail=f"system_default={prof.is_system_default}",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "segprofile": segprof.profile_row(prof)})


@router.post("/api/segprofiles/{pid}/delete")
async def api_segprofiles_delete(pid: int, request: Request):
    """Delete a segmentation profile (mirrors `segprofile delete`). is_admin. FAILS
    CLOSED (400) while any project/customer default still references it. Org bound via
    get_profile (404 foreign) resolved before the admin gate."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import segmentation_profiles as segprof

    prof = segprof.get_profile(pid, org_id=org_id)
    if prof is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    blocked = _role_block(ident, security.is_admin)
    if blocked:
        return blocked
    name = prof.name
    try:
        ok = segprof.delete_profile(pid, org_id=org_id)
    except segprof.SegProfileError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not ok:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident["actor"],
        action="segprofile.delete",
        target=f"profile {pid} ({name})",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "id": pid, "deleted": True})


@router.post("/api/segprofiles/assign")
async def api_segprofiles_assign(request: Request):
    """Set or clear a project's segmentation-profile override (mirrors `segprofile
    assign`). is_admin. Body: project_id, profile_id ('' / null clears). The project
    funnels through _project_in_org (404 foreign); assign_to_project validates the
    profile is global or the project's own customer's."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _role_block(ident, security.is_admin)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import segmentation_profiles as segprof

    with Session(engine) as s:
        pid = _int_id(body.get("project_id"))
        if pid is None or _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            proj = segprof.assign_to_project(s, pid, body.get("profile_id"), org_id=org_id)
        except segprof.SegProfileError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if proj is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        assigned = proj.segmentation_profile_id
    audit.log(
        actor=ident["actor"],
        action="segprofile.assign",
        target=f"project {pid}",
        detail="profile=%s" % (assigned or "cleared"),
        ip=deps._ip(request),
        customer_id=audit.customer_for_project(pid),
    )
    return JSONResponse({"ok": True, "project_id": pid, "segmentation_profile_id": assigned})


@router.get("/api/documents/{doc_id}/lqa/findings")
async def api_lqa_findings_list(doc_id: int, request: Request):
    """List non-withdrawn reviewer-authored findings for a document (optionally one
    locale). Doc-visibility scoped (mirrors the LQA page read); a foreign doc id 404s."""
    from app.kernel import reports as _reports

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        locale = (request.query_params.get("locale") or "").strip() or None
        rows = _reports.human_findings_for_document(s, document_id=doc_id, target_locale=locale)
        return JSONResponse({"ok": True, "findings": [_lqa_finding_dict(f) for f in rows]})


@router.post("/api/documents/{doc_id}/lqa/findings")
async def api_lqa_finding_log(doc_id: int, request: Request):
    """Log one reviewer MQM finding on a segment of this document. REVIEWER+. Body:
    {segment_id, dimension, severity, note}."""
    from app.kernel import lqa as _lqa

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # VISIBILITY BEFORE ROLE (#70 — mirrors the web lqa_finding_log's RING-BEFORE-
        # BODY guard + its GET twin): a foreign/invisible document 404s BEFORE the
        # reviewer gate can turn it into a role 403 that reveals the doc exists.
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _lqa_finding_review_block(ident, "log")
        if blocked:
            return blocked
        seg = s.get(Segment, _int_id(body.get("segment_id")))
        if seg is None or seg.document_id != doc_id:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            row = _lqa.log_human_finding(
                s,
                segment=seg,
                dimension=str(body.get("dimension") or ""),
                severity=str(body.get("severity") or ""),
                note=str(body.get("note") or ""),
                user=_creator(ident),
                actor=ident["actor"],
            )
        except (_lqa.HumanFindingError, _lqa.HumanFindingForbidden) as exc:
            return _lqa_finding_api_error(exc)
        audit.log(
            actor=ident["actor"],
            action="lqa.finding.log",
            target=f"finding:{row.id}",
            detail=f"doc={doc_id} seg={seg.id} {row.dimension}/{row.severity}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(doc.project_id),
        )
        out = _lqa_finding_dict(row)
    return JSONResponse({"ok": True, "finding": out})


@router.post("/api/segments/{sid}/propagation")
async def api_segment_propagation(sid: int, request: Request):
    """Issue #617 item 6: the repeat-propagation controls of one segment (twin of the web
    `POST /segments/{id}/propagation`). JSON body `{"linked": false}` unlinks the segment (it
    neither receives nor sends propagated text), `{"linked": true}` links it again,
    `{"undo": true}` restores the repeats the LAST approval of this segment propagated, while
    they are still exactly as propagated. A can_write action with project visibility, like the
    sibling approve route; every change is audited."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    from app.kernel import segment_review

    body = await _json(request)
    undo, linked = body.get("undo"), body.get("linked")
    if (undo is not None and not isinstance(undo, bool)) or (
        linked is not None and not isinstance(linked, bool)
    ):
        return JSONResponse(
            {"ok": False, "error": "undo and linked must be JSON booleans."}, status_code=400
        )
    if undo is not True and linked is None:
        return JSONResponse(
            {"ok": False, "error": 'send {"linked": true|false} or {"undo": true}.'},
            status_code=400,
        )
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        if seg is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, seg.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            if undo is True:
                res = segment_review.undo_propagation(
                    s, seg, doc, actor=ident["actor"], ip=deps._ip(request)
                )
                return JSONResponse({"ok": True, "segment_id": sid, **res})
            changed = segment_review.set_propagation(
                s, seg, doc, linked=linked, actor=ident["actor"], ip=deps._ip(request)
            )
        except segment_review.AwaitingReviewError as exc:
            return JSONResponse(
                {"ok": False, "code": exc.code, "error": str(exc), "run_id": exc.run_id},
                status_code=exc.status,
            )
    return JSONResponse({"ok": True, "segment_id": sid, "linked": linked, "changed": changed})
