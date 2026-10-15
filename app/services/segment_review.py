"""Approve a reviewed/edited segment and write its pair to Translation Memory.

THE ONE SEAM (chip task_9c665111) behind the CAT "approve" action on every surface.
Before this, the whole approve-and-write-TM policy lived INLINE in the web route
(`app/web/documents.py::segment_approve`), so `/api`, the CLI and MCP had no way to save
a reviewed segment to TM at all (AUDIT-phase2 FINDING 4). Extracting it here lets the web
route, `POST /api/segments/{sid}/approve`, `doc approve-segment` and the MCP
`approve_segment` tool all run the IDENTICAL guards -- the refusal/language perimeter can
never diverge across surfaces.

WHAT IT DOES (byte-identical to the pre-extraction web route):
  * a SOFT-DELETED key's value is refused (SegmentApproveError code='deleted_key', 404) --
    the same guard keys.set_value applies, so approving cannot seed shared TM from content
    the customer has retired;
  * the segment is marked APPROVED and its stale QE "routed to review" badge cleared (an
    approved segment has been reviewed, so the old gate decision is resolved);
  * the TM WRITE runs through the refusal/egress perimeter (repo law):
    a suspected refusal-hedge (`refusal.is_refusal`) OR a real-provider language mismatch
    (`refusal.looks_untranslated`) WITHHOLDS the TM write -- the segment still keeps its
    approved target (a human may knowingly accept a borderline unit), but a hedge/
    non-translation never propagates into shared TM as a future 100%-match that would
    silently defeat the guard downstream (finding C3). Only ever WITHHOLDS a write; never
    rewrites or drops the approved target.
  * a target whose real placeholders ({count}, %s, ...) diverge from the source is approved
    but WITHHELD from TM too (Issue #562), unless the caller passes
    `acknowledge_placeholder_loss=True` (web confirm, API body flag, CLI flag, MCP arg);
  * otherwise the pair is upserted via `tm.write_back` (which itself refuses token-bearing
    and source-fallback segments -- GitHub #42).

The PROVIDER-RESOLUTION asymmetry (real provider vs the offline MockProvider) is the same
deliberate, documented one the web route carried: the language backstop runs only for a
real provider (never the mock echo, which would be a systemic false positive), resolved
via `project.default_provider` as the best available proxy (a Segment carries no
per-write provider provenance).

REPEAT PROPAGATION (Issue #617 item 6, owner's decision 2026-10-08): the same seam then copies the
approved target onto the document's other REPEATS of this source (see `propagate_repeats`),
so every surface (web editor, /api, CLI, MCP) propagates identically.

Documented in: docs/architecture/collaboration-and-review.md
"""

import copy
import hashlib
import logging

from sqlalchemy import text as sa_text
from sqlmodel import select

from app import config
from app.models import Segment, SegStatus, TransMode
from app.services import audit, qa, qe, quality, refusal, tm

_logger = logging.getLogger(__name__)


class SegmentApproveError(Exception):
    """A DATA-level refusal (not auth) each surface maps to the right status/shape.
    `code` names the reason ('deleted_key'); `status` is the HTTP status the /api + web
    routes use (the CLI/MCP render the message)."""

    def __init__(self, message: str, *, code: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


PLACEHOLDER_SKIP_CODE = "placeholder_integrity"
# #581: the inline-tag withholds (never acknowledgeable, see the gate below).
TAG_SKIP_CODE = "tag_integrity"
TAGGED_SOURCE_SKIP_CODE = "tagged_source"
SOURCE_FALLBACK_SKIP_CODE = "source_fallback"
# #581: the refusal-guard withholds also carry a code (they only had prose before).
REFUSAL_SKIP_CODE = "suspected_refusal"
LANGUAGE_SKIP_CODE = "language_mismatch"


def awaiting_review_run(session, seg) -> dict | None:
    """#617 item 4: the human_review pause (see orchestrator.waiting_human_review) holding the
    segment's document+locale, or None. One cheap PAUSED-run lookup when nothing is paused."""
    from app.services.orchestrator_part3 import waiting_human_review

    return waiting_human_review(session, seg.document_id, seg.target_locale)


AWAITING_REVIEW_CODE = "awaiting_review"


class AwaitingReviewError(ValueError):
    """#617 item 4 (gate): a write to a document+locale whose run is parked on a human_review
    step. Every segment writer outside the run itself raises this (or answers the same 409)
    so nothing it writes can be overwritten by, or fight, the Approve-and-resume. A
    ValueError so the CLI's existing usage-error mapping renders it cleanly; `status` is the
    HTTP status every web and /api surface answers (409, code `awaiting_review`)."""

    status = 409
    code = AWAITING_REVIEW_CODE

    def __init__(self, run_id: int, message: str | None = None):
        self.run_id = run_id
        super().__init__(
            message
            or (
                f"this document is waiting for a human review step (run {run_id}); approve "
                "and resume the run instead of editing its segments."
            )
        )


def refuse_if_awaiting_review(session, document_id: int, locale: str) -> None:
    """Raise AwaitingReviewError when `document_id`+`locale` is held by a human_review pause.
    Callers run it AFTER their visibility gate, so it is never an existence oracle."""
    from app.services.orchestrator_part3 import waiting_human_review

    waiting = waiting_human_review(session, document_id, locale)
    if waiting is not None:
        raise AwaitingReviewError(waiting["run_id"])


def approve_and_write_tm(
    session,
    seg,
    project,
    doc,
    *,
    actor=None,
    ip=None,
    acknowledge_placeholder_loss=False,
    propagate=True,
) -> dict:
    """Approve `seg` and (under the guards) write its pair to TM. Returns
    {"status": str, "tm_written": bool, "tm_skipped": str|None, "propagated": [segment ids],
    "propagation_refused": [{"segment_id", "code", "reason"}]} (+ "tm_skipped_code" on a
    named withhold: placeholder_integrity is the only one a client may acknowledge;
    + "propagation_skipped" when propagation was not attempted and why). `propagate=False`
    is the per-call opt-out of repeat propagation. Raises
    SegmentApproveError on a data-level refusal. Callers do the auth/tenancy gate and
    pass the already-resolved `seg`, its `project` and `doc`.

    AUDIT (review M6): the successful approve is logged HERE, at the one seam, so web +
    /api + CLI all leave the SAME `segment.approve` trail (previously only the /api route
    logged; the CLI in-process arm left none). `actor`/`ip` are the surface's own actor +
    client ip (None from a trusted internal caller). A refusal (deleted key) logs nothing
    -- nothing was approved."""
    # Deleted-key guard (data safety, mirrors keys.set_value + the web route).
    key_id = getattr(seg, "key_id", None)
    if key_id:
        from app.models import StringKey

        key = session.get(StringKey, key_id)
        if key is not None and getattr(key, "deleted_at", None) is not None:
            raise SegmentApproveError(
                "this key is deleted; restore it in the key grid before writing values.",
                code="deleted_key",
                status=404,
            )

    # #617 item 4: a run parked on human review holds this locale's text; approving a segment
    # now would be overwritten (or fight) the resume. Same refusal on web, /api, CLI and MCP.
    if awaiting_review_run(session, seg) is not None:
        raise SegmentApproveError(
            "this document is waiting for a human review step; approve and resume the run "
            "instead of approving segments.",
            code="awaiting_review",
            status=409,
        )

    seg.status = SegStatus.APPROVED
    # A propagated copy that a human now approves is a human decision: it is no longer "a
    # copy of segment N" (the badge and the propagation overwrite rule both key on the marker).
    clear_propagation_marker(seg)
    seg.qa_flags = qe.clear_qe_flag(seg.qa_flags)
    # GATE C ROUND 4, L3: and the GATE-HOLD badge. The ratified release rule is "a later run
    # whose own check passed over the unit's new text, OR a reviewer approving/editing every
    # held unit" -- this IS that approval, and `export._gate_held_reason_for_docs` already
    # stops counting the unit the moment it reads APPROVED. Without the clear the badge had
    # no remaining clearing path at all (the release path `continue`s a status-protected slot
    # out, and the export skips it), so signed-off work wore a permanent CRITICAL in the
    # editor. Only the gate-hold family is touched; every other flag survives.
    seg.qa_flags = quality.clear_gate_hold_flags(seg.qa_flags)
    # Decisions 11 / 14 / 16 (gate): the review-hold badge (ambiguous date, line breaks, glued
    # words) asks a person to look; this approval is that look, so the badge goes (the copies
    # propagated below get the same through `_rejudge_flags`).
    seg.qa_flags = quality.clear_review_hold_flags(seg.qa_flags)
    session.add(seg)
    session.commit()

    guard_on = bool(config.REFUSAL_GUARD_ENABLED)
    # Issue #562: a target whose real placeholders ({count}, %s, ...) diverge from the source
    # is a customer-visible / runtime-crashing defect. The human may still approve it (the
    # target is kept, like a hedge), but it must never seed shared TM as a future 100% match
    # unless the caller explicitly acknowledges the loss. Judged LIVE on the text being
    # approved (not only the persisted write-seam flag), so every surface and every way the
    # target got there is covered by this one check.
    ph_reason = quality.placeholder_violation(seg.source_text, seg.target_text, segment=seg)
    ph_withheld = bool(ph_reason) and not acknowledge_placeholder_loss
    is_hedge = guard_on and refusal.is_refusal(seg.target_text)
    looks_nontrans = False
    if guard_on and not is_hedge:
        from app.services.providers import DeadEngineError, MockProvider, get_provider

        try:
            _prov = get_provider(project.default_provider if project else None)
        except DeadEngineError:
            _prov = MockProvider()
        if not isinstance(_prov, MockProvider):
            looks_nontrans = refusal.looks_untranslated(seg.target_text, seg.target_locale)
    # Gate (#562 / #581): inline-tag tokens. A source carrying inline-fidelity tokens is
    # never TM-eligible (tm.is_tm_eligible, GitHub #42 poisoning), and a target whose tokens
    # diverge from the source (the QA "tags" detector, quality.tag_damage) must never seed TM
    # either. Previously write_back refused silently, so the reply said tm_written:false with
    # no reason. The reason is now named. No acknowledgement applies: a token-bearing pair is
    # unusable as a TM match whatever the reviewer accepts, so this withhold is unconditional.
    tag_reason = (
        "" if (is_hedge or looks_nontrans) else quality.tag_damage(seg.source_text, seg.target_text)
    )
    tagged_source = not tm.is_tm_eligible(seg.source_text)
    if not (is_hedge or looks_nontrans) and (tag_reason or tagged_source):
        if tag_reason:
            skipped = (
                f"inline tag integrity broken ({tag_reason}) - not written to TM; a segment "
                "with damaged inline tags is never saved to TM"
            )
            code = TAG_SKIP_CODE
        else:
            skipped = (
                "source has inline tags - not written to TM; tagged segments are never saved to TM"
            )
            code = TAGGED_SOURCE_SKIP_CODE
        result = {
            "status": str(seg.status),
            "tm_written": False,
            "tm_skipped": skipped,
            "tm_skipped_code": code,
        }
    elif not (is_hedge or looks_nontrans) and ph_withheld:
        result = {
            "status": str(seg.status),
            "tm_written": False,
            "tm_skipped": (
                f"placeholder integrity broken ({ph_reason}) - not written to TM; acknowledge the "
                "placeholder loss to save it anyway"
            ),
            "tm_skipped_code": PLACEHOLDER_SKIP_CODE,
        }
    elif is_hedge or looks_nontrans:
        # NOTE (review L1): the ASCII hyphen here is a DELIBERATE change from the old
        # inline web route's em-dash ("- not written" vs "— not written"). This string is
        # no longer web-only -- it now rides the CLI `doc approve-segment` and the MCP
        # `approve_segment` reply, both of which require ASCII-safe output (repo law +
        # verify_segment_approve_parity's isascii() gate). No consumer parses tm_skipped
        # (surfaces display it verbatim), so the wording is unchanged; only the dash byte
        # differs, on purpose.
        skipped = (
            "suspected refusal - not written to TM"
            if is_hedge
            else "suspected non-translation (language mismatch) - not written to TM"
        )
        result = {
            "status": str(seg.status),
            "tm_written": False,
            "tm_skipped": skipped,
            "tm_skipped_code": REFUSAL_SKIP_CODE if is_hedge else LANGUAGE_SKIP_CODE,
        }
    else:
        written = tm.write_back(
            session,
            doc.project_id,
            seg.source_text,
            seg.target_text,
            project.source_locale,
            seg.target_locale,
            segment=seg,
        )
        result = {"status": str(seg.status), "tm_written": bool(written), "tm_skipped": None}
        if not written:
            # #581: write_back's remaining silent refusals get a named reason too.
            if tm._source_fallback_blocked(seg):
                result["tm_skipped"] = (
                    "target reverted to source text - not written to TM; source-fallback "
                    "text is never saved to TM"
                )
                result["tm_skipped_code"] = SOURCE_FALLBACK_SKIP_CODE
            elif not (seg.target_text or "").strip():
                result["tm_skipped"] = "empty target - not written to TM"
                result["tm_skipped_code"] = "empty_target"
            else:
                # #581: never a bare tm_written:false. write_back refused for a reason
                # this seam does not model, so say so rather than stay silent.
                result["tm_skipped"] = "not written to TM (the TM write was refused)"
                result["tm_skipped_code"] = "tm_write_refused"

    # M6: one audit trail for every surface. Detail names whether TM was actually seeded.
    # audit.log's fields are strings ("" defaults, joined into the HMAC chain) -- coerce
    # a None actor/ip from a trusted internal caller to the same defaults it uses.
    audit.log(
        actor=(actor or "system"),
        action="segment.approve",
        target=f"segment {seg.id} (doc {doc.id})",
        ip=(ip or ""),
        detail=(
            ("tm_written" if result["tm_written"] else "tm_withheld")
            # #562: an acknowledged placeholder loss that reached TM leaves a trace.
            + (";placeholder_loss_acknowledged" if (ph_reason and result["tm_written"]) else "")
        ),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    result["propagated"] = []
    result["propagation_refused"] = []
    if not propagate:
        result["propagation_skipped"] = "disabled for this call"
    elif result.get("tm_skipped_code") in _NO_PROPAGATE_CODES:
        result["propagation_skipped"] = "this target was withheld from TM as suspect"
    else:
        try:
            out = propagate_repeats(
                session,
                seg,
                doc,
                actor=actor,
                ip=ip,
                acknowledge_placeholder_loss=acknowledge_placeholder_loss,
            )
        except Exception:  # the approval is already committed: propagation must never undo it
            session.rollback()
            _logger.warning("repeat propagation failed for segment %s", seg.id, exc_info=True)
            out = {"propagated": [], "refused": [], "skipped": "propagation failed"}
        result["propagated"] = out["propagated"]
        result["propagation_refused"] = out["refused"]
        if out.get("skipped"):
            result["propagation_skipped"] = out["skipped"]
    return result


# =============================================================================
# REPEAT PROPAGATION (Issue #617 item 6)
# =============================================================================
# Industry-standard CAT behaviour (Trados, memoQ): confirming one copy of a repeated source
# sentence applies its target to the other copies that are still open. Each copy keeps its
# own MT first (nothing here changes translation); propagation only runs on an approve.
#
# DECISIONS (recorded in docs/architecture/collaboration-and-review.md):
#   * SCOPE: the SAME DOCUMENT and the same target locale only. The INCONSISTENT check and the
#     repetition band are both within-document, and a cross-document copy can live in a
#     different context (a UI label vs a paragraph); TM already carries it across documents.
#   * MATCH: the same normalization the INCONSISTENT check uses (`qa._consistency_norm`:
#     whitespace collapsed, case-folded). The target is then checked against EACH copy's own
#     source with the approve-time integrity rules, so case/spacing variants cannot smuggle in
#     a placeholder or tag break.
#   * STATE: there is no "propagated" SegStatus (35 write sites and every grid/export filter
#     key on the existing five). A propagated copy becomes EDITED (human-derived text, so a
#     later MT re-run cannot overwrite it) with `meta["propagated_from"] = <source seg id>`,
#     which drives a visible "propagated" badge. It is NEVER auto-approved: a linguist still
#     confirms it. A new approve may overwrite an earlier propagation (not human text) but
#     never a copy a human edited or approved.
#   * UNTOUCHED: APPROVED copies, copies a human edited (EDITED without the marker), locked
#     copies (hard or soft, `lock_band`), key-based rows, FULL_TEXT documents, and copies
#     marked `meta["no_propagate"]` (the per-segment unlink, which also stops a segment from
#     propagating OUT).
#   * REFUSED, not skipped: a copy whose own source the target would break (inline tag,
#     placeholder) is left as it was and reported in `refused`. An acknowledged placeholder
#     loss on the approve carries over (the approver accepted it for that text).
#   * AUDIT: ONE `segment.propagate` row lists the propagated ids (and the refused ones).
#   * UNDO: the previous target/status/flags of every touched copy are kept on the source's
#     `meta["last_propagation"]`; `undo_propagation` restores the copies that are still
#     exactly as propagated.
PROPAGATED_FROM_KEY = "propagated_from"
PROPAGATED_DIGEST_KEY = "propagated_digest"
NO_PROPAGATE_KEY = "no_propagate"
LAST_PROPAGATION_KEY = "last_propagation"
# The approve's own TM withholds that mean the target itself is suspect: never copy those.
_NO_PROPAGATE_CODES = frozenset(
    {REFUSAL_SKIP_CODE, LANGUAGE_SKIP_CODE, SOURCE_FALLBACK_SKIP_CODE, "empty_target"}
)
_AUDIT_ID_CAP = 200


def is_unlinked(seg) -> bool:
    """True when the segment is opted out of repeat propagation (both directions)."""
    return bool((getattr(seg, "meta", None) or {}).get(NO_PROPAGATE_KEY))


def _ids_detail(label: str, ids) -> str:
    ids = list(ids)
    shown = ",".join(str(i) for i in ids[:_AUDIT_ID_CAP])
    more = f"+{len(ids) - _AUDIT_ID_CAP} more" if len(ids) > _AUDIT_ID_CAP else ""
    return f"{label}={shown}{more}"


def _has_flag(seg, flag_type: str) -> bool:
    return any(isinstance(f, dict) and f.get("type") == flag_type for f in (seg.qa_flags or []))


def _take_write_lock(session) -> None:
    """Hold the database writer before reading rows we are about to overwrite (same device as
    admin_invariant): SQLite's no-op UPDATE takes the writer reservation, so no other request
    can commit between our read and our commit. Postgres gets row locks from FOR UPDATE in
    `_locked_copies` instead. The lock ends with our commit or rollback."""
    with session.no_autoflush:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(sa_text('UPDATE "segment" SET id = id WHERE 1 = 0'))


def _locked_copies(session, ids) -> list:
    """The segments `ids`, read in chunks (never one query per segment), row-locked where the
    database supports it and REFRESHED from the database so a stale identity-map copy cannot
    hide a change another request just committed. Ordered by document position.

    Gate: rows are locked in ascending id order across chunks (Postgres takes FOR UPDATE
    locks in output order), so two propagations over overlapping repeat sets queue on each other
    instead of deadlocking. Callers pass the SOURCE segment's id in the same set, so the source
    is locked in that one ordered pass too (no source-then-copies lock cycle)."""
    ids = sorted(set(ids))
    out: list = []
    for i in range(0, len(ids), 500):
        out.extend(
            session.exec(
                select(Segment)
                .where(Segment.id.in_(ids[i : i + 500]))
                .order_by(Segment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
    out.sort(key=lambda c: (c.order_index, c.id))
    return out


def _digest(text) -> str:
    return hashlib.sha1((text or "").encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def is_propagated_copy(seg) -> bool:
    """True while `seg` still holds exactly the text a propagation wrote (marker present AND the
    stored digest matches the current target). Gate: a writer that changes the text
    without knowing about the marker (an operator edit during a human_review pause, an inline-tag
    repair, a future writer) turns the copy back into ordinary text, so a later propagation can
    never overwrite it and the badge stops claiming it is a copy."""
    meta = getattr(seg, "meta", None) or {}
    return meta.get(PROPAGATED_FROM_KEY) is not None and meta.get(PROPAGATED_DIGEST_KEY) == _digest(
        getattr(seg, "target_text", "")
    )


def clear_propagation_marker(seg) -> None:
    """Drop the propagated-copy marker (a person edited or approved the text: it is theirs)."""
    meta = getattr(seg, "meta", None) or {}
    if PROPAGATED_FROM_KEY in meta or PROPAGATED_DIGEST_KEY in meta:
        seg.meta = {
            k: v for k, v in meta.items() if k not in (PROPAGATED_FROM_KEY, PROPAGATED_DIGEST_KEY)
        } or None


def _rejudge_flags(seg, source_text, target) -> None:
    """The flag courtesy a human save gives (documents_part3.segment_save): clear the stale
    QE routing badge and re-judge the integrity families against the text now stored."""
    flags = qe.clear_qe_flag(seg.qa_flags)
    flags = quality.clear_tag_flags(flags)
    ph = quality.placeholder_violation(source_text, target, segment=seg)
    flags = (
        quality.merge_placeholder_flags(flags, ph) if ph else quality.clear_placeholder_flags(flags)
    )
    ws = quality.whitespace_violation(source_text, target)
    flags = (
        quality.merge_whitespace_flags(flags, ws) if ws else quality.clear_whitespace_flags(flags)
    )
    num = quality.number_violation(source_text, target, seg.target_locale)
    flags = quality.merge_number_flags(flags, num) if num else quality.clear_number_flags(flags)
    # Decisions 11 / 14 / 16: a person approved this text, so the review hold has been seen.
    flags = quality.clear_review_hold_flags(flags)
    seg.qa_flags = flags


def propagate_repeats(
    session, seg, doc, *, actor=None, ip=None, acknowledge_placeholder_loss=False
) -> dict:
    """Copy the approved target of `seg` onto its open repeats in the same document.

    Returns {"propagated": [ids], "refused": [{"segment_id", "code", "reason"}],
    "skipped": str|None} (`skipped` names why nothing was attempted). Commits once and writes
    ONE `segment.propagate` audit row (only when something was propagated or refused)."""
    out: dict = {"propagated": [], "refused": [], "skipped": None}
    target = seg.target_text or ""
    if not target.strip():
        out["skipped"] = "empty target"
        return out
    if getattr(seg, "key_id", None):
        out["skipped"] = "key-based segment"
        return out
    if doc is not None and doc.mode == TransMode.FULL_TEXT:
        out["skipped"] = "full-text document"
        return out
    if is_unlinked(seg):
        out["skipped"] = "this segment is unlinked from its repeats"
        return out
    # #617 item 4 (gate): a run parked on human review holds this document+locale; the approve
    # itself is refused there, and a direct caller of this seam must not write copies either.
    if awaiting_review_run(session, seg) is not None:
        out["skipped"] = "this document is waiting for a human review step"
        return out
    norm = qa._consistency_norm(seg.source_text)
    if not norm:
        return out
    rows = session.exec(
        select(Segment.id, Segment.source_text).where(
            Segment.document_id == seg.document_id,
            Segment.target_locale == seg.target_locale,
            Segment.key_id.is_(None),
            Segment.id != seg.id,
        )
    ).all()
    repeat_ids = [sid for sid, src in rows if qa._consistency_norm(src) == norm]
    if not repeat_ids:
        return out
    # RACE (review): two linguists approving different repeats at once. The copies are read
    # AFTER the writer lock is held (SQLite) / with FOR UPDATE (Postgres) and refreshed from the
    # database, so a copy another request approved, edited, locked or unlinked a moment ago is
    # judged on its CURRENT state, and nobody can commit a change between this check and our
    # single commit. Without this a stale read could overwrite a just-approved copy.
    _take_write_lock(session)
    # Gate: the SOURCE row is re-read under the same lock (populate_existing refreshes
    # `seg` in place). Its meta is rewritten below, so a stale copy would drop an unlink (or
    # any other meta) another request committed meanwhile; and if its text changed or it was
    # unlinked after the approve, nothing is propagated.
    locked = _locked_copies(session, [*repeat_ids, seg.id])
    copies = [c for c in locked if c.id != seg.id]
    if (seg.target_text or "") != target or is_unlinked(seg):
        session.commit()  # release the writer
        out["skipped"] = "the segment changed before its repeats were updated"
        return out
    prev: dict = {}
    for c in copies:
        meta = dict(c.meta or {})
        if meta.get(NO_PROPAGATE_KEY) or c.lock_band or c.status == SegStatus.APPROVED:
            continue
        if c.status == SegStatus.EDITED and not is_propagated_copy(c):
            continue  # a human typed this (or changed a copy since): never overwritten
        if c.status == SegStatus.NEEDS_REVIEW and _has_flag(c, quality.TAG_FLAG_TYPE):
            continue  # a human-confirmed tag-breaking save lands here: keep it for review
        snap = {
            "t": c.target_text,
            "s": c.status,
            "q": copy.deepcopy(c.qa_flags),
            "f": meta.get(PROPAGATED_FROM_KEY),
            "d": meta.get(PROPAGATED_DIGEST_KEY),
        }
        # The write and its per-copy integrity gate are one call in the choke-point module.
        kind, reason = quality.write_propagated_copy(
            c, target, acknowledge_placeholder_loss=acknowledge_placeholder_loss
        )
        if kind:
            code = TAG_SKIP_CODE if kind == "tag" else PLACEHOLDER_SKIP_CODE
            out["refused"].append({"segment_id": c.id, "code": code, "reason": reason})
            continue
        prev[str(c.id)] = snap
        _rejudge_flags(c, c.source_text, target)
        meta[PROPAGATED_FROM_KEY] = seg.id
        meta[PROPAGATED_DIGEST_KEY] = _digest(target)
        c.meta = meta
        session.add(c)
        out["propagated"].append(c.id)
    if out["propagated"]:
        smeta = dict(seg.meta or {})
        smeta[LAST_PROPAGATION_KEY] = {"ids": out["propagated"], "text": target, "prev": prev}
        seg.meta = smeta
        session.add(seg)
    # Always end the transaction here: the writer reservation taken above must never outlive
    # this call (a caller that then writes through another session would wait on itself).
    session.commit()
    if out["propagated"] or out["refused"]:
        detail = [_ids_detail("propagated", out["propagated"])]
        if out["refused"]:
            detail.append(_ids_detail("refused", [r["segment_id"] for r in out["refused"]]))
        audit.log(
            actor=(actor or "system"),
            action="segment.propagate",
            target=f"segment {seg.id} (doc {seg.document_id})",
            ip=(ip or ""),
            detail=";".join(detail),
            customer_id=audit.customer_for_project(doc.project_id) if doc is not None else None,
        )
    return out


def undo_propagation(session, seg, doc, *, actor=None, ip=None) -> dict:
    """Undo the LAST propagation made from `seg`. A copy is restored only while it is still
    exactly as propagated (same text, still EDITED, marker intact, not locked); a copy a human
    touched since is kept and reported. Returns {"restored": [ids], "kept": [ids], "segments":
    [{"id", "target_text", "status"} of each restored copy, for the editor to redraw]}."""
    # #617 item 4 (gate): undo writes the copies, so a human_review hold refuses it (409
    # awaiting_review on web and /api, so MCP; a usage error on the CLI). Callers run their
    # visibility gate first, so this is never an existence oracle.
    refuse_if_awaiting_review(session, seg.document_id, seg.target_locale)
    out: dict = {"restored": [], "kept": [], "segments": []}
    if not (seg.meta or {}).get(LAST_PROPAGATION_KEY):
        return out
    # Same race rule as propagate_repeats: judge every copy on its CURRENT committed state
    # while holding the writer, so a copy approved or edited a moment ago is kept, not reverted.
    # Gate: the source row is locked and refreshed in the same ordered pass and the undo
    # record is read from THAT state, so two concurrent undos cannot both restore.
    _take_write_lock(session)
    stale_ids = ((seg.meta or {}).get(LAST_PROPAGATION_KEY) or {}).get("ids") or []
    found = {c.id: c for c in _locked_copies(session, [*stale_ids, seg.id])}
    last = (seg.meta or {}).get(LAST_PROPAGATION_KEY)
    if not last:
        session.commit()  # release the writer: another undo got here first
        return out
    prev = last.get("prev") or {}
    missing = [i for i in (last.get("ids") or []) if i not in found]
    if missing:
        found.update({c.id: c for c in _locked_copies(session, missing)})
    for sid in last.get("ids") or []:
        c = found.get(sid)
        meta = dict(c.meta or {}) if c is not None else {}
        snap = prev.get(str(sid))
        if (
            c is None
            or snap is None
            or sid == seg.id
            or c.document_id != seg.document_id
            or meta.get(PROPAGATED_FROM_KEY) != seg.id
            or not is_propagated_copy(c)
            or c.status != SegStatus.EDITED
            or c.target_text != last.get("text")
            or c.lock_band
            # compare-and-set in the choke-point module: only a copy still holding exactly
            # the propagated text is reverted
            or not quality.restore_propagated_copy(c, snap["t"], propagated_text=last.get("text"))
        ):
            out["kept"].append(sid)
            continue
        c.status = snap["s"]
        c.qa_flags = snap["q"]
        meta.pop(PROPAGATED_FROM_KEY, None)
        meta.pop(PROPAGATED_DIGEST_KEY, None)
        if snap.get("f") is not None:
            meta[PROPAGATED_FROM_KEY] = snap["f"]
            if snap.get("d") is not None:
                meta[PROPAGATED_DIGEST_KEY] = snap["d"]
        c.meta = meta or None
        session.add(c)
        out["restored"].append(sid)
        out["segments"].append(
            {
                "id": sid,
                "target_text": snap["t"],
                "status": str(getattr(snap["s"], "value", snap["s"])),
            }
        )
    seg.meta = {k: v for k, v in (seg.meta or {}).items() if k != LAST_PROPAGATION_KEY} or None
    session.add(seg)
    session.commit()
    detail = [_ids_detail("restored", out["restored"])]
    if out["kept"]:
        detail.append(_ids_detail("kept", out["kept"]))
    audit.log(
        actor=(actor or "system"),
        action="segment.propagate_undo",
        target=f"segment {seg.id} (doc {seg.document_id})",
        ip=(ip or ""),
        detail=";".join(detail),
        customer_id=audit.customer_for_project(doc.project_id) if doc is not None else None,
    )
    return out


def set_propagation(session, seg, doc, *, linked: bool, actor=None, ip=None) -> bool:
    """Link (the default) or unlink `seg` from repeat propagation, persisted on the segment and
    audited. Returns True when the state changed. An unlinked segment neither receives a
    propagation nor propagates its own approval."""
    # #617 item 4 (gate): link/unlink is a segment write too; refused under a human_review hold.
    refuse_if_awaiting_review(session, seg.document_id, seg.target_locale)
    # Gate: read the meta under the writer (refreshed), so this write cannot drop a
    # propagation marker or undo record another request committed a moment ago.
    _take_write_lock(session)
    _locked_copies(session, [seg.id])
    if is_unlinked(seg) == (not linked):
        session.commit()  # release the writer
        return False
    meta = {k: v for k, v in (seg.meta or {}).items() if k != NO_PROPAGATE_KEY}
    if not linked:
        meta[NO_PROPAGATE_KEY] = True
    seg.meta = meta or None
    session.add(seg)
    session.commit()
    audit.log(
        actor=(actor or "system"),
        action="segment.propagation_link" if linked else "segment.propagation_unlink",
        target=f"segment {seg.id} (doc {seg.document_id})",
        ip=(ip or ""),
        customer_id=audit.customer_for_project(doc.project_id) if doc is not None else None,
    )
    return True
