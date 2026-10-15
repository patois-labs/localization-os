"""orchestrator: part9

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import uuid
from datetime import datetime

from sqlmodel import Session

from app import config
from app.models import (
    Customer,
    Document,
    LQAResult,
    Project,
    RunStatus,
    StepRun,
    StepType,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    lqa,
    lqa_attribution,
    pii,
    pii_boundary,
    tap_envelope,
)
from app.services.orchestrator_part2 import (
    _IN_FLIGHT_RUN_STATUSES,
    _commit_run_terminal_direct,
    _notify_human_review_paused,
    _spec_for_step,
    _StepDirective,
)
from app.services.orchestrator_part3 import (
    _lqa_event_payload,
    _lqa_scope_note,
    _lqa_step_error,
    _prior_lqa_verdicts,
    _remember_lqa_scope,
    _skip_empty_lqa,
)
from app.services.orchestrator_part4 import _abort_if_superseded, _spend_block_reason
from app.services.orchestrator_part5 import _branch_cursor, _record_gate_hold
from app.services.orchestrator_part6 import _next_cursor, _progress_lock, _progress_state
from app.services.orchestrator_part8 import _flush_progress
from app.services.orchestrator_terminology_backen import (
    GATE_HOLD_JUDGE_UNAVAILABLE,
    GATE_HOLD_NO_PROGRESS,
    _end_run_cursor,
    _lqa_judge_unavailable,
    _lqa_no_progress,
    _lqa_no_progress_error,
    _lqa_shape_kwargs,
)
from app.services.providers import (
    DeadEngineError,
)

log = logging.getLogger(__name__)


def _claim_pause(session: Session, run: WorkflowRun) -> bool:
    """Issue #492 (gate): park the run PAUSED as a compare-and-set, never a blind write.

    The pre-step probe reads committed state, but an operator cancel can commit in the
    window between that probe and this thread's PAUSED commit; a plain ORM write would then
    flip the CANCELLED row back to PAUSED. The conditional UPDATE only moves a row that is
    still in flight, in the same transaction as the PAUSED StepRun, so a cancel that won the
    race keeps its verdict (False: the caller rolls back and stops). A cancel that arrives
    after this UPDATE waits for the commit and then cancels the PAUSED run normally."""
    from sqlalchemy import update as sa_update

    claimed = session.execute(
        sa_update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.status.in_(tuple(_IN_FLIGHT_RUN_STATUSES)),
        )
        .values(status=RunStatus.PAUSED)
    )
    return bool(claimed.rowcount)


def _execute_step(
    session: Session,
    run: WorkflowRun,
    steps,
    step: WorkflowStep,
    cursor: int,
    *,
    guard,
    key_index,
    input_blob: str,
    lqa_src: str,
    lqa_terms,
    sg_render: str,
    workflow_id: int,
    doc_id: int,
    env_ids,
    src: str,
    locale: str,
    step_started_at,
    pause_context_fn,
    dispatch_payload_fn=None,
    post_dispatch_persist_fn=None,
    external_glossary=None,
    lqa_scope=None,
):
    """The shared per-step dispatch seam — the ONE place both run loops (FULL_TEXT and
    SEGMENTED) route the orchestration-only step types through, so a new such step type
    (e.g. the upcoming `external` dispatch-and-wait, PROPOSAL-external-processing-step
    §8 Wave A) is added HERE, not in two hand-synced ~1,500-LOC twins.

    It owns the step bodies that were byte-for-byte identical across the two loops
    modulo the working text they carry:
      * HUMAN_REVIEW — the pause/resume leg (persist the resume-safe context, write the
        PAUSED StepRun, notify, emit `paused`, stop);
      * EXTERNAL     — the dispatch-and-wait leg (PROPOSAL-external-processing-step Wave
        A): package the current translation state via `dispatch_payload_fn`, compute +
        persist the per-vendor scoping estimate, send the signed JSON contract outbound
        through the SSRF guard, persist a claim token, and PARK PAUSED reusing the
        human_review pause machinery (a dispatch failure FAILS the step instead);
      * LQA          — the structured judge (score, persist findings + LQAResults +
        StepRun, route by gate);
      * the llm_agent SPEND GATE — fail fast when the whole provider chain is over cap.

    It NEVER touches the segment `working[]` array or the full-text doc body, and it
    never runs the llm_agent fan-out or the segmented tm_match fill — those genuinely
    differ by mode and stay in each loop, reached via a `fallthrough` directive.

    BYTE-IDENTICAL CONTRACT: every StepRun, LQAResult, run.context write, SSE event,
    commit and early-return below is a verbatim move of the twin bodies. The only
    parameterized differences are `input_blob` (the mode's assembled working text used
    for the pause blob + StepRun input/output + the judge's target), `lqa_src` (the
    source blob the judge scores), `lqa_terms` (the glossary handed to the LQA judge —
    under the T3 toggle the producing step's shown-set union, else the full match),
    `external_glossary` (the glossary handed to the EXTERNAL/human-vendor dispatch — always
    the FULL matched set, never the `llm`-gated `lqa_terms`, M6; defaults to `lqa_terms`),
    `pause_context_fn` (a closure returning the mode's resume-safe pause payload
    dict, or None to leave run.context untouched) and `dispatch_payload_fn` (a closure
    returning the mode's per-segment contract units for the EXTERNAL branch -- FULL_TEXT
    hands one whole-body unit, SEGMENTED one unit per Segment with its TM band).
    `post_dispatch_persist_fn` (P-A1) is called ONCE on a SUCCESSFUL external dispatch,
    AFTER the leg is recorded and BEFORE the run parks -- the SEGMENTED loop uses it to
    durably persist this run's in-run tm_match HARD locks (+ their TM fill) onto the
    Segment rows, so a vendor return can never overwrite a hard-locked segment while the
    run is parked (FULL_TEXT has no tm_match, so it passes None -> inert)."""
    # Issue #492: an operator CANCEL (or a sweep) that landed while the previous step ran has
    # already made this run's row terminal. The pausing branches below write PAUSED and the
    # external branch sends a signed contract to a real vendor; neither may happen for a run
    # that is no longer ours. Checked on COMMITTED state before every step (not only at the
    # final write-back) and again after the external dispatch.
    if _abort_if_superseded(session, run):
        session.rollback()
        yield {
            "type": "error",
            "run_id": run.id,
            "error": "This run was cancelled or superseded while it was executing; "
            "its remaining steps were not run.",
        }
        return _StepDirective(stop=True)

    if step.step_type == StepType.HUMAN_REVIEW:
        # Issue #492 (gate): park PAUSED as a compare-and-set FIRST, so a cancel that committed
        # after the probe above keeps its verdict and nothing below is written.
        if not _claim_pause(session, run):
            session.rollback()
            yield {
                "type": "error",
                "run_id": run.id,
                "error": "This run was cancelled or superseded while it was executing; "
                "its remaining steps were not run.",
            }
            return _StepDirective(stop=True)
        run.current_step_index = cursor
        # RESUME-SAFE write-through: the mode-specific payload (FULL_TEXT's ft_lang_guard;
        # SEGMENTED's seg_lang_guard/seg_step_error/tm_lock_plan/tm_origin bundle). The
        # closure preserves each loop's NULL contract (None -> run.context untouched).
        _new_ctx = pause_context_fn()
        if _new_ctx is not None:
            run.context = _new_ctx
        session.add(run)
        session.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.PAUSED,
                input_text=input_blob,
                output_text="",
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )
        )
        session.commit()
        # Feature B: notify the assignee ONCE per actual pause — right AFTER the PAUSED
        # StepRun is persisted (observe_run replay creates no StepRun, so it never
        # re-fires). Guarded so a mail failure can NEVER crash/alter the run.
        _notify_human_review_paused(run, step)
        yield {
            "type": "paused",
            "run_id": run.id,
            "step_index": cursor,
            "step_name": step.name or "Human review",
            "text": input_blob,
        }
        return _StepDirective(stop=True)

    if step.step_type == StepType.EXTERNAL:
        # PROPOSAL-external-processing-step Wave A: dispatch-and-wait. Package the current
        # translation state (the mode-specific `dispatch_payload_fn` closure -> the §3.2
        # JSON contract's segments), compute + persist the per-vendor SCOPING ESTIMATE,
        # send the signed contract OUTBOUND through the EXISTING SSRF guard to the step's
        # vendor (fail-closed against the document's customer's linked vendors), persist a
        # claim token, and PARK the leg PAUSED reusing the human_review pause machinery
        # (decision 3: expiry ALWAYS escalates to a human_review pause, never hard-fails --
        # `vendor_dispatch.sweep_expired` does that later; the leg is parked identically to
        # a human_review pause so that escalation is a status flip, not a new pause). ONE
        # branch, both loops (the seam's whole point). A dispatch FAILURE (no vendor, SSRF
        # refusal, non-2xx) FAILS the step and NEVER parks a phantom leg.
        from app.services import vendor_dispatch
        from app.services import vendors as _vendors
        from app.services.connectors.base import ConnectorError
        from app.services.connectors.egress_guard import MockEgressBlocked

        _doc = session.get(Document, doc_id)
        _proj = session.get(Project, _doc.project_id) if _doc is not None else None
        _customer_id = getattr(_proj, "customer_id", None)
        # L7: Project has no org_id column -- derive the org from the CUSTOMER (the
        # boundary the vendor link actually enforces), never from the inert _proj.org_id.
        _org_id = None
        if _customer_id is not None:
            _cust = session.get(Customer, _customer_id)
            _org_id = getattr(_cust, "org_id", None)
        _payload = list(dispatch_payload_fn() if dispatch_payload_fn else [])
        if not _payload:
            # F6 empty-payload (adversarial re-review, LOW): every dispatchable unit was excluded
            # upstream (e.g. every segment is human-approved/edited or locked, so the segmented
            # payload builder dropped them all), leaving nothing to send. Dispatching an EMPTY
            # contract would PARK the run until sweep_expired escalates it to a human -- a dead
            # wait for no reason, and a phantom leg to reconcile. Record the step as a completed
            # no-op and ADVANCE (mirrors the run_condition SKIP shape): no leg, no spend, no park.
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.COMPLETED,
                    input_text=input_blob,
                    output_text=input_blob,
                    error="external step skipped: no dispatchable units (all protected/locked)",
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or "External",
                "step_type": step.step_type,
                "output": input_blob,
            }
            return _StepDirective(next_cursor=cursor + 1)
        try:
            # Wave C (interop R5): a TRANSIENT transport failure NO LONGER raises here --
            # dispatch_external_step parks a RETRY_WAIT leg and returns it (the run pauses
            # exactly like a delivered dispatch; scheduler.retry_due_legs re-attempts it,
            # dead-lettering to a human on exhaustion). Only POLICY refusals -- no vendor /
            # invalid config (VendorError), SSRF-guard / bad-scheme refusal (ConnectorError),
            # mock-egress block (MockEgressBlocked) -- still FAIL the step, deterministically,
            # exactly as before.
            _leg = vendor_dispatch.dispatch_external_step(
                session,
                run=run,
                step=step,
                document_id=doc_id,
                segments=_payload,
                source_locale=src,
                target_locale=locale,
                customer_id=_customer_id,
                org_id=_org_id,
                project=_proj,
                # Terminology T3 (LAW-TERM-4/-15, M6): the human vendor package gets the
                # FULL matched glossary (`external_glossary`), NEVER the `llm`-audience-gated,
                # concept-capped `lqa_terms` subset the LQA judge reads — a `human`-channel
                # consumer must not be handed an `llm`-channel artifact (it would drop exactly
                # the `audience=human` terms LAW-TERM-4 reserves for it and cap the termbase).
                # Falls back to `lqa_terms` only when a caller passes none (byte-identical to
                # the pre-split behavior — both were `terms`/`_lqa_terms` on main).
                glossary=(external_glossary if external_glossary is not None else lqa_terms),
                style_guide=sg_render,
            )
        except (_vendors.VendorError, ConnectorError, MockEgressBlocked) as exc:
            err = f"step '{step.name or 'External'}' ({src}->{locale}): {exc}"
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.FAILED,
                    input_text=input_blob,
                    output_text="",
                    error=err,
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            _commit_run_terminal_direct(session, run, RunStatus.FAILED)
            yield {"type": "error", "run_id": run.id, "error": err}
            return _StepDirective(stop=True)
        # P-A1 (HIGH, data integrity): the leg is now recorded and the run is about to
        # PARK awaiting the vendor return -- which is an external WRITER of these very
        # segments. tm_match's locks normally live in run-working-state and reach the
        # Segment rows only at the terminal write-back, so at this pause the rows are
        # empty/unlocked and a vendor return would overwrite even a 100%/HARD-locked
        # segment, bypassing the "a hard lock outranks a vendor return" guarantee (the
        # guard fires correctly ONCE the lock is on the row -- proven by a lock persisted
        # by a PRIOR completed run). So here, on a successful dispatch and BEFORE the run
        # parks, durably persist this run's in-run tm_match HARD locks (+ their TM fill)
        # so vendor_return's existing is_hard_locked seam refuses the overwrite. Inert on
        # any run with no tm_match hard-lock plan (and on FULL_TEXT, which passes None).
        if post_dispatch_persist_fn is not None:
            post_dispatch_persist_fn()
        if _abort_if_superseded(session, run) or not _claim_pause(session, run):
            # Issue #492: the run was cancelled DURING the dispatch. The leg now exists and
            # cancel_run's leg-close ran before it did: close it here, never park a
            # cancelled run PAUSED. The park itself is a compare-and-set (`_claim_pause`),
            # so a cancel landing after the probe is caught too.
            session.rollback()
            try:
                vendor_dispatch.close_open_legs_for_run(session, run.id)
            except Exception:
                log.exception("closing the legs of cancelled run %s failed", run.id)
            return _StepDirective(stop=True)
        # Dispatched -> park PAUSED exactly like a human_review pause (same write-through
        # of the resume-safe context, same PAUSED StepRun + notify + `paused` event). The
        # SSE event carries the dispatch envelope (host only, estimate, counts -- NEVER the
        # signing secret or the claim token, which stay in the VendorDispatch row).
        # The status was parked PAUSED by `_claim_pause` above.
        run.current_step_index = cursor
        _new_ctx = pause_context_fn()
        if _new_ctx is not None:
            run.context = _new_ctx
        session.add(run)
        session.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.PAUSED,
                input_text=input_blob,
                output_text="",
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )
        )
        session.commit()
        _notify_human_review_paused(run, step)
        yield {
            "type": "paused",
            "run_id": run.id,
            "step_index": cursor,
            "step_name": step.name or "External",
            "text": input_blob,
            # L5: a DISTINCT paused reason so a UI never renders this as a "Human
            # review" pause -- a reviewer resuming it and advancing past the vendor with
            # their own text is the risk. The `external` envelope carries host/estimate.
            "paused_reason": "external_dispatch",
            "external": {
                "vendor_id": _leg.vendor_id,
                "endpoint_host": _leg.endpoint_host,
                "segment_count": _leg.segment_count,
                "estimate_cost_usd": _leg.estimate_cost_usd,
                "estimate_currency": _leg.estimate_currency,
                # Wave C: the leg may be parked RETRY_WAIT (a transient
                # transport failure) rather than DISPATCHED -- surface the
                # status + backoff so a UI shows "retrying" not "sent".
                "status": _leg.status,
                "attempt_count": _leg.attempt_count,
                "next_attempt_at": (
                    _leg.next_attempt_at.isoformat() if _leg.next_attempt_at else None
                ),
                "expires_at": (_leg.expires_at.isoformat() if _leg.expires_at else None),
            },
        }
        return _StepDirective(stop=True)

    if step.step_type == StepType.LQA:
        if lqa_scope is not None and not lqa_scope["judged"]:
            event = _skip_empty_lqa(session, run, step, cursor, guard, step_started_at, lqa_scope)
            # No verdict: nothing failed, so route on_pass without storing a pass.
            next_cur, _ = _branch_cursor(
                step, cursor, gate="passed", key_index=key_index, steps=steps, guard=guard
            )
            run.current_step_index = next_cur
            session.add(run)
            session.commit()
            yield event
            return _StepDirective(next_cursor=next_cur, lqa_findings_block="")
        # ISSUE #95 -- NO-PROGRESS GUARD, before any judge call. A loop-back that returns
        # to this gate with BYTE-IDENTICAL text is not re-judged: the previous verdict is
        # carried forward verbatim and, when it was a FAIL, the run HOLDS here rather than
        # falling through to a downstream (e.g. deliver) step. See `_lqa_no_progress` for
        # why re-judging identical bytes is the defect and not merely wasteful. INERT on a
        # first visit (no prior verdict row) -> a linear run is byte-identical.
        _prior_lqa = _lqa_no_progress(
            session, _prior_lqa_verdicts(session, run.id, step.id), input_blob
        )
        if _prior_lqa is not None:
            _remember_lqa_scope(run, step, guard, lqa_scope)
            _np_error = _lqa_scope_note(lqa_scope, _lqa_no_progress_error(_prior_lqa))
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.COMPLETED,
                    input_text=input_blob,
                    output_text=input_blob,
                    error=_np_error,
                    # The CARRIED verdict, unchanged -- never a fresh (luckier) roll.
                    score=_prior_lqa.score,
                    gate=_prior_lqa.gate,
                    # No provider call was made on this visit: provider/attempts/cost stay
                    # NULL/0 (honest -- a carried verdict is not a judging event).
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            # A carried FAIL ends the run HELD (the same honest end #56/F12 gives a judge
            # outage): looping again would re-enter the already-judged-bytes state
            # forever, and falling through would deliver text the gate never passed. The
            # hold is RECORDED on the run so the segment write-back flags the units and
            # the export refuses -- in a classic workflow "hold" has to mean the text does
            # not ship, not merely that later steps do not run (gate C round 1, H1).
            if _prior_lqa.gate == "failed":
                _np_cursor = _end_run_cursor(steps)
                _record_gate_hold(
                    session, run, step=step, step_index=cursor, cause=GATE_HOLD_NO_PROGRESS
                )
            else:
                # A carried PASS: the gate has already passed exactly these bytes, so
                # there is no quality problem -- but the LOOP is over either way (gate C
                # round 1, M4). Following an `on_pass` loop-back would burn full-document
                # provider passes on a repair that provably changes nothing, until some
                # OTHER step hits its ceiling. So route forward: a backward/self jump
                # degrades to `cursor + 1`, exactly the fall-through D4 uses when a
                # loop-back target is at its ceiling. `next`/`end` are unchanged.
                _np_cursor = _next_cursor(
                    step,
                    cursor,
                    gate=_prior_lqa.gate,
                    key_index=key_index,
                    steps=steps,
                    guard=guard,
                )
                if _np_cursor <= cursor:
                    _np_cursor = cursor + 1
            run.current_step_index = _np_cursor
            session.add(run)
            session.commit()
            yield {
                "type": "step",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or "LQA",
                "step_type": step.step_type,
                "provider": None,
                "model": None,
                "chunks": 1,
                "attempts": 0,
                "error": _np_error,
                # The carried verdict, marked as carried. `errors` is deliberately absent:
                # this visit counted no findings, and reporting 0 would be a lie.
                "lqa": {
                    "score": _prior_lqa.score,
                    "gate": ("auto_approved" if _prior_lqa.gate == "passed" else "needs_review"),
                    "no_progress": True,
                },
                "output": input_blob,
            }
            return _StepDirective(next_cursor=_np_cursor)
        rulebook = lqa.load_rulebook(src, locale)
        # PII: LQA must honor the redact choice too — scrub source+target before they
        # reach the scoring engine. The scorecard is not restored text, so redacted
        # input is sufficient (no restore needed) and safe.
        _lqa_src, _lqa_tgt = lqa_src, input_blob
        if step.redact_pii or pii.enabled():
            _lqa_src = pii.redact(_lqa_src, lang=src)[0]
            _lqa_tgt = pii.redact(_lqa_tgt, lang=locale)[0]
        try:
            card = pii_boundary.call_with_redaction(
                step.redact_pii,
                lqa.score_text,
                _lqa_src,
                _lqa_tgt,
                src,
                locale,
                rulebook,
                step.provider or config.DEFAULT_PROVIDER,
                style_guide=sg_render,
                # feat/lqa-configurable-threshold: this step's numeric
                # pass bar (NULL -> the built-in default 90, byte-
                # identical). An lqa step with lqa_threshold=94 gates on
                # 94, driving its on_pass/on_fail branch accordingly.
                lqa_threshold=getattr(step, "lqa_threshold", None),
                # Phases 2+3: the judge sees the SAME run-level glossary
                # the translate/fix steps got (resolved ONCE, org/
                # customer-scoped) — no new matching call, no re-query.
                glossary_terms=lqa_terms,
                # Addendum: the judge honors the STEP's pinned model +
                # fallback chain, not the provider default.
                spec=_spec_for_step(step),
                # Prompt Tap (change 4): correlate the judge call (doc-
                # level card -> no segment_index).
                tap_meta=tap_envelope.stamp_meta(
                    {
                        "workflow_id": workflow_id,
                        "workflow_run_id": run.id,
                        "step_key": step.key,
                        "step_name": step.name or step.step_type,
                        "document_id": doc_id,
                    },
                    *env_ids,
                ),
            )
        except DeadEngineError as exc:
            # Defense-in-depth: score_text soft-degrades a dead engine:<id> ref to a
            # clean (no-LLM) scorecard, but a future regression there must not 500 the
            # run/POST — fail the STEP (naming it), not the request.
            err = f"step '{step.name or 'LQA'}' ({src}->{locale}): {exc}"
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.FAILED,
                    input_text=input_blob,
                    output_text="",
                    error=err,
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            _commit_run_terminal_direct(session, run, RunStatus.FAILED)
            yield {"type": "error", "run_id": run.id, "error": err}
            return _StepDirective(stop=True)
        # Change 1: stash the FRESH LQA card as a rendered findings block so a downstream
        # fix step (this visit's, not a stale earlier one) can consume it. Persist into
        # run.context (review fix 2) so it SURVIVES a human_review pause/resume. Guarded
        # on non-empty (a scored card always renders non-empty; a card-less run leaves
        # run.context untouched).
        _remember_lqa_scope(run, step, guard, lqa_scope)
        # #163: the span map rides WITH the block, written and cleared together, so a
        # resumed run can never pair one visit's block with another visit's spans.
        lqa_findings_block, _findings_spans = lqa.render_findings_with_spans(card)
        if lqa_findings_block:
            run.context = {
                **(run.context or {}),
                "lqa_findings_block": lqa_findings_block,
                "lqa_findings_spans": {str(k): v for k, v in _findings_spans.items()},
            }
        # Wave 2 attribution (§3.1) — same shape as the parallel lane above: one scoring_id
        # per card (segment_id=None workflow granularity), vendor leg resolved by time +
        # locale, dimension/severity canonicalized at persist only.
        _sid = uuid.uuid4().hex
        _vid, _did = lqa_attribution.vendor_for_scored_content(
            session, workflow_run_id=run.id, target_locale=locale, scored_at=datetime.utcnow()
        )
        for e in card["errors"]:
            # Phase 3: an ADJUDICATED finding still persists — downgraded severity +
            # lqa.result_note's audit marker (no migration).
            session.add(
                LQAResult(
                    document_id=doc_id,
                    segment_id=None,
                    dimension=lqa.canon_dimension(e["dimension"]),
                    severity=lqa.canon_severity(e["severity"]),
                    note=lqa.result_note(e),
                    score=card["score"],
                    target_locale=locale,
                    finding_source=e.get("source"),
                    judge=e.get("judge"),
                    scoring_id=_sid,
                    vendor_id=_vid,
                    dispatch_id=_did,
                )
            )
        lqa_gate = "passed" if card["passed"] else "failed"
        session.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.COMPLETED,
                input_text=input_blob,
                output_text=input_blob,
                error=_lqa_scope_note(lqa_scope, _lqa_step_error(card)),
                # #96 JUDGE ATTRIBUTION: the judge's resolved engine/model, its attempts,
                # this visit's elapsed and the call's finish reason / error class -- the
                # same failure SHAPE an llm_agent step stamps. `{}` on an offline/mock card
                # (no live judge) -> byte-identical to before.
                **_lqa_shape_kwargs(
                    card, int((datetime.utcnow() - step_started_at).total_seconds() * 1000)
                ),
                # Phase 1 durable signal (SPEC §3.2): the LQA outcome a Phase 2/3 condition/
                # branch reads. `gate` is the canonical pass/fail; outcome_index is this
                # step's 0-based visit number.
                score=float(card["score"]),
                gate=lqa_gate,
                # Real per-step-visit spend: the judge call's exact ledger cost + tokens.
                cost_usd=card.get("cost_usd") or 0.0,
                tokens_in=card.get("tokens_in") or 0,
                tokens_out=card.get("tokens_out") or 0,
                outcome_index=guard.visit_index(step),
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )
        )
        # SPEC §5.4: an LQA step with a branch routes by its durable gate (Phase 3) —
        # cursor+1 by default when it has none (byte-identical Phase 1/2).
        # #56/F5+F12: a card whose judge was UNAVAILABLE must HOLD — the "failure" is a
        # judge OUTAGE, not a quality defect. It must NOT drive the on_fail fix loop (wasted
        # full-document passes over a phantom) AND must NOT be MORE permissive than a real
        # failing judge: a bare cursor+1 fell through to downstream steps (a DELIVER step
        # RAN on an outage while a healthy fail routed on_fail:end and stopped). So on an
        # outage we TERMINATE the run at this step (len(steps) exits the loop -> the run
        # COMPLETES cleanly, exactly like on_fail:"end"), never falling through. The durable
        # StepRun.gate above still records the honest needs_review + "judge unavailable".
        if _lqa_judge_unavailable(card):
            next_cur = _end_run_cursor(steps)
            # M3 (gate C round 2, ruled): FAIL CLOSED. Ending the run was only half the
            # answer -- the text this run produced was still committed and still exported
            # 200, with no run-level warning at all, so a CONFIGURED gate that could not
            # be evaluated delivered unjudged text more quietly than a gate that failed.
            # A configured-but-unevaluated gate does not deliver: same HELD posture, its
            # own cause and its own sentence ("gate not evaluated: judge unavailable").
            _record_gate_hold(
                session, run, step=step, step_index=cursor, cause=GATE_HOLD_JUDGE_UNAVAILABLE
            )
        else:
            # H2: a refused `on_fail` loop-back from a FAILED gate ends the run HELD, not
            # forward into the downstream steps (see `_branch_cursor`).
            next_cur, _hold_cause = _branch_cursor(
                step, cursor, gate=lqa_gate, key_index=key_index, steps=steps, guard=guard
            )
            if _hold_cause:
                _record_gate_hold(session, run, step=step, step_index=cursor, cause=_hold_cause)
        run.current_step_index = next_cur
        session.add(run)
        session.commit()
        _lqa_shape = (card or {}).get("judge_shape") or {}
        yield {
            "type": "step",
            "run_id": run.id,
            "step_index": cursor,
            "step_name": step.name or "LQA",
            "step_type": step.step_type,
            # #96: the live event names the JUDGE's engine/model/attempts too, so the SSE
            # stream and the CLI --json row agree with the durable StepRun row. None/0 on
            # an offline/mock card (no live judge) -> byte-identical to before.
            "provider": _lqa_shape.get("provider"),
            "model": _lqa_shape.get("model"),
            "chunks": 1,
            "attempts": int(_lqa_shape.get("attempts") or 0),
            "error": _lqa_scope_note(lqa_scope),
            "lqa": {**_lqa_event_payload(card), **(lqa_scope or {})},
            "output": input_blob,
        }
        return _StepDirective(next_cursor=next_cur, lqa_findings_block=lqa_findings_block)

    # SPEC §2 spend gate (Phase 1 prerequisite): an llm_agent step whose ENTIRE provider
    # chain is over the monthly cap fails the run FAST with a clean "spend cap reached"
    # reason, BEFORE any paid call. None (no-op) when any chain target is mock/exempt/
    # under-cap, so every hermetic test + plain run + fallback-rescued run is byte-
    # identical. On no block the caller runs its own (mode-specific) fan-out.
    block_msg = _spend_block_reason(step)
    if block_msg:
        session.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.FAILED,
                input_text=input_blob,
                output_text="",
                error=block_msg,
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )
        )
        _commit_run_terminal_direct(session, run, RunStatus.FAILED)
        yield {"type": "error", "run_id": run.id, "error": block_msg}
        return _StepDirective(stop=True)
    return _StepDirective(fallthrough=True)


def publish_run_progress(
    run_id,
    done: int,
    total: int,
    unit: str,
    *,
    force: bool = False,
    step: str = "",
    step_index: int | None = None,
) -> None:
    """Publish "done of total <unit>" for `run_id` as an ABSOLUTE position (GitHub #101):
    into the in-memory map for this process's own readers, and (via the background writer)
    onto the run row subject to the throttle. `force` on a step's first publish, so a step
    shows movement immediately."""
    if not run_id:
        return
    now_dt = datetime.utcnow()
    with _progress_lock:
        st = _progress_state.get(run_id)
        if st is None:
            st = {
                "flushed_done": -1,
                "flushed_at": 0.0,
                "attempt_at": 0.0,
                "step": "",
                "step_index": 0,
            }
            _progress_state[run_id] = st
        st["done"] = max(0, int(done))
        st["total"] = max(0, int(total))
        st["unit"] = str(unit or "")[:32]
        if step:
            st["step"] = str(step)[:64]
        if step_index is not None:
            # #505 item 8: `is not None`, not truthiness. Step 0 is a real step, and a loop that
            # re-enters it must overwrite the previous step's index instead of leaving it stale.
            st["step_index"] = max(0, int(step_index))
        st["at"] = now_dt
    _flush_progress(run_id, force=force)
