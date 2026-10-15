"""orchestrator: part3

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
from datetime import datetime

from sqlmodel import Session, select

from app import config, db
from app import extensions as _extensions
from app.models import (
    Document,
    RunStatus,
    StepRun,
    StepType,
    TransMode,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    glossary_concepts,
    lqa,
    postedit,
    prompts,
)
from app.services.lqa_safety import LQA_EMPTY_SCOPE_PREFIX
from app.services.orchestrator_part2 import (
    _clean_draft,
    _latest_paused_steprun,
    _load_segments,
    _source_text,
    _stamp_note,
    get_steps,
)
from app.services.orchestrator_terminology_backen import (
    _DEFAULT_VERDICT_SCOPE,
    _FINDING_LINE_RE,
    _GATE_FAMILY_LQA,
    _LQA_PRIOR_VERDICT_LIMIT,
    DEFAULT_PROMPTS,
    EMPTY_DOCUMENT_MSG,
    GATE_HOLD_JUDGE_UNAVAILABLE,
    GATE_HOLD_LOOP_EXHAUSTED,
    GATE_HOLD_NO_PROGRESS,
    GATE_HOLD_UNREPAIRED,
    GATE_HOLD_VISIT_CAP,
    LQA_FINDINGS_SEGMENT_CAP,
    SEG_JOINER,
    _adjudicated_count,
    _cap_keep_fence,
    _findings_relevant,
    _five_key,
    _int_or,
    _is_lqa_onfail_target,
    _PriorVerdict,
    _references_prev_lqa,
    _verdict_family,
)
from app.services.providers import (
    ProviderCallError,
    ProviderSpec,
    ProviderTruncated,
    assert_provider_visible,
    get_provider,
    resolve_default_model,
    run_completion,
)

# Optional add-on: reached through the extension facade (no-op when absent); name unchanged.
llmemory = _extensions.use("memory")

log = logging.getLogger("loc_tms.orchestrator")


def _is_correction_loop_step(step, steps) -> bool:
    """True when `step` is an llm_agent step in the CORRECTION-LOOP shape — gated on
    `prev_lqa:failed`, or the loop-back target of some LQA step's `on_fail` branch.

    ONE predicate, three consumers: `_findings_suffix` (auto-append the failing card's
    findings), and the LQA safety floor's (D1) TM provenance guard, which protects
    `tm_origin` segments on exactly this shape by default
    (PROPOSAL-lqa-glossary-adjudication §4.D1: "the risky path — a step acting on
    findings that may not even be about the segment it is rewriting"). Keeping the
    shape definition in one place means the step that RECEIVES findings and the step
    whose TM segments are protected can never drift apart."""
    if step.step_type != StepType.LLM_AGENT:
        return False
    return _references_prev_lqa(step) or _is_lqa_onfail_target(step, steps)


def _lqa_repair_steps(checks, steps):
    """Repair route for one check, or parallel lanes sharing a single join route.

    An explicit failure jump owns the repair route. With failure fallthrough,
    prev_lqa:failed agents before the next check consume this check's findings.
    Exclusion requires every possible repairing agent to refuse the segment.
    """
    routing = next((st for st in checks if st.branch), checks[-1])
    target = (routing.branch or {}).get("on_fail")
    if isinstance(target, dict):
        return [
            st
            for st in steps
            if st.key == target.get("step") and st.step_type == StepType.LLM_AGENT
        ]
    if target == "end":
        return []
    repairers = []
    for st in steps[steps.index(checks[-1]) + 1 :]:
        if st.step_type == StepType.LQA:
            break
        if st.step_type == StepType.LLM_AGENT and _references_prev_lqa(st):
            repairers.append(st)
    return repairers


def _remember_lqa_scope(run, step, guard, scope):
    if scope is not None:
        key = f"{step.id}:{guard.visit_index(step)}"
        scopes = {
            **(run.context or {}).get("lqa_excluded_indices", {}),
            key: scope["excluded_indices"],
        }
        run.context = {
            **(run.context or {}),
            "lqa_tm_excluded": len({j for indices in scopes.values() for j in indices}),
            "lqa_excluded_indices": scopes,
        }


def _lqa_scope_note(scope, note=None):
    if scope is None:
        return note
    reason = (
        f"LQA excluded {scope['excluded']} protected segment(s): "
        f"correction-loop fixer may not repair them; {scope['judged']} segment(s) judged."
    )
    return f"{note} | {reason}" if note else reason


def _skip_empty_lqa(session, run, step, cursor, guard, started_at, scope):
    """No applicable units means SKIPPED, never a fabricated passing verdict."""
    _remember_lqa_scope(run, step, guard, scope)
    note = _lqa_scope_note(
        scope,
        LQA_EMPTY_SCOPE_PREFIX + " no score or verdict; prev_lqa:failed and "
        "prev_lqa:passed are both false, so steps gated on either do not run",
    )
    session.add(
        StepRun(
            workflow_run_id=run.id,
            step_id=step.id,
            status=RunStatus.SKIPPED,
            input_text="",
            output_text="",
            error=note,
            outcome_index=guard.visit_index(step),
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
    )
    # The block and its span map are written together and cleared together (#163), so a
    # later writer can never pair an empty block with a previous visit's spans.
    run.context = {
        **(run.context or {}),
        "lqa_findings_block": "",
        "lqa_findings_spans": {},
    }
    session.add(run)
    session.flush()
    return {
        "type": "step_skipped",
        "run_id": run.id,
        "step_index": cursor,
        "step_name": step.name or "LQA",
        "step_type": step.step_type,
        "condition": {},
        "output": "",
        "error": note,
        "lqa": {"skipped": True, "score": None, "gate": None, **scope},
    }


def _prior_lqa_verdicts(session: Session, run_id: int, step_id: int) -> list:
    """Every VERDICT-BEARING StepRun this LQA step already wrote in this run, newest
    first, as metadata-only `_PriorVerdict`s (bounded read, projected columns).

    Rows that carry no gate are skipped deliberately: a SKIPPED (run_condition) row and a
    FAILED dead-engine row are not verdicts, so they can neither be "carried forward" nor
    mistaken for a previous judging of the same bytes.

    ALL of them, not just the newest (gate C round 1, M3): a repair that OSCILLATES
    between two outputs (x -> x+" X" -> x -> ...) makes progress by the newest-row rule on
    every visit and so restores the exact re-roll lottery #95 exists to close. Returning to
    ANY previously-judged bytes is no progress."""
    rows = session.exec(
        select(StepRun.id, StepRun.gate, StepRun.score, StepRun.outcome_index)
        .where(
            StepRun.workflow_run_id == run_id,
            StepRun.step_id == step_id,
            StepRun.gate.in_(("passed", "failed")),
        )
        .order_by(StepRun.id.desc())
        .limit(_LQA_PRIOR_VERDICT_LIMIT)
    ).all()
    return [_PriorVerdict(r) for r in rows]


def _gate_hold_message(step, cause: str, detail: str = "") -> str:
    """The one operator-facing sentence for a gate hold, shared by every surface."""
    name = getattr(step, "name", None) or getattr(step, "step_type", None) or "the check"
    if cause == GATE_HOLD_JUDGE_UNAVAILABLE:
        # M3 (gate C round 2, ruled): an outage is NOT "the gate failed this text" -- the
        # gate was never evaluated. Same posture (flagged units, refused export), its own
        # honest sentence, carrying the exact phrase every surface quotes.
        return (
            f"HELD: quality check {name!r} was not evaluated: judge unavailable. The run "
            "ended at the check with its text UNJUDGED; the segments it produced are "
            "flagged for review and the document export is refused until a reviewer "
            "approves them." + ((" " + detail) if detail else "")
        )
    why = {
        GATE_HOLD_NO_PROGRESS: (
            "its correction loop stopped making progress (the repair returned text this "
            "gate had already judged)"
        ),
        GATE_HOLD_LOOP_EXHAUSTED: (
            "its correction loop ran out of visits (the step it loops back into is at its "
            "max_visits ceiling)"
        ),
        GATE_HOLD_VISIT_CAP: "the check itself reached its max_visits ceiling",
        # ROUND 3, L1: the old wording asserted a branch this ending may not have. The
        # commonest shape it covers is a check with NO branch at all.
        GATE_HOLD_UNREPAIRED: (
            "the check has no correction loop (its branch ends the run or moves on, or it "
            "has no branch at all), so nothing was going to fix the text"
        ),
    }.get(cause, "its correction loop ended")
    return (
        f"HELD: quality check {name!r} last judged this text as FAILED and {why}. The run "
        "ended at the check; the segments it produced are flagged for review and the "
        "document export is refused until a reviewer approves them."
        + ((" " + detail) if detail else "")
    )


def _standing_gate_verdicts(session: Session, run_id: int, scope: frozenset | None = None) -> dict:
    """`{step_id: "passed" | "failed"}` -- the LAST verdict each DOCUMENT-LEVEL check wrote
    in this run. A loop that failed and then passed stands at `passed`. Projected read
    (step_id + gate + step_type only), verdict rows only.

    SCOPE: `lqa` STEPS ONLY (gate C round 3, H2 -- ruled). `StepRun.gate` is also written
    by an `llm_agent` step with the QE routing gate armed, and E2's posture ("a run that
    ENDS with a standing FAILED verdict is HELD") was therefore silently converting that
    gate into a DELIVERY block: ONE below-threshold segment out of 500 made the step's
    aggregate `sr_gate == "failed"`, held the run, badged every produced unit CRITICAL and
    refused the whole locale. The QE gate's documented contract is PER-SEGMENT -- score
    each unit, route the low ones to review, ship the rest -- so it stays the router it is
    documented to be; only an `lqa` step's whole-document verdict decides delivery. (A QE
    step whose own CORRECTION LOOP ends -- no-progress, exhausted, capped -- still records
    its hold at the branch seam under E1; that is unchanged, and it is a loop ending, not a
    below-threshold row.) `_is_gate_step` deliberately keeps counting a QE-gated step: that
    predicate governs the `max_visits` CEILING (routing), which is not the posture.

    FAIL CLOSED on a step row that is GONE (deleted mid-run) or RETYPED mid-run: neither
    can be positively identified as a QE row, so both are KEPT -- see `_verdict_family`.
    A deleted one is named by `_DELETED_GATE_STEP`.

    `scope` (gate C round 4, M2) widens the read to other families for the RELEASE half:
    `{lqa, qe}` lets a QE-gated workflow's own passing verdict release the hold ERRATUM E1
    recorded from that same workflow's correction loop. The HOLD half never widens -- every
    caller that decides the posture (`_terminal_gate_hold`) takes the default."""
    rows = session.exec(
        select(StepRun.step_id, StepRun.gate, WorkflowStep.step_type, WorkflowStep.qe_gate)
        .join(WorkflowStep, WorkflowStep.id == StepRun.step_id, isouter=True)
        .where(
            StepRun.workflow_run_id == run_id,
            StepRun.gate.in_(("passed", "failed")),
        )
        .order_by(StepRun.id)
    ).all()
    families = scope or _DEFAULT_VERDICT_SCOPE
    standing: dict = {}
    for step_id, gate, step_type, qe_gate in rows:
        if _verdict_family(step_type, qe_gate) not in families:
            continue
        standing[step_id] = gate
    return standing


def _last_gate_judged_text(
    session: Session, run_id: int, scope: frozenset | None = None, *, with_indices=False
):
    """The EXACT text this run's most recent IN-SCOPE verdict judged, or None when no check
    in this run ever produced one.

    GATE C ROUND 3, M1. The release rule is "a check PASSED over the unit's CURRENT text",
    and a run-level "some check passed somewhere" cannot say that: `mt -> lqa(pass) ->
    post-edit` passes its check and then REWRITES the text, so the badge cleared on a body
    no judge ever saw. The LQA StepRun stores the judged blob verbatim (`input_text` ==
    `output_text` == the mode's working text at the moment of the verdict), so the
    write-back can compare it, unit by unit, with the text it is about to commit.

    The LAST verdict row is the right one: `_standing_gate_verdicts` keeps the last row per
    step, so when the run's outcome is `passed` (no check standing FAILED) the newest
    verdict row IS a standing PASS -- the most recent text any check approved.

    GATE C ROUND 4, M1 -- SELECT the newest IN-SCOPE row; do not BAIL on the newest row of
    any type. Round 3 read the newest verdict row of ANY kind and returned None when it was
    not `lqa`, which is a different scope from `_standing_gate_verdicts` (which skips PAST a
    foreign row and keeps looking). The two readers of one table disagreed, so a `qe_gate`
    step placed anywhere AFTER the check blocked the release permanently -- even when it
    rewrote nothing and the check had judged the exact bytes about to be committed, and the
    same three steps in the other order released. The scope now lives in ONE place
    (`_verdict_family`) and both readers express it identically."""
    rows = session.exec(
        select(
            StepRun.output_text,
            WorkflowStep.step_type,
            WorkflowStep.qe_gate,
            StepRun.step_id,
            StepRun.outcome_index,
        )
        .join(WorkflowStep, WorkflowStep.id == StepRun.step_id, isouter=True)
        .where(
            StepRun.workflow_run_id == run_id,
            StepRun.gate.in_(("passed", "failed")),
        )
        .order_by(StepRun.id.desc())
    ).all()
    families = scope or _DEFAULT_VERDICT_SCOPE
    for text, step_type, qe_gate, step_id, visit in rows:
        if _verdict_family(step_type, qe_gate) in families:
            if with_indices:
                run = session.get(WorkflowRun, run_id)
                key = f"{step_id}:{visit}"
                excluded = (run.context or {}).get("lqa_excluded_indices", {}).get(key)
                # Read legacy runs too; never write their document-sized lists.
                indices = (run.context or {}).get("lqa_judged_indices", {}).get(key)
                if excluded is not None:
                    omitted = set(excluded)
                    size = len((text or "").split(SEG_JOINER)) + len(omitted)
                    indices = [j for j in range(size) if j not in omitted]
                return text or "", indices
            return text or ""
    return (None, None) if with_indices else None


def _release_scope(held_families) -> frozenset:
    """The verdict families that may release a unit held by `held_families`.

    GATE C ROUND 4, M2. `lqa` is always in scope -- a document check that passes over the
    unit's new text releases whatever held it, which is the behaviour every surface
    documents and the only exit an `lqa` hold has ever had. A unit held by a QE step's own
    correction loop (ERRATUM E1) ALSO accepts that step's family, so the workflow that
    recorded the hold can release it by running clean. A unit held by BOTH gets both."""
    return frozenset({_GATE_FAMILY_LQA}) | {f for f in (held_families or ()) if f}


def _lqa_step_error(card) -> str:
    """The StepRun.error summary line for an LQA visit — the run surface `run_view.html`
    renders as a badge and the CLI shows on the step row.

    PROPOSAL §4.B "Audit trail": adjudicated findings must be VISIBLE in the run
    surfaces, and this phase deliberately adds NO storage schema. So the count rides the
    existing summary string, appended ONLY when non-zero -> byte-identical for every run
    that adjudicated nothing."""
    line = f"LQA score {card['score']} · {card['gate']}"
    # feat/lqa-configurable-threshold: show the configured pass bar ONLY when it
    # deviates from the built-in default (90) -> byte-identical for every existing /
    # unset step, and self-explaining for a step that gates on e.g. 94.
    thr = card.get("threshold")
    if thr is not None and thr != lqa.LQA_PASS_THRESHOLD:
        line += " · gate >=%d" % thr  # noqa: UP031
    n = _adjudicated_count(card)
    if n:
        line += " · %d adjudicated" % n  # noqa: UP031
    # #56/F4: name the REASON the gate is held when a configured judge did not deliver a
    # verdict, so StepRun.error / run_view / CLI history explain the needs_review (a bare
    # "score 100 · needs_review" reads like a mystery). Conditional -> byte-identical for
    # every OK/offline step (no marker key).
    if card.get("llm_judge") == lqa.JUDGE_UNAVAILABLE:
        line += " · judge unavailable"
    return line


def _lqa_event_payload(card) -> dict:
    """The `lqa` sub-dict of the `step` SSE event (score/gate/errors), plus the
    adjudication count when non-zero. `errors` stays the FULL count — nothing vanishes,
    an adjudicated finding is still a finding — and the extra key flows to the CLI's
    `--json` step summary for free (it copies `ev["lqa"]` verbatim)."""
    payload = {"score": card["score"], "gate": card["gate"], "errors": len(card["errors"])}
    # feat/lqa-configurable-threshold: carry the configured pass bar ONLY when it
    # deviates from the default (90), so an existing run's `lqa` event is
    # byte-identical while a configured step surfaces its gate to the CLI/UI.
    thr = card.get("threshold")
    if thr is not None and thr != lqa.LQA_PASS_THRESHOLD:
        payload["threshold"] = thr
    n = _adjudicated_count(card)
    if n:
        payload["adjudicated"] = n
    # #56: surface an UNAVAILABLE judge on the STEP so the CLI/UI shows WHY the gate is
    # needs_review (a configured judge did not deliver a verdict — not a clean pass).
    # Conditional, so an OK/offline step's event stays byte-identical (JUDGE_OK/SKIPPED
    # never emit this key), matching the existing "score/gate/errors" event-shape tests.
    if card.get("llm_judge") == lqa.JUDGE_UNAVAILABLE:
        payload["llm_judge"] = lqa.JUDGE_UNAVAILABLE
    return payload


def _render_cfg_for(step) -> dict:
    """Resolve THIS step's per-step render config (LAW-TERM-14 — per-step, no run-level
    resolver, no cross-step inheritance). `token_cap` 0 falls back to the install
    `glossary_prompt_token_cap` (default 1500, LAW-TERM-13)."""
    on = bool(getattr(step, "concept_glossary_render", False))
    cap = _int_or(getattr(step, "glossary_concept_cap", 10), 10)
    tcap = _int_or(getattr(step, "glossary_token_cap", 0), 0)
    if tcap <= 0:
        from app.services import settings as _settings

        tcap = _int_or(_settings.get("glossary_prompt_token_cap"), 1500)
    return {"on": on, "concept_cap": cap, "token_cap": tcap}


def _render_glossary_block(concepts, terms, render_cfg):
    """The per-step glossary seam shared by both run modes (residue [8]/[10]/[11]). Returns
    `(gloss_text, shown_terms, mt_terms)`:

      * OFF (or no concept data): `gloss_text = prompts.glossary_line(terms)`,
        `shown_terms = None`, `mt_terms = terms` — byte-identical to main (LAW-TERM-1/14).
      * ON: `gloss_text` = the audience(`llm`)-gated, ordered, capped Markdown block;
        `shown_terms` = exactly what survived (feeds the gates + the audit, LAW-TERM-15);
        `mt_terms` = the five-key projection of the audience(`mt`)-gated matches (the plain-
        MT/side-channel `payload["glossary"]`, uncapped — H7/residue [10]).

    Pure (plain dicts only) so it is safe on the MT worker thread inside
    `_run_step_on_segment` (residue [15])."""
    if not (render_cfg and render_cfg.get("on")) or concepts is None:
        return prompts.glossary_line(terms), None, terms
    llm_kept = glossary_concepts.filter_by_audience(concepts, "llm")
    gloss_text, shown = glossary_concepts.render_for_llm(
        llm_kept,
        concept_cap=render_cfg["concept_cap"],
        token_cap=render_cfg["token_cap"],
    )
    mt_terms = _five_key(glossary_concepts.filter_by_audience(concepts, "mt"))
    return gloss_text, shown, mt_terms


def _bound_findings_block(
    block: str,
    seg_source: str = "",
    seg_target: str = "",
    cap: int = LQA_FINDINGS_SEGMENT_CAP,
    *,
    excluded_findings: frozenset[str] = frozenset(),
    relevant_findings: frozenset[str] | None = None,
) -> str:
    """`block` bounded to `cap` chars for ONE segment's prompt (GitHub #45).

    Multi-segment callers supply exclusions computed against the whole document:
    exclude only findings matching another segment but not this one. Prose matching
    is a stopgap; LQAResult.span is the structured attribution source for a follow-up.
    Single-unit callers supply no exclusions to retain the existing behavior.
    Otherwise BYTE-IDENTICAL when the block already fits. An OVER-CAP block is bounded:
      * findings whose quoted text occurs in this segment's source/target come FIRST
        (stable within each group, so document order is otherwise preserved);
      * the original finding NUMBERS are kept, never renumbered — they are the
        traceable identity of a finding on the scorecard the operator can open;
      * meta lines render_findings appended (adjudication / its own truncation note)
        are kept, including when filtering removes every finding (except when
        the metadata itself exceeds the cap);
      * the model is TOLD that findings were elided, in the block itself. A fixer that
        silently receives a partial picture is exactly the failure mode
        `render_findings`' own "(+N more truncated)" marker exists to avoid."""
    block = block or ""
    # Missing segment text cannot justify deletion, even with caller exclusions.
    if excluded_findings and (seg_source or seg_target):
        lines = block.split("\n")
        if len(lines) < 3:
            raise ValueError("Segment findings exclusions require a fenced block")
        head, tail, body = lines[0], lines[-1], lines[1:-1]
        retained = [ln for ln in body if ln not in excluded_findings]
        n = len(body) - len(retained)
        if n:
            note = f"({n} finding{'' if n == 1 else 's'} excluded as belonging to other segments)"
            block = "\n".join([head, *retained, note, tail])
    if len(block) <= cap:
        return block
    lines = block.split("\n")
    if len(lines) < 3:
        return _cap_keep_fence(block, cap)
    head, tail, body = lines[0], lines[-1], lines[1:-1]
    findings = [ln for ln in body if _FINDING_LINE_RE.match(ln)]
    meta = [ln for ln in body if not _FINDING_LINE_RE.match(ln)]

    # #163: the CALLER's attribution decides the order when it has one. It is span-first,
    # and it is the same map the exclusions above were computed from, so which findings are
    # ALLOWED onto this segment and which survive the cap can never disagree. Falling back
    # to the prose predicate here would re-introduce the bug one layer down: a finding whose
    # prose is unattributable but whose span names THIS segment would be correctly admitted
    # and then sorted into the tail and truncated away, so the fix would change who is
    # allowed a finding without changing who receives it.
    def _relevant(ln):
        if relevant_findings is None:
            return _findings_relevant(ln, seg_source, seg_target)
        return ln in relevant_findings

    ordered = [ln for ln in findings if _relevant(ln)] + [
        ln for ln in findings if not _relevant(ln)
    ]
    kept: list[str] = []
    for ln in ordered:
        # Budget the CANDIDATE block, not the raw line, so head/tail/meta/marker are all
        # inside the bound (the same accounting render_findings uses).
        note = (
            [
                "(+%d more finding%s omitted for this segment)"  # noqa: UP031
                % (len(ordered) - len(kept) - 1, "" if len(ordered) - len(kept) - 1 == 1 else "s")
            ]
            if len(kept) + 1 < len(ordered)
            else []
        )
        cand = "\n".join([head] + kept + [ln] + note + meta + [tail])  # noqa: RUF005
        if len(cand) > cap:
            break
        kept.append(ln)
    dropped = len(ordered) - len(kept)
    out = [head] + kept  # noqa: RUF005
    if dropped:
        out.append(
            "(+%d more finding%s omitted for this segment)" % (dropped, "" if dropped == 1 else "s")  # noqa: UP031
        )
    out += meta + [tail]  # noqa: RUF005
    res = "\n".join(out)
    if len(res) > cap:
        # Nothing fit (a pathological head/meta). Keep the FENCE and the honest count —
        # a bounded, well-formed, empty block beats an over-cap one.
        res = "\n".join(
            [head, f"(all {len(findings)} findings omitted for this segment)"]
            + [ln for ln in meta if "excluded as belonging to other segments)" in ln]
            + [tail]
        )
    return _cap_keep_fence(res, cap)


def _resolve_step_template(session: Session, step) -> "prompts.ResolvedPrompt":
    """Resolve THIS step's effective prompt (SPEC-prompt-template-runtime), the ONE seam
    both orchestrator loops (FULL_TEXT + segmented) call so the two paths can never drift.
    Honors the owner's precedence via the pure prompts.resolve_effective_prompt: inline
    prompt_text wins; else the bound PromptTemplate's body; else DEFAULT_PROMPTS.

    The template is looked up ONLY when it could actually win (inline empty) -- an inline
    prompt short-circuits, so a set inline prompt costs no query and a dangling/deleted
    bound id under it raises no note. A dangling/invalid binding falls back to the default
    with an honest note (never a crashed run); package_view uses the SAME pure resolver so
    the panel's prompt fact reports the identical resolved source."""
    default_text = DEFAULT_PROMPTS.get(step.step_type, "{text}")
    tid = getattr(step, "prompt_template_id", None)
    exists, body, name = False, None, None
    if tid and not (step.prompt_text or "").strip():
        from app.models import PromptTemplate

        tmpl = session.get(PromptTemplate, tid)
        if tmpl is not None:
            exists, body, name = True, tmpl.body, tmpl.name
    return prompts.resolve_effective_prompt(
        step.step_type,
        step.prompt_text,
        default_text=default_text,
        template_id=tid,
        template_exists=exists,
        template_body=body,
        template_name=name,
    )


def document_has_no_content(session: Session, doc: Document, locale: str) -> bool:
    """T1-05: True when a document has nothing to translate for `locale` — a
    SEGMENTED doc with no segments in that locale, or a FULL_TEXT doc whose assembled
    source is blank/whitespace-only. The submit surfaces reject on this (clean, up
    front); the run-time guard below is the durable backstop for every surface."""
    if doc.mode == TransMode.SEGMENTED:
        return not _load_segments(session, doc.id, locale)
    return not _source_text(doc).strip()


def _empty_document_run(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None,
    created_by: str = "",
):
    """T1-05: fail a run cleanly when the document has no translatable content.
    Fetches (resume) or creates (fresh) the WorkflowRun row, marks it FAILED with the
    shared EMPTY_DOCUMENT_MSG, and yields run_started + error so EVERY surface (web
    SSE, run_workflow/resume summaries, CLI) reports the reason instead of a bare
    'failed'. `created_by` attributes a FRESH (run_id None) empty run to its launcher
    (jobs-history)."""
    if run_id is not None:
        run = session.get(WorkflowRun, run_id)
    else:
        run = WorkflowRun(
            workflow_id=workflow_id,
            document_id=doc_id,
            target_locale=locale,
            created_by=created_by or "",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
    run.status = RunStatus.FAILED
    run.finished_at = datetime.utcnow()
    # GitHub #106 sweep (gate F round 1, H2 asked for one): this was the LAST terminal
    # write that left nothing on the row. The reason rides the yielded "error" event, which
    # every LIVE surface shows -- but that event is gone by the time anyone reads the run
    # back, and this path writes no StepRun either, so `_failure_reason` fell through to
    # empty notes and `GET /api/runs/{id}` reported a bare `failed`. It is NOT a reconciler
    # sentence, so `run_reason` still (correctly) reports None for it -- this run failed for
    # a cause the ordinary error surfaces can state.
    if EMPTY_DOCUMENT_MSG not in (run.notes or ""):
        _stamp_note(run, EMPTY_DOCUMENT_MSG)
    session.add(run)
    session.commit()
    yield {
        "type": "run_started",
        "run_id": run.id,
        "steps": len(get_steps(session, workflow_id)),
        "start": 0,
    }
    yield {"type": "error", "run_id": run.id, "error": EMPTY_DOCUMENT_MSG}


def _paused_blob(session: Session, run_id: int) -> str | None:
    """T1-02: the assembled working text captured at the LAST human_review pause
    (its StepRun.input_text = SEG_JOINER.join(working) at pause time). Used to resume
    with 'no edits' when the reviewer submits an empty box — the produced MT is kept
    instead of being discarded. Returns None if there is no paused StepRun.

    'LAST' = highest StepRun.id (newest-row-wins): a Phase-3 loop can pause the SAME
    human_review step more than once (multiple PAUSED rows share order_index), so id-
    desc selects the CURRENT visit's blob, matching run_view.html's step_runs[-1]."""
    sr = _latest_paused_steprun(session, run_id)
    return sr.input_text if sr is not None else None


def repark_run_at_external_step(
    session: Session, run: WorkflowRun, step_id: int, locale: str
) -> bool:
    """Vendor Wave E R4: re-PARK a run at its EXTERNAL step so a rework re-dispatch can be
    admitted + resumed by the SAME machinery a first-time dispatch uses. A rework decision
    happens AFTER a completing return has already resumed the run (possibly to completion), so
    this re-establishes the parked state:

      * sets `current_step_index` to the external step's cursor (its position in the ordered
        step list) -- the resume entrypoints then advance PAST it exactly as they do for a
        first dispatch, so the reworked content is spliced in and the run continues onward;
      * writes a fresh PAUSED StepRun for that step whose `input_text` is the CURRENT working
        blob (SEG_JOINER.join of the segments' stored targets) -- the seed a return splices
        into via `_aligned_slots`;
      * flips the run PAUSED and clears `finished_at` (reopening a COMPLETED run is intended
        here -- a person/agent explicitly asked to rework it; `_mark_execution_start` will
        reset the staleness clock when the rework return resumes it).

    Returns True on success, False when the step is not in the run's workflow (defensive). The
    caller has already recorded the rework dispatch leg tied to (run, step); this only
    reopens the run so that leg's return has somewhere to land."""
    steps = get_steps(session, run.workflow_id)
    cursor = next((i for i, s in enumerate(steps) if s.id == step_id), None)
    if cursor is None:
        return False
    doc = session.get(Document, run.document_id) if run.document_id else None
    segments = _load_segments(session, doc.id, locale) if doc is not None else []
    blob = SEG_JOINER.join((s.target_text or s.source_text or "") for s in segments)
    run.status = RunStatus.PAUSED
    run.finished_at = None
    run.current_step_index = cursor
    session.add(run)
    session.add(
        StepRun(
            workflow_run_id=run.id,
            step_id=step_id,
            status=RunStatus.PAUSED,
            input_text=blob,
            output_text="",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    session.commit()
    return True


def paused_review(session: Session, run_id: int) -> dict | None:
    """Issue #23 (a): an inspectable, machine-usable view of a PAUSED run's human-
    review state, so a headless operator can read (and safely edit) the text-to-review
    without ever hand-touching the SEG_JOINER delimiter.

    Returns None when the run does not exist or is NOT paused (a terminal/running run
    has no live review to hand out — historical PAUSED StepRun rows persist forever, so
    the run.status gate is what makes this precise). Otherwise returns the run status,
    the paused step's identity (key/name/type/index), and — the load-bearing part —
    the review blob split into an explicit `segments` array (one string per boundary,
    in order_index order). A script edits segments[i] individually and resumes via
    `--edited-segments`, never assembling or risking the delimiter. For a FULL_TEXT run
    the blob carries no delimiter, so `segments` is a single-element array (count 1).

    Mirrors run_view.html's read (step_runs[-1].input_text) via the shared newest-
    PAUSED-row selector — the CLI/api read the SAME state the web page shows; no new
    state is introduced."""
    run = session.get(WorkflowRun, run_id)
    if run is None or run.status != RunStatus.PAUSED:
        return None
    sr = _latest_paused_steprun(session, run_id)
    if sr is None:
        return None
    step = session.get(WorkflowStep, sr.step_id)
    blob = sr.input_text or ""
    segments = blob.split(SEG_JOINER)
    return {
        "run_id": run.id,
        "status": run.status,
        "workflow_id": run.workflow_id,
        "document_id": run.document_id,
        "target_locale": run.target_locale,
        "current_step_index": run.current_step_index,
        "paused_step": {
            "index": step.order_index if step else run.current_step_index,
            "key": (step.key if step else None) or None,
            "name": (step.name if step else None) or "Human review",
            "type": step.step_type if step else StepType.HUMAN_REVIEW,
        },
        "review_text": blob,
        "segments": segments,
        "segment_count": len(segments),
        "delimiter": SEG_JOINER,
    }


def waiting_human_review(session: Session, document_id: int, target_locale: str) -> dict | None:
    """#617 item 4: the human_review pause this document+locale is waiting on, or None.

    The newest PAUSED run of the document for that locale whose paused step is a HUMAN_REVIEW
    step (an external/vendor pause is a different wait and returns None). Returns the run id,
    the step name and `segments`, the pending MT as one string per segment in order_index order
    (the review blob the run holds until Approve and resume). Read-only: it reuses
    `paused_review`, so the editor, /api and the CLI see exactly what the run page shows."""
    if not target_locale:
        return None
    run = session.exec(
        select(WorkflowRun)
        .where(
            WorkflowRun.document_id == document_id,
            WorkflowRun.target_locale == target_locale,
            WorkflowRun.status == RunStatus.PAUSED,
        )
        .order_by(WorkflowRun.id.desc())
    ).first()
    if run is None:
        return None
    review = paused_review(session, run.id)
    if review is None or review["paused_step"]["type"] != StepType.HUMAN_REVIEW:
        return None
    return {
        "run_id": run.id,
        "step_name": review["paused_step"]["name"],
        "segments": review["segments"],
    }


def _capture_resume_edits(
    session: Session, run_id: int, before: list, after: list, actor: str = ""
) -> None:
    """LLMemory capture seam 2 (PROPOSAL-llmemory §4.2): record ONE event per segment
    the operator actually CHANGED in a resume-with-edits submission.

    `before` is the paused review's per-segment blob (what the workflow produced),
    `after` is what the operator submitted; the lists are already count-validated by the
    caller, so index i is the same segment in both. UNCHANGED segments are never passed
    to `record_resume_edit` — the whole submission is re-posted on every resume, so
    treating it as N corrections would hand the distiller N-1 fabricated ones.

    Memory capture enforces its own switch. Distance capture is independent, like the
    CAT save, and uses the same episode upsert. A full-text body's backing segment is
    one full_text unit. Capture failures must never fail the operator's resume."""
    try:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            return
        segs = _load_segments(session, run.document_id, run.target_locale)
        doc = session.get(Document, run.document_id)
    except Exception:
        log.warning("postedit: resume_edit capture setup failed", exc_info=True)
        return
    for i, (b, a) in enumerate(zip(before, after, strict=False)):
        if (b or "") == (a or ""):
            continue
        seg_id = None
        try:
            seg_id = segs[i].id if i < len(segs) else None
            llmemory.record_resume_edit(
                session, run, b or "", a or "", actor=actor or "", segment_id=seg_id
            )
            if seg_id is not None:
                row = postedit.capture_guarded(
                    session,
                    segment=segs[i],
                    pre=b or "",
                    post=a or "",
                    actor_kind="human",
                    actor=actor or "",
                    unit_kind="full_text" if doc and doc.mode == TransMode.FULL_TEXT else "segment",
                )
                if row is not None and not db.commit_or_shed(
                    session, stage=lambda row=row: session.add(row), keep_loaded=True
                ):
                    log.warning(
                        "postedit: resume_edit PE capture dropped for segment %s: "
                        "sustained write contention (bounded retry exhausted)",
                        seg_id,
                    )
        except Exception:
            log.warning(
                "postedit: resume_edit capture failed for segment %s", seg_id, exc_info=True
            )
            # A failed outer commit can leave the session unusable for later segments.
            try:
                session.rollback()
            except Exception:
                log.debug("postedit: resume_edit capture rollback failed", exc_info=True)


def author_prompt(
    description: str, provider: str | None = None, org_id=None, model: str | None = None
) -> str:
    """Prompt-authoring assistant: turn a plain-language description into a step prompt.

    `model` threads the authoring model the SAME way the promptable-setup seam does
    (providers.resolve_default_model): a caller pick wins; blank resolves a built-in
    cloud default (or the first live model id) so a keyed provider whose
    DEFAULT_<X>_MODEL is empty no longer trips the empty-model guard; an engine:<id>
    ref keeps its own profile model. Without this, this authoring seam (a direct
    prov.complete() call at the time) passed NO model and hit the same empty-model
    failure the project/workflow authoring did.

    Raises DeadEngineError (unchanged, NOT swallowed) when `provider` names a dead
    engine:<id> ref (deleted/disabled/keyless/undecryptable) -- the workflow builder's
    provider dropdown includes engine:<id> refs, so this is reachable from real user
    input, not just misconfiguration. Unlike the spend-cap brake below (a recoverable,
    expected "not yet" that's returned as a draft-shaped string), a dead ref is a hard
    misconfiguration that both callers (web POST /workflows/{id}/author, CLI
    `workflow author-prompt`) surface as an actual error -- a clean error response /
    non-zero exit, matching every other DeadEngineError call site -- rather than one
    being silently accepted as HTTP 200 with the error text sitting in the draft box.

    `org_id` scopes the provider ref (MED-2): a foreign-org engine:<id> is refused
    as DeadEngineError BEFORE resolution — indistinguishable from a dead ref, never
    enumerable. No-op when the multi-org gate is off (org_id None)."""
    assert_provider_visible(provider, org_id)
    prov = get_provider(provider)
    auth_model = resolve_default_model(provider, model, org_id)
    meta = (
        "Write a concise, effective instruction prompt for one step of a localization "
        f"workflow, described as: {description}. The prompt may use the placeholders "
        "{source_locale}, {target_locale}, {glossary}, and {text}. Output only the prompt."
    )
    # Spend cap: over cap on a REAL engine -> return the terminal message as the draft
    # rather than calling (the caller surfaces it as an HTTP-200 draft, a recoverable
    # "not yet"). Mock is exempt. This pre-check mirrors run_completion's own per-target
    # spend brake below; it fires first to preserve the return-as-draft semantics (vs
    # run_completion collapsing a block into an error dict).
    from app.services import spend as _spend

    block_msg = _spend.blocked_for(prov)
    if block_msg:
        return block_msg
    # Route through run_completion (NOT a direct prov.complete): a step prompt drafted
    # from a one-line description is a LARGER output than input, so the input-sized
    # heuristic truncated it on the direct call (the LIVE 502 sibling of the setup-draft
    # bug). run_completion gives escalate-on-truncation (grow the budget toward the model
    # ceiling), Prompt-Tap capture of this call, and an accurate one-time ledger stamp --
    # none of which the old direct call had. We seed a generous STARTING budget so the
    # first attempt is not truncated; escalation covers anything larger. On a genuine
    # at-ceiling overflow we RE-RAISE ProviderTruncated so both callers (web POST
    # /workflows/{id}/author, CLI `workflow author-prompt`) still format their clean
    # truncation message -- the loud-failure contract is preserved, not swallowed.
    spec = ProviderSpec(provider=provider or config.DEFAULT_PROVIDER, model=(auth_model or None))
    start_budget = min(config.model_output_ceiling(auth_model or None), 16384)
    res = run_completion(
        spec,
        meta,
        task="author_prompt",
        payload={"description": description},
        tap_meta={"task_origin": "author_prompt"},
        max_tokens=start_budget,
    )
    if res["error"] is not None:
        if res.get("truncated"):
            # Escalation reached the model's output ceiling and the draft STILL
            # overflowed. Re-raise the typed error the callers format specifically.
            raise ProviderTruncated(res["error"])
        # Any other failure — HTTP 401 (bad key), connection-refused to a local LM
        # server, a malformed response body, or an NMT engine with no prompt channel
        # (NmtUnsupported) — must reach the user as a clean message, not a raw 500.
        # (DeadEngineError is raised earlier by get_provider, before run_completion.)
        raise ProviderCallError(
            f"The authoring provider could not complete the request: {res['error']}"
        )
    # run_completion recorded spend once on the successful attempt -- do not re-stamp.
    return _clean_draft(res["text"])
