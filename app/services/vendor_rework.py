"""The reject/rework transition for the vendor dispatch-and-wait step (PROPOSAL-vendor-
portal-interop R4; Vendor Wave E).

Before Wave E a bad return's only exits were escalate-to-human or cancel. R4 adds
RETURNED -> REJECTED -> re-dispatch to the SAME vendor, carrying STRUCTURED FEEDBACK (which
segment ids failed which guard lanes / LQA findings, plus any operator note). It composes
with the LQA-feedback correction-loop pattern and reuses every existing seam:

  * The rework dispatch is a REAL dispatch: it rides the same package rendition (the JSON
    contract + an XLIFF 1.2 rendition when the vendor has include_xliff), the same HMAC
    signing, and a NEW claim token -- built through vendor_dispatch.build_contract +
    dispatch() exactly like an original dispatch, plus a top-level `rework` marker and
    per-segment `feedback`.
  * The rework RETURN funnels through the SINGLE guarded admission seam
    (vendor_return.admit_return -> commit_mt_output with every guard lane + the K1 source-echo
    law). NON-NEGOTIABLE INVARIANT: there is NO new admission path. This module only SENDS a
    rework and re-parks the run; the return comes back through Wave B's one seam.
  * Rework rounds are CAPPED per leg (the `vendor_rework_max_rounds` setting, default 1).
    When the cap is hit the ONLY path is the existing auto-escalate-to-human (decision 3): the
    run re-parks into a human_review pause and a person owns it -- no further vendor round.
  * Reworked segments keep `vendor` provenance (the return's own provenance path is unchanged);
    the rework ROUND is recorded on the new leg for the spend/envelope surfaces.

Re-parking (the run has already resumed past the external step by the time a delivery is
rejected) is done by orchestrator.repark_run_at_external_step -- it reopens the run PAUSED at
the external step so the rework return lands + resumes through the identical machinery a
first-time dispatch uses.

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.models import (
    Document,
    Project,
    RunStatus,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, vendor_cost, vendor_dispatch, vendor_return, vendors

log = logging.getLogger("loc_tms.vendor_rework")

DEFAULT_REWORK_MAX_ROUNDS = 1


# --- errors -------------------------------------------------------------------
class VendorReworkError(RuntimeError):
    """Base for every reject/rework refusal. Carries a clean, surface-shared message; the
    CLI / /api / MCP callers catch the BASE and map it to a usage/4xx error."""


class ReworkStateError(VendorReworkError):
    """The leg cannot be rejected/reworked: it is not a RETURNED leg, its run/document is
    gone, or its vendor is no longer connected to the customer (fail-closed)."""


# --- config -------------------------------------------------------------------
def rework_cap() -> int:
    """The max rework rounds per leg (the `vendor_rework_max_rounds` install setting; default
    1). Round 0 is the original dispatch; a leg at round == cap can no longer be reworked (the
    next rejection escalates to a human). #632: the key is registered in settings.DEFAULTS and
    every write door refuses anything but 1..VENDOR_REWORK_MAX_ROUNDS_CAP; a stored value
    outside that range (a raw row written around the doors) still reads as the default, so a
    bad value can never make rework unbounded, zero or negative."""
    from app.services import settings

    try:
        v = int(str(settings.get("vendor_rework_max_rounds") or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_REWORK_MAX_ROUNDS
    if 1 <= v <= settings.VENDOR_REWORK_MAX_ROUNDS_CAP:
        return v
    return DEFAULT_REWORK_MAX_ROUNDS


# --- structured feedback ------------------------------------------------------
def build_rework_feedback(
    session: Session, leg: VendorDispatch, operator_feedback, note: str
) -> dict:
    """Assemble the structured feedback the rework dispatch carries. Two sources, merged by
    segment id:

      * AUTO-DERIVED from the guarded admission's persisted verdict -- each segment currently
        NEEDS_REVIEW or carrying qa_flags contributes its flag TYPES as `issues` (refusal,
        lang, tag, glossary, dnt, number, length, ...). This is the "which ids failed which
        guard lanes" detail the return-admission path already produced.
      * OPERATOR-SUPPLIED -- an optional list of `{id, issues?, note?}` (from an LQA step or a
        human reviewer), which augments / overrides the auto-derived note per id.

    Returns `{"note": <overall note>, "segments": [{"id", "issues", "note"}, ...]}` -- the
    shape build_contract embeds as the top-level `rework` marker + per-segment `feedback`."""
    by_id: dict = {}
    # auto-derive from the current segment qa state, RESTRICTED to the segments THIS leg
    # actually dispatched (finding #15): a segment flagged for a reason unrelated to the
    # vendor's delivery (or one the vendor was never sent) must not blame the vendor.
    dispatched = set(vendor_return.dispatched_ids(leg))
    doc = session.get(Document, leg.document_id) if leg.document_id else None
    if doc is not None:
        for seg in orchestrator._load_segments(session, doc.id, leg.target_locale):
            if dispatched and str(seg.id) not in dispatched:
                continue
            issues = []
            for fl in seg.qa_flags or []:
                if isinstance(fl, dict) and fl.get("type"):
                    issues.append(str(fl["type"]))
            from app.services import quality

            flagged = (seg.status == quality.SegStatus.NEEDS_REVIEW) or bool(issues)
            if flagged:
                by_id[str(seg.id)] = {"id": seg.id, "issues": sorted(set(issues)), "note": ""}
    # merge operator-supplied feedback (augments / overrides per id)
    for fb in operator_feedback or []:
        if not isinstance(fb, dict) or fb.get("id") is None:
            continue
        sid = str(fb.get("id")).strip()
        entry = by_id.setdefault(sid, {"id": fb.get("id"), "issues": [], "note": ""})
        extra = [str(x) for x in (fb.get("issues") or []) if str(x).strip()]
        if extra:
            entry["issues"] = sorted(set(entry.get("issues") or []) | set(extra))
        if str(fb.get("note") or "").strip():
            entry["note"] = str(fb.get("note")).strip()
    return {"note": str(note or "").strip(), "segments": [by_id[k] for k in sorted(by_id)]}


# --- the informing assets an original dispatch attaches (finding #6 / R4) ----
def _dispatch_assets(
    session: Session,
    run: WorkflowRun,
    doc: Document,
    project: Project | None,
    src_locale: str,
    target_locale: str,
):
    """Re-derive the informing assets a SEGMENTED dispatch attaches (orchestrator ~3326-3365),
    so a rework ships the SAME context -- a rework must not blame the glossary lane then omit
    the glossary. Returns (glossary, style_guide).

    R4 (round 2): MATCH THE ORIGINAL EXACTLY. A segmented dispatch sends glossary + style_guide
    ONLY (orchestrator.py:2065-2067) -- NOT a context asset -- so this does too. And the term
    SET must match: the glossary honors the workflow's PINNED glossary
    (`_resolved_glossary_id`) and the context package's disambiguation SENSE HINT (the context
    package is resolved only for that hint; the context prefix itself is not sent). FULLY
    GUARDED: any resolution failure degrades to an empty asset (the rework still dispatches)."""
    glossary, style_guide = [], ""
    if project is None:
        return glossary, style_guide
    # The disambiguation sense hint from the run's context package (used for glossary sense
    # selection exactly as the original dispatch does); the context asset itself is NOT sent.
    sense = ""
    try:
        from app.services import context_pkg

        eff, _ = context_pkg.resolve(
            session, document=doc, project=project, org_id=getattr(project, "org_id", None)
        )
        sense = context_pkg.sense_hint(eff)
    except Exception:
        log.warning("rework: context sense-hint resolution failed", exc_info=True)
    try:
        from app.services import glossary_module as _gm

        steps = orchestrator.get_steps(session, run.workflow_id)
        gid = orchestrator._resolved_glossary_id(
            session,
            steps,
            src_locale,
            target_locale,
            getattr(project, "content_type_id", None),
            None,
        )
        segs = orchestrator._load_segments(session, doc.id, target_locale)
        plan = _gm.build_glossary_plan(
            session, project, src_locale, target_locale, pinned_glossary_id=gid
        )
        conflicts: set = set()
        seg_terms = [
            _gm.match_terms_merged(
                session,
                doc.project_id,
                s.source_text or "",
                src_locale,
                target_locale,
                plan,
                conflict_state=conflicts,
                disambig=sense,
            )
            for s in segs
        ]
        glossary = orchestrator._union_seg_terms(seg_terms)
    except Exception:
        log.warning("rework: glossary asset resolution failed", exc_info=True)
    try:
        from app.services import formality, style_guides

        # #617 item 7: the original dispatch's style_guide carries the resolved MT formality,
        # so the rework ships the same sentence.
        _fm = formality.resolve_for(
            session, project=project, document=doc, target_locale=target_locale
        )
        guide = style_guides.resolve_style_guide(
            session,
            org_id=getattr(project, "org_id", None),
            customer_id=getattr(project, "customer_id", None),
            content_type_id=getattr(project, "content_type_id", None),
            source_locale=src_locale,
            target_locale=target_locale,
        )
        style_guide = style_guides.render_style_guide(guide, _fm) or ""
    except Exception:
        log.warning("rework: style-guide asset resolution failed", exc_info=True)
    return glossary, style_guide


# --- the rework re-dispatch (a real dispatch) --------------------------------
def _dispatch_rework(
    session: Session,
    *,
    vendor: Vendor,
    run: WorkflowRun,
    step: WorkflowStep,
    doc: Document,
    project: Project | None,
    src_locale: str,
    target_locale: str,
    feedback: dict,
    rework_round: int,
    rework_of: int,
    orig_leg: VendorDispatch,
    org_id=None,
    transport=None,
) -> VendorDispatch:
    """Send the rework to the vendor and record the new leg. Reuses the SAME primitives an
    original dispatch does -- egress perimeter, the informing assets (glossary/style/context),
    build_contract (incl. XLIFF when include_xliff), dispatch() (SSRF + HMAC signing),
    record_dispatch -- with a NEW claim token, the `rework` marker + per-segment feedback, and
    the rework lineage (round + rework_of). A quote-gated vendor re-enters the quote gate on
    rework too (the leg is recorded QUOTED). Raises ReworkStateError on ANY delivery/policy
    failure (finding #5: the mock-egress perimeter and every transport error convert to a
    clean ReworkStateError the surfaces map to a 4xx, never a 500).

    F3: a rework re-dispatch of a `manual` leg must follow the SAME manual contract as the
    first dispatch -- keyed off the ORIGINAL leg's protocol SNAPSHOT (`orig_leg`), NOT the
    vendor's live protocol (which may have been flipped since). A wireless (manual) transport
    SKIPS the egress perimeter (same WHY as first dispatch: no endpoint / no wire to leak
    deterministic offline text to), records an EMPTY endpoint_host, and lands DISPATCHED
    without any wire I/O; the new leg snapshots the original's protocol so the lineage stays
    coherent even if the vendor is flipped again."""
    from app.services.connectors.base import ConnectorError
    from app.services.connectors.egress_guard import MockEgressBlocked

    # F3/F5: resolve the transport implementation ONCE off the ORIGINAL leg's protocol snapshot.
    proto = vendor_dispatch.leg_protocol(orig_leg, vendor)
    impl = vendor_dispatch.transport_for_protocol(proto)
    # Finding #5: the egress perimeter is INSIDE the guarded path now. MockEgressBlocked (the
    # DEFAULT offline posture when a real endpoint is configured) would otherwise escape as a
    # 500 on api/web/CLI. F3: SKIP it for a wireless (manual) rework -- exactly as the first
    # manual dispatch does -- since nothing is put on the wire (a mock provider in an
    # egress_is_real posture must NOT refuse a manual rework, while a native rework still does).
    if not getattr(impl, "wireless", False):
        try:
            vendor_dispatch.assert_dispatch_egress_allowed(
                session, project, target_locale, vendor_dispatch._effective_transport(transport)
            )
        except MockEgressBlocked as exc:
            raise ReworkStateError(
                f"rework dispatch to vendor {vendor.id} refused: "
                f"{vendor_dispatch.sanitize_error(exc)}"
            ) from exc
    claim_token = vendor_dispatch.new_claim_token()
    seg_rows = orchestrator._load_segments(session, doc.id, target_locale)
    segments = [
        {
            "id": s.id,
            "source": s.source_text or "",
            "target": s.target_text or "",
            "tm_match_pct": s.tm_match_pct,
            "lock_band": s.lock_band,
        }
        for s in seg_rows
    ]
    if not segments:
        raise ReworkStateError(
            f"document {doc.id} has no segments in {target_locale}; nothing to rework."
        )
    estimate = vendor_cost.estimate_from_segments(segments, vendors.rate_card_for(vendor))
    glossary, style_guide = _dispatch_assets(session, run, doc, project, src_locale, target_locale)
    rework_block = {
        "round": rework_round,
        "note": (feedback or {}).get("note") or "",
        "prior_run_id": run.id if run else None,
        "prior_dispatch_id": rework_of,
        "segments": (feedback or {}).get("segments") or [],
    }
    # W3-E gate H2: a rework re-dispatch must carry the buyer query conversation too -- else
    # every rework round loses it, falsifying §7.1's own rationale for a sibling `queries` key
    # (a conversation must survive the next rework). Same guarded degradation + kill switch as
    # the first dispatch; seg ids are the int Segment ids collect_outbound_queries keys on.
    queries_by_segment = None
    if vendor_dispatch._queries_enabled():
        try:
            queries_by_segment = vendor_dispatch.collect_outbound_queries(
                session, [s.get("id") for s in segments], target_locale
            )
        except Exception:
            queries_by_segment = None
    contract = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=segments,
        source_locale=src_locale,
        target_locale=target_locale,
        claim_token=claim_token,
        vendor=vendor,
        glossary=glossary,
        style_guide=style_guide,
        rework=rework_block,
        queries_by_segment=queries_by_segment,
    )
    dispatched_ids = [s.get("id") for s in contract.get("segments") or []]
    omitted_ids = (
        [s.get("id") for s in contract.get("segments") or [] if s.get("locked")]
        if vendors.omit_locked_targets(vendor)
        else []
    )
    # F3: a wireless (manual) rework records NO host and puts nothing on the wire, exactly like
    # a first manual dispatch; a native rework records its endpoint host and rides the wire.
    host = (
        ""
        if getattr(impl, "wireless", False)
        else vendor_dispatch._endpoint_host(vendor.endpoint_url)
    )
    try:
        vendor_dispatch.dispatch(vendor, contract, transport=transport, impl=impl)
    except (
        ConnectorError,
        MockEgressBlocked,
        vendor_dispatch.VendorDispatchError,
        vendor_dispatch.VendorPolicyRefusal,
    ) as exc:
        # A rework delivery failure is surfaced to the operator to retry (rework is an
        # operator-driven recovery, not an automatic pipeline step) -- never a phantom leg.
        raise ReworkStateError(
            f"rework dispatch to vendor {vendor.id} failed: {vendor_dispatch.sanitize_error(exc)}"
        ) from exc
    requires_quote = vendors.requires_quote_approval(vendor)
    status = VendorDispatchStatus.QUOTED if requires_quote else VendorDispatchStatus.DISPATCHED
    new_leg = vendor_dispatch.record_dispatch(
        session,
        vendor=vendor,
        run=run,
        step=step,
        document_id=doc.id,
        target_locale=target_locale,
        claim_token=claim_token,
        endpoint_host=host,
        estimate=estimate,
        segment_count=len(segments),
        word_count=int(estimate.get("total_words") or 0),
        expires_at=vendor_dispatch.expiry_for(step),
        org_id=org_id,
        customer_id=getattr(project, "customer_id", None),
        dispatched_ids=dispatched_ids,
        omitted_ids=omitted_ids,
        status=status,
        requires_quote=requires_quote,
        rework_round=rework_round,
        rework_of=rework_of,
        rework_feedback=feedback,
        # F3: the new rework leg inherits the ORIGINAL leg's protocol snapshot (not the vendor's
        # possibly-flipped current one), so the whole rework lineage stays coherent.
        protocol=impl.name,
        # A wire-less (manual) rework is an operator-delivered package exactly like a first
        # manual dispatch, so FREEZE the just-built rework contract (rework marker + per-segment
        # feedback + assets, minus the duplicated assets.xliff) for the download to render
        # verbatim; a native rework leaves it NULL. See vendor_dispatch._frozen_package.
        retry_contract=(
            vendor_dispatch._frozen_package(contract) if getattr(impl, "wireless", False) else None
        ),
    )
    return new_leg


# --- the entry point ----------------------------------------------------------
def reject_and_rework(
    session: Session,
    leg: VendorDispatch,
    *,
    feedback=None,
    note: str = "",
    org_id=None,
    actor: str = "system",
    transport=None,
) -> dict:
    """Reject a RETURNED leg's delivery and re-dispatch a rework to the SAME vendor with
    structured feedback (RETURNED -> REJECTED -> re-dispatch). Caps rework rounds (default 1);
    at the cap it escalates to a human instead. Raises ReworkStateError. Returns a summary.

    `feedback` is an optional list of `{id, issues?, note?}` (from an LQA step or reviewer);
    `note` is an overall rework note. `org_id`, when given, scopes the leg to the caller."""
    if org_id is not None and getattr(leg, "org_id", None) != org_id:
        raise ReworkStateError(f"no such dispatch: {leg.id}")
    if (leg.status or "") != VendorDispatchStatus.RETURNED:
        raise ReworkStateError(
            f"dispatch {leg.id} is {leg.status!r}, not RETURNED; only a delivered return can "
            "be rejected and reworked."
        )
    run = session.get(WorkflowRun, leg.workflow_run_id)
    if run is None:
        raise ReworkStateError(f"dispatch {leg.id}: its run no longer exists.")
    # Compose with run-cancel: a rework re-parks the run (reopening a COMPLETED run is the
    # normal case), but a CANCELLED run was DELIBERATELY killed by an operator -- reworking it
    # would resurrect it PAUSED. Refuse, so rework never drags a cancelled run back to life.
    if run.status == RunStatus.CANCELLED:
        raise ReworkStateError(
            f"dispatch {leg.id}: run {run.id} was CANCELLED; a cancelled run cannot be "
            "reworked (rework never resurrects a run an operator killed)."
        )
    doc = session.get(Document, leg.document_id) if leg.document_id else None
    if doc is None:
        raise ReworkStateError(f"dispatch {leg.id}: its document no longer exists.")
    step = session.get(WorkflowStep, leg.step_id) if leg.step_id else None
    if step is None:
        raise ReworkStateError(
            f"dispatch {leg.id}: its workflow step no longer exists; cannot re-park."
        )
    project = session.get(Project, doc.project_id) if doc.project_id else None
    locale = leg.target_locale
    cap = rework_cap()
    next_round = int(leg.rework_round or 0) + 1

    # Finding #11: validate the run is RE-PARKABLE at this leg's step BEFORE any mutation, so a
    # refusal leaves the leg UNTOUCHED (the old code discarded repark's bool -- on failure the
    # leg was already REJECTED + a rework dispatched but the run never parked, so the return
    # could never admit).
    ok, why = _repark_precheck(session, run, step)
    if not ok:
        raise ReworkStateError(f"dispatch {leg.id}: {why}")

    # Finding #3: EXACTLY-ONCE. Flip RETURNED -> REJECTED with a conditional compare-and-set
    # so two concurrent rejects cannot BOTH proceed to dispatch a rework (a network call sits
    # between the read and the write). The loser matches 0 rows and is dropped.
    from sqlalchemy import update as _sa_update

    res = session.execute(
        _sa_update(VendorDispatch)
        .where(VendorDispatch.id == leg.id, VendorDispatch.status == VendorDispatchStatus.RETURNED)
        .values(status=VendorDispatchStatus.REJECTED)
    )
    if res.rowcount != 1:
        session.rollback()
        raise ReworkStateError(
            f"dispatch {leg.id} was already rejected by a concurrent request; this one is "
            "dropped (the RETURNED->REJECTED transition is exactly-once)."
        )
    leg.status = VendorDispatchStatus.REJECTED
    # A REJECTED leg has no delivery pending (it is superseded by the rework leg) -> clear any
    # held package. A wire-less leg being reworked already cleared it on RETURNED, so this is
    # belt-and-braces; single-sourced lifecycle (vendor_dispatch._clear_or_keep_frozen).
    vendor_dispatch._clear_or_keep_frozen(leg, VendorDispatchStatus.REJECTED)
    session.commit()

    # CAP HIT (decision 3): no further vendor round -- re-park + escalate to a human_review
    # pause (finding #7: write the escalation note like every sibling, and REJECTED now
    # renders a pause reason via vendor_pause_state). No feedback is assembled here (finding
    # #15: the scan is wasted when nothing is dispatched).
    if next_round > cap:
        if not orchestrator.repark_run_at_external_step(session, run, step.id, locale):
            raise ReworkStateError(
                f"dispatch {leg.id}: could not re-park the run at its external step."
            )
        _escalate_note_and_notify(
            session,
            run,
            step,
            note=(
                f"vendor rework cap ({cap}) reached for dispatch {leg.id} ({locale}); "
                "escalated to human review (no further vendor round)"
            ),
        )
        from app.services import audit

        audit.log(
            actor=actor or "system",
            action="vendor.rework_cap_escalated",
            target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
            detail=f"rework cap {cap} reached at round {leg.rework_round}; escalated to "
            "human review (no further vendor round)",
            customer_id=leg.customer_id,
        )
        return {
            "ok": True,
            "dispatch_id": leg.id,
            "run_id": run.id,
            "reworked": False,
            "escalated": True,
            "rework_round": leg.rework_round,
            "cap": cap,
        }

    # Fail-closed vendor resolution, exactly like an original dispatch. On any refusal below,
    # ROLL the leg back to RETURNED so the operator can retry (nothing was dispatched yet).
    vendor = (
        vendors.vendor_for_customer(session, leg.vendor_id, leg.customer_id, org_id=None)
        if leg.vendor_id
        else None
    )
    if vendor is None:
        _rollback_to_returned(session, leg)
        raise ReworkStateError(
            f"dispatch {leg.id}: vendor {leg.vendor_id} is no longer connected to this "
            "customer (or is archived); rework refused (fail-closed)."
        )

    fb = build_rework_feedback(session, leg, feedback, note)
    try:
        new_leg = _dispatch_rework(
            session,
            vendor=vendor,
            run=run,
            step=step,
            doc=doc,
            project=project,
            src_locale=getattr(project, "source_locale", "") or "",
            target_locale=locale,
            feedback=fb,
            rework_round=next_round,
            rework_of=leg.id,
            orig_leg=leg,
            org_id=leg.org_id,
            transport=transport,
        )
    except BaseException:
        # R2 (round 2): the CAS already committed leg -> REJECTED, so ANY failure after it
        # (not only ReworkStateError -- e.g. a RuntimeError from record_dispatch, a
        # KeyboardInterrupt) must roll the leg back to RETURNED, or the leg is stranded
        # REJECTED with no rework leg and the run never re-parked (unretryable). Best-effort
        # rollback, then re-raise the original.
        try:
            _rollback_to_returned(session, leg)
        except Exception:
            log.exception("rework: rollback to RETURNED failed for dispatch %s", leg.id)
        raise

    # Reopen the run PAUSED at the external step so the rework return lands + resumes.
    if not orchestrator.repark_run_at_external_step(session, run, step.id, locale):
        raise ReworkStateError(
            f"dispatch {leg.id}: could not re-park the run at its external step."
        )

    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.rework_dispatched",
        target=f"dispatch {new_leg.id} (vendor {new_leg.vendor_id})",
        detail=f"rework round {next_round} of dispatch {leg.id}; "
        f"{len(fb.get('segments') or [])} segment(s) with feedback",
        customer_id=leg.customer_id,
    )
    return {
        "ok": True,
        "dispatch_id": leg.id,
        "run_id": run.id,
        "reworked": True,
        "escalated": False,
        "new_dispatch_id": new_leg.id,
        "rework_round": next_round,
        "cap": cap,
        "feedback_segments": len(fb.get("segments") or []),
    }


def _repark_precheck(session: Session, run: WorkflowRun, step: WorkflowStep):
    """Finding #11: is the run re-parkable at `step`? Returns (ok, reason). The step must be
    in the run's workflow, and if the run is CURRENTLY paused it must be paused at THIS step
    (H4 step-isolation) -- re-parking a run paused at a LATER step (a human_review) would
    silently abandon that pause."""
    steps = orchestrator.get_steps(session, run.workflow_id)
    if not any(s.id == step.id for s in steps):
        return False, "its workflow step is not in the run's workflow; cannot re-park"
    if run.status == RunStatus.PAUSED:
        sr = orchestrator._latest_paused_steprun(session, run.id)
        if sr is not None and sr.step_id != step.id:
            return False, (
                "the run is currently paused at a different step; reworking would "
                "clobber that pause -- resolve it first"
            )
    return True, ""


def _rollback_to_returned(session: Session, leg: VendorDispatch) -> None:
    """Un-reject a leg back to RETURNED after a rework dispatch was refused (fail-closed
    vendor / delivery failure), so nothing is left half-done and the operator can retry."""
    leg.status = VendorDispatchStatus.RETURNED
    # RETURNED has no pending delivery -> clear any held package. This leg was just REJECTED (which
    # already cleared it), and a wire-less leg cleared it on the original RETURNED flip, so this is
    # a no-op belt-and-braces -- single-sourced lifecycle (vendor_dispatch._clear_or_keep_frozen).
    vendor_dispatch._clear_or_keep_frozen(leg, VendorDispatchStatus.RETURNED)
    session.add(leg)
    session.commit()


def _escalate_note_and_notify(
    session: Session, run: WorkflowRun, step: WorkflowStep, *, note: str
) -> None:
    """Write the escalation note on the run (APPEND, never clobber a sibling's note) and fire
    the human-review notification -- the same shape vendor_dispatch._escalate_run_to_human
    uses, so the cap-escalation pause reads like every other escalation."""
    run.notes = f"{run.notes}; {note}" if (run.notes or "").strip() else note
    session.add(run)
    session.commit()
    _notify_human(session, run, step)


def _notify_human(session: Session, run: WorkflowRun, step: WorkflowStep) -> None:
    """Best-effort human-review notification for the cap-escalation path (a mail failure
    never breaks the escalation)."""
    try:
        from app.services import notifications

        notifications.on_human_review_paused(run, step)
    except Exception:
        log.warning("rework cap escalation notification failed for run %s", run.id, exc_info=True)


__all__ = [
    "DEFAULT_REWORK_MAX_ROUNDS",
    "ReworkStateError",
    "VendorReworkError",
    "build_rework_feedback",
    "reject_and_rework",
    "rework_cap",
]
