"""orchestrator: part7

Documented in: docs/architecture/translation-pipeline.md
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlmodel import Session

from app import config
from app import db as _db
from app.models import (
    LQAResult,
    RunStatus,
    StepRun,
    WorkflowRun,
)
from app.services import (
    lqa,
    lqa_attribution,
    pii,
    pii_boundary,
    tap_envelope,
)
from app.services.orchestrator_part2 import _spec_for_step
from app.services.orchestrator_part3 import (
    _lqa_event_payload,
    _lqa_scope_note,
    _lqa_step_error,
    _prior_lqa_verdicts,
    _remember_lqa_scope,
    _skip_empty_lqa,
)
from app.services.orchestrator_part4 import RunGuard
from app.services.orchestrator_part5 import _branch_cursor, _eval_run_condition, _record_gate_hold
from app.services.orchestrator_terminology_backen import (
    GATE_HOLD_JUDGE_UNAVAILABLE,
    GATE_HOLD_NO_PROGRESS,
    _end_run_cursor,
    _lqa_judge_unavailable,
    _lqa_no_progress,
    _lqa_no_progress_error,
    _lqa_shape_kwargs,
    _mt_workers,
)


def _run_parallel_group(
    session: Session,
    run: WorkflowRun,
    steps,
    first: int,
    last: int,
    *,
    guard: "RunGuard",
    key_index: dict,
    src_blob: str,
    tgt_blob: str,
    src: str,
    locale: str,
    sg_render: str,
    lqa_terms,
    workflow_id: int,
    doc_id: int,
    env_ids=(None, None),
    lqa_scope=None,
) -> dict:
    """Execute ONE parallel group (SPEC-parallel-routing §5.2-§5.4) and return what the
    calling loop needs to continue. Shared VERBATIM by both orchestrator loops (the
    segmented primary path and the FULL_TEXT mirror) so the two can never drift — the
    only difference between them is which blobs they hand in, because a Phase-1 lane
    only READS the working text.

    Returns:
      {"cap_reason": str|None, "cap_step": WorkflowStep|None,   # loop cap, before dispatch
       "events": [dict, ...],                                   # in order_index order
       "next_cursor": int, "failed": bool, "error": str|None,
       "findings_block": str|None}                              # "" is meaningful, None = unchanged

    THE ORDER OF OPERATIONS IS THE DESIGN. It is the house discipline
    (`_run_step_on_segment` / `eval_runner._build_plan`) applied to whole steps:

      1. guard.enter() every member, in order_index order, on the DB thread. It commits
         per its existing contract; a `loop_cap_reached` returns immediately, BEFORE any
         dispatch, so the run fails exactly as a sequential step would.
      2. Evaluate every member's run_condition (a DB read of durable StepRuns). A skipped
         member stages a SKIPPED StepRun at the join and never dispatches.
      3. Resolve EVERY ORM-touching input to plain data: the ProviderSpec, the provider
         name, the redaction choice, the tap meta, the visit index, and the frozen
         source/target blobs. Nothing an ORM instance owns may be read after this point.
      4. RELEASE. `db.commit_if_staged` hands SQLite's writer lock back if we hold it.
         This is QA-1's cure, not tidiness: a staged write held across a fan-out
         deadlocks against the nested Sessions `spend.record_usage` / `audit.log` /
         `lqa._capture_lqa_findings` open on the workers. It also must NOT expire the
         identity map (see its docstring) — never replace it with a plain commit().
      5. FAN OUT on a pool bounded by the SAME setting per-segment work uses
         (`_mt_workers()` / `mt_concurrency`). It is a fresh per-group pool, not a shared
         pool instance — what is shared is the CEILING, which is the property that
         matters: worst-case in-flight stays batch_max_parallel x mt_concurrency, so
         lanes buy no third multiplier. `lqa.score_text` is session-free by contract
         (its own docstring; it opens its OWN short-lived Session for the LLMemory
         capture, the same posture as audit.log), so it is safe off-thread.
         `mt_concurrency = 1` therefore SERIALIZES a group's lanes. That is deliberate
         and is not floored to 2: an install that set 1 chose to make one provider call
         at a time, and quietly overriding that to run two judges at once would be the
         worse surprise. Semantics are unchanged either way (a group's result does not
         depend on overlap) — only wall-clock is. Documented on the setting too.

      ARGUMENTS SHARED ACROSS LANES ARE SHARED BY REFERENCE and must stay READ-ONLY:
      `rulebook` (str), `sg_render` (str) and `lqa_terms` (the run's already-matched
      glossary list) are passed to every lane's `score_text` as the same objects. The
      two strings are immutable; `lqa_terms` is a list of dicts that `score_text` only
      reads (it never queries or mutates the glossary — its own docstring is explicit).
      A future lane-side mutation of `lqa_terms` would be a cross-lane data race with no
      lock anywhere, so it must not be introduced.
      6. JOIN on the DB thread: persist every member's StepRun in order_index order
         inside ONE commit, then route.

    THE ID-ORDER INVARIANT (§5.4). `_latest_outcome_steprun` and `observe_run` both
    infer execution order from StepRun.id. Members complete in a nondeterministic order,
    so the join re-imposes order_index order and FLUSHES each row as it is added — the
    ids are then monotonic with a deterministic order, and an observer's replay of a
    parallel run is reproducible. One commit still bounds the whole join.

    FAILURE (§5.2). A member whose scoring call raised is a FAILED StepRun and the run
    fails — but only AFTER every member has completed and persisted, so the operator
    sees every lane's outcome rather than just the first crash. Exceptions are caught
    broadly (not just DeadEngineError) because a lane runs off-thread: an escaping
    exception there would surface as a bare future traceback with no StepRun at all."""
    # THE GROUP'S EXECUTION START (review NEW-1). Every StepRun this function persists
    # -- skipped, failed and completed lanes alike -- stamps it, so a run's newest
    # StepRun timestamp is a real "last sign of life" the staleness floor and the boot
    # healer can trust. The lanes run concurrently, so the GROUP's start is the honest
    # per-lane start.
    group_started_at = datetime.utcnow()
    members = list(steps[first : last + 1])
    out = {
        "cap_reason": None,
        "cap_step": None,
        "events": [],
        "next_cursor": last + 1,
        "failed": False,
        "error": None,
        "findings_block": None,
    }

    # 1. FORK bookkeeping. current_step_index holds the group's FIRST member index for
    #    the duration of the group — one honest scalar: "the run is at the group".
    run.current_step_index = first
    session.add(run)
    for m in members:
        cap = guard.enter(m)
        if cap:
            out["cap_reason"], out["cap_step"] = cap, m
            return out

    # 2. Pre-step guards. A skipped member stages its SKIPPED row at the join (§5.2.2).
    dispatching = [
        m for m in members if not (m.run_condition and not _eval_run_condition(m, run, session))
    ]
    if lqa_scope is not None and not lqa_scope["judged"]:
        for m_index, m in enumerate(members, first):
            if m in dispatching:
                continue
            vi = guard.visit_index(m)
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=m.id,
                    status=RunStatus.SKIPPED,
                    input_text=tgt_blob,
                    output_text=tgt_blob,
                    outcome_index=vi,
                    started_at=group_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            session.flush()
            out["events"].append(
                {
                    "type": "step_skipped",
                    "run_id": run.id,
                    "step_index": m_index,
                    "step_name": m.name or m.step_type,
                    "step_type": m.step_type,
                    "condition": dict(m.run_condition or {}),
                    "output": tgt_blob,
                    "parallel_group": m.parallel_group,
                }
            )
        for member in dispatching:
            index = first + members.index(member)
            out["events"].append(
                _skip_empty_lqa(session, run, member, index, guard, group_started_at, lqa_scope)
            )
        # No verdict: nothing failed. Use the join's on_pass arm, without a pass row.
        routing = next((m for m in members if m.branch), None)
        if routing is not None:
            out["next_cursor"], _ = _branch_cursor(
                routing, last, gate="passed", key_index=key_index, steps=steps, guard=guard
            )
        run.current_step_index = out["next_cursor"]
        session.add(run)
        session.commit()
        out["findings_block"] = ""
        return out

    # ISSUE #95 -- NO-PROGRESS GUARD, per LANE. A member re-entered on BYTE-IDENTICAL text
    # carries its own previous verdict forward instead of re-rolling its judge (see
    # `_lqa_no_progress`). Computed BEFORE dispatch so a carried lane makes no call at all.
    # Empty on every first visit -> a linear / first-pass group is byte-identical.
    carried = {}
    for m in dispatching:
        _p = _lqa_no_progress(session, _prior_lqa_verdicts(session, run.id, m.id), tgt_blob)
        if _p is not None:
            carried[m.id] = _p
    dispatching = [m for m in dispatching if m.id not in carried]
    dispatch_ids = {m.id for m in dispatching}

    # 3. Resolve every ORM-touching input to plain data (the frozen snapshot included).
    rulebook = lqa.load_rulebook(src, locale)
    plans = []
    for m in dispatching:
        l_src, l_tgt = src_blob, tgt_blob
        if m.redact_pii or pii.enabled():
            # NER language gate (Train P): scrub the source under the source locale, the target
            # under the target locale, so an English NER model never over-captures foreign text.
            l_src = pii.redact(l_src, lang=src)[0]
            l_tgt = pii.redact(l_tgt, lang=locale)[0]
        plans.append(
            {
                "step_id": m.id,
                "redact_pii": m.redact_pii,
                "provider": m.provider or config.DEFAULT_PROVIDER,
                "spec": _spec_for_step(m),
                # feat/lqa-configurable-threshold: PER-MEMBER pass bar — each lane in a
                # parallel LQA group gates on its OWN lqa_threshold (NULL -> default 90).
                "lqa_threshold": getattr(m, "lqa_threshold", None),
                "src": l_src,
                "tgt": l_tgt,
                "tap_meta": tap_envelope.stamp_meta(
                    {
                        "workflow_id": workflow_id,
                        "workflow_run_id": run.id,
                        "step_key": m.key,
                        "step_name": m.name or m.step_type,
                        "document_id": doc_id,
                    },
                    *env_ids,
                ),
            }
        )
    visits = {m.id: guard.visit_index(m) for m in members}

    # 4. RELEASE — nothing staged may be held across dispatch.
    _db.commit_if_staged(session)

    # 5. FAN OUT. Every lane sees the SAME frozen snapshot resolved in step 3; none can
    #    observe another lane's work, because a check writes no text at all.
    def _score(plan):
        # #96: time each LANE separately so every member's StepRun carries ITS OWN elapsed
        # (the group's wall time would over-report the fast lanes).
        _t0 = time.monotonic()
        try:
            return _score_lane(plan)
        finally:
            plan["elapsed_ms"] = int((time.monotonic() - _t0) * 1000)

    def _score_lane(plan):
        return pii_boundary.call_with_redaction(
            plan["redact_pii"],
            lqa.score_text,
            plan["src"],
            plan["tgt"],
            src,
            locale,
            rulebook,
            plan["provider"],
            style_guide=sg_render,
            glossary_terms=lqa_terms,
            spec=plan["spec"],
            lqa_threshold=plan["lqa_threshold"],
            tap_meta=plan["tap_meta"],
        )

    cards: dict = {}
    errors: dict = {}
    plan_by_step: dict = {p["step_id"]: p for p in plans}
    if plans:
        with ThreadPoolExecutor(max_workers=max(1, min(_mt_workers(), len(plans)))) as pool:
            # AI opt-out (DPAI-08): carry the AI scope into each scoring thread.
            from app.services import ai_optout as _ai_optout

            futs = {_ai_optout.submit(pool, _score, p): p["step_id"] for p in plans}
            for fut in futs:
                sid = futs[fut]
                try:
                    cards[sid] = fut.result()
                except Exception as exc:
                    errors[sid] = exc

    # 6. JOIN — persist in order_index order, ONE commit.
    findings = None
    findings_spans: dict = {}
    routing_step, routing_gate = None, None
    routing_index = first  # graph index of the routing member (for the hold record)
    routing_hold_cause = None  # non-None -> record a run-level GATE HOLD at the join
    # The routing member must END THE RUN at this group rather than route. TWO reasons,
    # both 'the gate did not deliver a usable verdict on NEW text':
    #   * #56/F5 -- the routing member's judge came back UNAVAILABLE (an outage);
    #   * #95    -- the routing member carried a FAILED verdict forward because the loop
    #               made no progress (byte-identical text), so re-routing on_fail could
    #               only repeat forever and falling through would deliver ungated text.
    # (Renamed from `routing_hold` in round 2's M3: "hold" is the POSTURE, recorded by
    # `_record_gate_hold`; this flag is only the routing decision.)
    routing_end = False
    # Round 2, M1: True when the ROUTING member's verdict was CARRIED (no judge call this
    # visit). A carried verdict means the loop made no progress, so the loop is over --
    # exactly as at the sequential site, a backward/self jump must NOT be taken.
    routing_carried = False
    # Round 2, L3: a NON-routing lane that carried a FAILED verdict. §5.3 routes on the
    # routing member alone, which was harmless while a carried FAIL only stopped the
    # cursor -- but a carried FAIL is now a DELIVERY-BLOCKING hold, and "only the routing
    # lane's verdict can block delivery" is not a rule anyone would choose. A second lane
    # that carried a FAIL holds the join exactly like the routing lane; routing itself is
    # untouched (§5.3 still resolves on the branch-carrying member).
    lane_hold: tuple | None = None  # (step, graph index) of the first such lane
    # #505 item 6: a NON-routing lane whose judge was UNAVAILABLE (an outage). The sequential
    # path and the routing lane both end the run HELD on an outage (#56 / M3: a configured gate
    # that could not be evaluated must not deliver); a plain lane used to be ignored, so the
    # unevaluated text shipped. Same shape as `lane_hold`: routing is untouched, the hold is
    # added at the join.
    lane_outage: tuple | None = None  # (step, graph index) of the first such lane
    for _mi, m in enumerate(members):
        m_index = first + _mi
        vi = visits.get(m.id, 0)
        if m.id not in dispatch_ids and m.id not in carried:
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=m.id,
                    status=RunStatus.SKIPPED,
                    input_text=tgt_blob,
                    output_text=tgt_blob,
                    outcome_index=vi,
                    started_at=group_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            session.flush()
            out["events"].append(
                {
                    "type": "step_skipped",
                    "run_id": run.id,
                    "step_index": m_index,
                    "step_name": m.name or m.step_type,
                    "step_type": m.step_type,
                    "condition": dict(m.run_condition or {}),
                    "output": tgt_blob,
                    "parallel_group": m.parallel_group,
                }
            )
            continue
        if m.id in carried:
            _remember_lqa_scope(run, m, guard, lqa_scope)
            # #95: this lane's judge was NOT called -- its previous verdict rides forward
            # verbatim, with the reason named on the row (score/gate carried, provider and
            # spend NULL because no call was made).
            _c = carried[m.id]
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=m.id,
                    status=RunStatus.COMPLETED,
                    input_text=tgt_blob,
                    output_text=tgt_blob,
                    error=_lqa_scope_note(lqa_scope, _lqa_no_progress_error(_c)),
                    score=_c.score,
                    gate=_c.gate,
                    outcome_index=vi,
                    started_at=group_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            session.flush()
            if getattr(m, "branch", None):
                routing_step, routing_gate, routing_index = m, _c.gate, m_index
                # A carried FAIL must HOLD at the join, exactly like the sequential site:
                # re-entering the already-judged-bytes state can only repeat forever, and
                # falling through would deliver text this gate never passed.
                # `_end_run_cursor` is reached through the same `routing_end` seam, and the
                # hold is RECORDED on the run so the write-back flags the units and the
                # export refuses (gate C round 1, H1).
                routing_end = _c.gate == "failed"
                routing_hold_cause = GATE_HOLD_NO_PROGRESS if routing_end else None
                # M1: carried either way -- a carried PASS must not take an `on_pass`
                # loop-back back into the group (see the join).
                routing_carried = True
            elif _c.gate == "failed" and lane_hold is None:
                lane_hold = (m, m_index)  # L3
            out["events"].append(
                {
                    "type": "step",
                    "run_id": run.id,
                    "step_index": m_index,
                    "step_name": m.name or "LQA",
                    "step_type": m.step_type,
                    "provider": None,
                    "model": None,
                    "chunks": 1,
                    "attempts": 0,
                    "error": _lqa_no_progress_error(_c),
                    "lqa": {
                        "score": _c.score,
                        "gate": ("auto_approved" if _c.gate == "passed" else "needs_review"),
                        "no_progress": True,
                    },
                    "output": tgt_blob,
                    "parallel_group": m.parallel_group,
                }
            )
            continue
        if m.id in errors:
            err = f"step '{m.name or 'LQA'}' ({src}->{locale}): {errors[m.id]}"
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=m.id,
                    status=RunStatus.FAILED,
                    input_text=tgt_blob,
                    output_text="",
                    error=err,
                    outcome_index=vi,
                    started_at=group_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            session.flush()
            out["failed"] = True
            out["error"] = out["error"] or err
            out["events"].append(
                {
                    "type": "step",
                    "run_id": run.id,
                    "step_index": m_index,
                    "step_name": m.name or "LQA",
                    "step_type": m.step_type,
                    "provider": None,
                    "model": None,
                    "chunks": 1,
                    "attempts": 0,
                    "error": err,
                    "output": tgt_blob,
                    "parallel_group": m.parallel_group,
                }
            )
            continue
        card = cards[m.id]
        _remember_lqa_scope(run, m, guard, lqa_scope)
        block, block_spans = lqa.render_findings_with_spans(card)
        if block:
            # LAST NON-EMPTY block by order_index wins -- which is subtly NOT "the last
            # member's block" when a later lane renders "" (render_findings returns ""
            # only for a card with no score at all). In that corner the findings a fix
            # step receives come from an EARLIER lane than the one `prev_lqa` resolves
            # to, so the two can disagree. Kept deliberately: the alternative (letting an
            # empty render CLEAR the block) would hand the fixer nothing at all, which is
            # strictly worse, and it matches the sequential path's own guarded overwrite
            # exactly. An author who needs a specific lane's verdict names it with
            # `step:<key>`.
            findings = block  # see the note under the loop
            # #163: the winning lane's spans travel with its block, never mixed across
            # lanes -- a span from a lane whose block LOST would attribute findings the
            # fixer never sees.
            findings_spans = block_spans
        # Wave 2 attribution (§3.1). ONE scoring_id per card (this step visit scores one
        # card over the whole document, segment_id=None), stamped on every row. Vendor
        # attribution resolves the RETURNED leg whose content this card scored (§3.1.7);
        # (None, None) when none applies. dimension/severity canonicalized at persist only.
        _sid = uuid.uuid4().hex
        _vid, _did = lqa_attribution.vendor_for_scored_content(
            session, workflow_run_id=run.id, target_locale=locale, scored_at=datetime.utcnow()
        )
        for e in card["errors"]:
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
        gate = "passed" if card["passed"] else "failed"
        session.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=m.id,
                status=RunStatus.COMPLETED,
                input_text=tgt_blob,
                output_text=tgt_blob,
                error=_lqa_scope_note(lqa_scope, _lqa_step_error(card)),
                # #96 JUDGE ATTRIBUTION -- the same stamp the sequential LQA site writes,
                # per LANE (each member has its own judge call and its own elapsed).
                **_lqa_shape_kwargs(card, (plan_by_step.get(m.id) or {}).get("elapsed_ms")),
                score=float(card["score"]),
                gate=gate,
                cost_usd=card.get("cost_usd") or 0.0,
                tokens_in=card.get("tokens_in") or 0,
                tokens_out=card.get("tokens_out") or 0,
                outcome_index=vi,
                started_at=group_started_at,
                finished_at=datetime.utcnow(),
            )
        )
        session.flush()
        if not getattr(m, "branch", None) and lane_outage is None and _lqa_judge_unavailable(card):
            lane_outage = (m, m_index)
        if getattr(m, "branch", None):
            # §5.3: only the ROUTING member's branch routes. Save-time validation
            # allows at most one member to carry a branch, so this cannot be ambiguous.
            routing_step, routing_gate, routing_index = m, gate, m_index
            # #56/F5: remember whether the ROUTING member's judge was unavailable, so the
            # join below ends the run instead of driving the on_fail fix loop over a
            # phantom. M3 (round 2): that ending is now a HELD one -- a configured gate
            # that could not be EVALUATED must not deliver its text either.
            routing_end = _lqa_judge_unavailable(card)
            routing_hold_cause = GATE_HOLD_JUDGE_UNAVAILABLE if routing_end else None
        _m_shape = (card or {}).get("judge_shape") or {}
        out["events"].append(
            {
                "type": "step",
                "run_id": run.id,
                "step_index": m_index,
                "step_name": m.name or "LQA",
                "step_type": m.step_type,
                # #96: name this lane's judge engine/model/attempts on the event too.
                "provider": _m_shape.get("provider"),
                "model": _m_shape.get("model"),
                "chunks": 1,
                "attempts": int(_m_shape.get("attempts") or 0),
                "error": _lqa_scope_note(lqa_scope),
                "lqa": {**_lqa_event_payload(card), **(lqa_scope or {})},
                "output": tgt_blob,
                "parallel_group": m.parallel_group,
            }
        )

    # THE FINDINGS BLOCK, when several lanes produced one. The rule is the LAST member
    # by order_index wins — the same answer a SEQUENTIAL run of these same steps gives
    # ("the most recent LQA card"), and the same member `prev_lqa` /
    # `_latest_outcome_steprun` resolve to (highest id = highest order_index, by the
    # invariant above). Findings and prev_lqa therefore always name the SAME lane; an
    # author who wants a different one addresses it with `step:<key>` (§5.5). Choosing
    # the ROUTING member instead was rejected: it would make the two disagree.
    if findings:
        run.context = {
            **(run.context or {}),
            "lqa_findings_block": findings,
            "lqa_findings_spans": {str(k): v for k, v in findings_spans.items()},
        }
        out["findings_block"] = findings

    # §5.3 routing at the JOIN. No routing member (or a gate outside {passed, failed},
    # or a skipped/failed routing member) -> fall through to the index after the LAST
    # member, exactly like a branch-less gate today.
    _held_here = False  # did THIS join already record a gate hold?
    if routing_step is not None:
        if routing_end:
            # #56/F5+F12: END THE RUN when the routing member's judge was UNAVAILABLE, or
            # when it carried a FAILED verdict. Never route on_fail's fix loop, never fall
            # through to downstream steps — a bare `last + 1` let a DELIVER step run on an
            # outage while a healthy fail routed on_fail:end and stopped. len(steps) exits
            # the loop cleanly, the same honest end as on_fail:"end".
            out["next_cursor"] = _end_run_cursor(steps)
            if routing_hold_cause:
                # #95 (gate C H1): a carried FAIL at the join is a GATE HOLD, recorded on
                # the run so the write-back flags its units and the export refuses. M3
                # (round 2): a judge OUTAGE is held the same way, under its own cause.
                _record_gate_hold(
                    session,
                    run,
                    step=routing_step,
                    step_index=routing_index,
                    cause=routing_hold_cause,
                )
                _held_here = True
        else:
            # H2: a refused `on_fail` loop-back from a FAILED gate ends the run HELD. The
            # OTHER failed endings route normally here and are held at the terminal
            # write-back instead (M2, `_terminal_gate_hold`).
            out["next_cursor"], _join_hold = _branch_cursor(
                routing_step, last, gate=routing_gate, key_index=key_index, steps=steps, guard=guard
            )
            # M1 (gate C round 2): the M4 clamp, at the JOIN. `_branch_cursor` resolves an
            # `on_pass: {step: ...}` to an EARLIER index (branches may only target an
            # earlier step, so EVERY step-target jump is backward), and the sequential
            # carried-verdict site degrades such a jump to `cursor + 1` because a CARRIED
            # verdict means the repair changed nothing and the loop is over. The join
            # called `_branch_cursor` raw, so the identical logical shape burned five full
            # document provider passes where the sequential path burned two. `last + 1` is
            # the join's own fall-through (the index after the LAST member), so a
            # self-jump into the group degrades exactly like the sequential self-jump.
            if routing_carried and out["next_cursor"] <= last:
                out["next_cursor"] = last + 1
            if _join_hold:
                _record_gate_hold(
                    session, run, step=routing_step, step_index=routing_index, cause=_join_hold
                )
                _held_here = True
    if lane_outage is not None and not _held_here:
        # #505 item 6: end the run at the group, held, exactly as a judge outage on the
        # routing lane (or on the sequential path) does.
        out["next_cursor"] = _end_run_cursor(steps)
        _record_gate_hold(
            session,
            run,
            step=lane_outage[0],
            step_index=lane_outage[1],
            cause=GATE_HOLD_JUDGE_UNAVAILABLE,
        )
        _held_here = True
    if lane_hold is not None and not _held_here:
        # L3 (gate C round 2): a NON-routing lane carried a FAILED verdict. Routing has
        # already resolved above (§5.3 -- the branch-carrying member alone decides where
        # the cursor goes, unchanged); this adds the HOLD, and ends the run at the group
        # for the same reason the ROUTING lane's carried FAIL does: the text this lane
        # failed is the text the write-back is about to commit, and "only the routing
        # lane's verdict can block delivery" is not a rule anyone would choose now that a
        # carried FAIL blocks delivery. Applies with or without a routing member.
        out["next_cursor"] = _end_run_cursor(steps)
        _record_gate_hold(
            session, run, step=lane_hold[0], step_index=lane_hold[1], cause=GATE_HOLD_NO_PROGRESS
        )
    if not out["failed"]:
        run.current_step_index = out["next_cursor"]
    session.add(run)
    session.commit()
    return out
