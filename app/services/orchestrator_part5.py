"""orchestrator: part5

Documented in: docs/architecture/translation-pipeline.md
"""

from sqlmodel import Session

from app import extensions as _extensions
from app.db import POOL_EXHAUSTED_NOTE
from app.models import (
    RunStatus,
    StepType,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    segment,
)
from app.services.orchestrator_part2 import (
    _RC_OPS,
    _RC_REFS,
    QUEUE_LAUNCH_FAILED_NOTE,
    RESTART_NOTE,
    STALE_NOTE,
    SUPERSEDED_ABORT_NOTE,
    WRITE_CONTENTION_NOTE,
    ResumeMismatch,
    _is_step_ref,
    _lang_directive,
    _refuse_archived,
    _step_ref,
)
from app.services.orchestrator_part3 import (
    _capture_resume_edits,
    _gate_hold_message,
    _is_correction_loop_step,
    _standing_gate_verdicts,
    paused_review,
)
from app.services.orchestrator_part4 import (
    CANCEL_NOTE,
    RunGuard,
    _latest_outcome_steprun,
    _refuse_project_budget,
)
from app.services.orchestrator_terminology_backen import (
    _LQA_FINDINGS_PH,
    _LQA_FINDINGS_PREAMBLE,
    _SYSTEM_HEAD,
    _SYSTEM_IDENTITY,
    _SYSTEM_MASKED_CLAUSE,
    _SYSTEM_NO_TOKENS_CLAUSE,
    _SYSTEM_RAW_CLAUSE,
    _SYSTEM_TAIL,
    GATE_HOLD_LOOP_EXHAUSTED,
    GATE_HOLD_WARNING_CODE,
    SEG_JOINER,
    _end_run_cursor,
    _is_gate_step,
    _memory_mode_off,
    _verdict_family,
)

# Optional add-on: reached through the extension facade (no-op when absent); name unchanged.
llmemory = _extensions.use("memory")


def _system_for(
    tag_map,
    text: str | None = None,
    *,
    src_locale: str = "",
    tgt_locale: str = "",
    head_body: str | None = None,
) -> str:
    """The step system message: the language pair, then ONLY the marker notation THIS
    call will actually see.

    THREE notation cases, keyed on the SEGMENT's own tokens:
      * `tag_map` non-empty -> the text was MASKED, so describe sentinels and never show
        a ⟦⟧ literal the model could imitate;
      * empty map, text HAS ⟦⟧ tokens -> the raw notation clause (mask off);
      * empty map, text has NO tokens -> describe no notation at all (see the section
        comment above: this case used to fall into the raw clause and taught tokens to
        text that had none).

    `text` is the segment's PRE-MASK working text; when `tag_map` is non-empty the
    masked branch wins regardless, so passing either form is equivalent there. Callers
    that pass NO locales get a string byte-identical to the pre-#43 constants.

    `head_body` (GitHub #356 phase 2a): an org admin's override of the EDITABLE part of the
    head, already resolved and validated by the caller. None (the default, and the only
    value while the feature flag is off) keeps the built-in head, byte-identical. An
    override replaces ONLY the body: the identity sentence, the language pair and the
    notation clause are composed here in code around it, so no override can remove them."""
    if tag_map:
        clause = _SYSTEM_MASKED_CLAUSE + _SYSTEM_TAIL
    elif text is None or segment.has_marker_chars(text):
        # `text is None` = a caller with nothing to inspect: keep the honest raw default
        # (the pre-#43 behavior), never the "there are no markers" assertion.
        clause = _SYSTEM_RAW_CLAUSE + _SYSTEM_TAIL
    else:
        clause = _SYSTEM_NO_TOKENS_CLAUSE
    head = _SYSTEM_HEAD if head_body is None else _SYSTEM_IDENTITY + head_body.strip() + " "
    return head + _lang_directive(src_locale, tgt_locale) + clause


def _system_prompt_stamp(resolved, src_locale: str, tgt_locale: str) -> dict:
    """The StepRun columns that record which system-prompt HEAD an AI step visit ran with
    (GitHub #356 phase 2a, decision 8): the editable key, the override version in force (0 =
    the built-in head), which SCOPE won (phase 2b, #365: "workflow", "base_workflow", "org" or
    "builtin") and the sha256 of the COMPOSED step system text (head + language pair + the
    plain-text marker clause, for the run's language pair; the per-segment marker sentence and
    fence/retry sentences vary per call and are not part of the digest). Never the text itself.
    `resolved` is the run's `Resolved` override or None.

    EMPTY while the feature flag is off, so a flag-off install writes nothing new and its
    step-run rows stay byte-identical."""
    from app.services import system_prompt_overrides as _spo

    if not _spo.enabled():
        return {}
    composed = _system_for(
        {},
        "",
        src_locale=src_locale,
        tgt_locale=tgt_locale,
        head_body=None if resolved is None else resolved.body,
    )
    return {
        "system_prompt_key": _spo.KEY_STEP_HEAD,
        "system_prompt_version": 0 if resolved is None else resolved.version,
        "system_prompt_sha256": _spo.sha256_hex(composed),
        "system_prompt_scope": _spo.SCOPE_BUILTIN if resolved is None else resolved.scope,
    }


def _memory_eligible_step(step, steps, template_text: str = "") -> bool:
    """THE RECALL GATE (PROPOSAL-llmemory §5 "translate steps only", §2 "no recall into
    LQA judge or fixer"). True only for a step that is doing ORIGINAL TRANSLATION — with
    the fixer carve-out now opt-in behind a default-OFF flag (Train D §7.3).

    THREE exclusions, in the order they are cheapest to check (exclusions 2 and 2b are
    lifted TOGETHER when `llmemory_fixer_recall_enabled` is on — Train D §7.3/E25; exclusion
    1 is never lifted, so the judge stays blind):

    1. NOT AN llm_agent STEP. `human_review` runs no engine at all, and an `lqa` step
       takes its own branch in both run loops (it calls `lqa.score_text` directly and
       `continue`s) — it can never reach `_run_step_on_segment`. The predicate states
       the rule anyway rather than relying on that control flow: the JUDGE MUST STAY
       MEMORY-BLIND is the whole reason Phase 1 can measure whether memory helped, and
       that guarantee should not depend on the shape of an `if` fifteen hundred lines
       away. (The QE judge is likewise structurally safe: `qe.score_segment` builds its
       own prompt and is never handed a memory block.)

    2. THE CORRECTION-LOOP FIXER, via the repo's EXISTING role predicate
       `_is_correction_loop_step` — a step gated on `prev_lqa:failed`, or the target of
       an LQA `on_fail` branch. This is the one judgement call in the gate, so: the
       fixer's job is to apply THIS card's findings to THIS text, and it is the step the
       repo already treats as the risky path (the D1 TM provenance guard protects TM
       segments from exactly this shape). Adding a second, independently-derived context
       source to it would (a) put memory in competition with the findings it is supposed
       to be executing, and (b) close a feedback loop — LQA findings become memories,
       memories steer the fixer, the fixer's output is judged by the next LQA — that
       Phase 1 has no instrument to measure. Reusing the EXISTING predicate rather than
       inventing a second definition of "fix step" is deliberate: one definition means
       the step whose TM segments are protected, the step that receives findings, and
       the step denied memory can never drift apart.

    2b. A STEP WHOSE TEMPLATE CONSUMES `{lqa_findings}` (adversarial review S13). The
       predicate above recognises a fixer by its WIRING — a prev_lqa:failed gate, or
       being an LQA on_fail branch target. But a step whose prompt embeds the
       `{lqa_findings}` placeholder is handed the failing card's findings by the
       prompt-package engine and is a fixer in every way that matters, whatever its
       wiring says; it was memory-eligible purely because nobody had written the third
       shape down. The exclusion is about ROLE, not plumbing: any step reading LQA
       findings is executing a correction, and both reasons in (2) — memory competing
       with the findings it should be applying, and the LQA -> memory -> fixer -> LQA
       feedback loop Phase 1 cannot measure — apply to it identically.

    3. THE STEP-LEVEL OPT-OUT (`memory_mode == "off"`), checked LAST because it is an
       opt-out only: it can subtract a step from an eligible set, never add one.

    Everything else — the plain "Translator" step, an in-country-reviewer step, a polish
    step not wired into a correction loop — is eligible. FULL_TEXT mode was out of scope
    for Phase 1 entirely (proposal §5: it had no auxiliary blocks); Train D adds a
    FULL_TEXT recall path behind the default-OFF `llmemory_fulltext_recall_enabled` flag
    (§7.2), and the FULL_TEXT loop now consults THIS SAME predicate (a FULL_TEXT translate
    step, never a fix/judge step). This predicate is otherwise mode-agnostic — it decides a
    step's ROLE, not which loop is running."""
    if getattr(step, "step_type", None) != StepType.LLM_AGENT:
        return False
    # Train D §7.3/E25: `llmemory_fixer_recall_enabled` (default OFF) lifts BOTH fixer
    # exclusions — the wiring-based one (2) AND the {lqa_findings}-template one (2b) —
    # together, so the flag's behavior never depends on how a given fixer was wired.
    # OFF (the default) keeps both exclusions and the gate is byte-identical to Phase 1.
    # Exclusion 1 (not an llm_agent step) is NEVER lifted, so the LQA/QE judge stays
    # memory-blind regardless of this flag (LAW 5).
    if not llmemory.fixer_recall_enabled():
        if _is_correction_loop_step(step, steps):
            return False
        # `template_text` is the RESOLVED effective prompt (inline > bound template >
        # default) the caller already computed; the inline field is checked too so a caller
        # that has not resolved anything still gets the common shape right.
        if _LQA_FINDINGS_PH in ((template_text or "") + (getattr(step, "prompt_text", "") or "")):
            return False
    return not _memory_mode_off(step)


def _record_gate_hold(session: Session, run, *, step, step_index: int, cause: str) -> str:
    """Record the run-level GATE HOLD on `run.context` and return its message.

    ONE durable record, read by all four surfaces: the web run page renders
    `run.context["warnings"]` (`run_view.html`), `GET /api/runs/{id}` returns it in its
    DEFAULT payload (so MCP `work_status`, which is a thin client of that route, gets it
    too) and `workflow show-run`'s DEFAULT human output prints it (`! <message>`). That is
    the four-surface signal gate C round 1 M2 asked for -- a `StepRun.error` string is
    visible on neither of the two default headless views.

    `run.context["gate_hold"]` is the machine-readable twin the segment write-back reads to
    flag the units it is about to commit. De-duped on (code, step_index) so a loop that
    re-enters the same gate cannot stack banner lines.

    `family` (gate C round 4, M2) records WHICH KIND of check held -- `lqa` for a document
    check, `qe` for a QE-gated step whose own correction loop ended (ERRATUM E1). It rides
    the run record and the unit's badge so the RELEASE can match the family that held; a QE
    hold was otherwise unreleasable by the workflow that created it."""
    message = _gate_hold_message(step, cause)
    family = _verdict_family(getattr(step, "step_type", None), getattr(step, "qe_gate", False))
    ctx = dict(run.context or {})
    warnings = list(ctx.get("warnings") or [])
    if not any(
        w.get("code") == GATE_HOLD_WARNING_CODE and w.get("step_index") == step_index
        for w in warnings
    ):
        warnings.append(
            {
                "code": GATE_HOLD_WARNING_CODE,
                "step_index": step_index,
                "step_name": getattr(step, "name", None) or getattr(step, "step_type", ""),
                "cause": cause,
                "family": family,
                "message": message,
            }
        )
    ctx["warnings"] = warnings
    # First hold wins: a run cannot be held twice, and the first gate to hold is the one
    # that stopped it.
    ctx.setdefault(
        "gate_hold",
        {
            "step_index": step_index,
            "step_name": getattr(step, "name", None) or getattr(step, "step_type", ""),
            "cause": cause,
            "family": family,
            "message": message,
        },
    )
    run.context = ctx
    session.add(run)
    return message


def _run_gate_outcome(session: Session, run_id: int, scope: frozenset | None = None) -> str | None:
    """This run's OWN standing gate verdict: `"passed"`, `"failed"`, or None when no gate
    in this run ever produced a verdict.

    Per gate-producing step, the LAST verdict-bearing StepRun row wins (a loop that failed
    and then passed stands at `passed`); the RUN passes only when at least one gate spoke
    and NO gate is left standing at `failed`.

    GATE C ROUND 2, H1 -- WHY THIS EXISTS. The gate hold is a LIVE predicate over the
    segments, so it needs a stale-badge rule: a badge describing text a later run has
    since replaced must clear, or the document would be undeliverable forever. Round 1
    cleared it on ANY later run that reached its write-back without a hold of its own,
    which handed every operator a one-run release requiring no reviewer and no
    `allow_held`: run the workflow again with a check that FAILS (or with no check at
    all) and the gate-failed text downloaded 200, wearing no badge -- issue #95's exact
    harm, reached through the very remedy the four surfaces documented ("or a later CLEAN
    run replaces the text"). *Clean* was the word that was never checked.

    THE RULE (ratified 2026-09-06): a standing hold is released by exactly two things --
    (a) a later run whose OWN gate PASSED over the unit's new text, or (b) a reviewer
    approving/editing every held unit (`_gate_held_reason_for_docs` skips APPROVED/EDITED
    rows). A later run with NO gate, with a FAILED gate, or that never reached its gate
    releases NOTHING; it may of course add its own hold.

    A projected read (step_id + gate only, verdict rows only), once per write-back."""
    standing = _standing_gate_verdicts(session, run_id, scope)
    if not standing:
        return None
    return "failed" if any(g == "failed" for g in standing.values()) else "passed"


def _findings_suffix(step, steps, template: str, findings_block: str) -> str:
    """The auto-append suffix ("\\n\\nMQM LQA findings to resolve:\\n<block>") for an
    llm_agent step in a correction-loop shape (gated on prev_lqa, or an LQA on_fail loop-
    back target) whose template does NOT carry {lqa_findings}. "" (byte-identical append)
    when it isn't such a step, already has the placeholder, or no LQA produced findings
    yet — so a step OUTSIDE those shapes only ever gets findings via the explicit
    placeholder, and an empty block appends NOTHING."""
    if not findings_block or _LQA_FINDINGS_PH in template:
        return ""
    if not _is_correction_loop_step(step, steps):
        return ""
    return _LQA_FINDINGS_PREAMBLE + findings_block


def _normalize_resume_text(text: str) -> str:
    """Canonicalize free-text review line endings before delimiter splits (#136)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def prepare_resume_seed(
    session: Session,
    run_id: int,
    *,
    edited_text: str | None = None,
    edited_segments: list | None = None,
    actor: str = "",
) -> str:
    """Issue #23 (b): validate an edited resume payload against the paused run's
    boundary count and return the seed_text to hand to resume_workflow — WITHOUT
    mutating any run state. This is the count check moved AHEAD of the state change so
    a mismatch is a clean refusal, never a terminal FAIL.

    - `edited_segments` (list[str]): the structured, delimiter-free path — count-
      validated against the paused blob, then joined with SEG_JOINER. A non-list, or a
      non-string element, is a ValueError (bad payload). This is what a script should
      use: it never hand-assembles a delimiter.
    - `edited_text` (str): the free-text path — a BLANK/whitespace-only value means
      'accept as-is' (returns "" unchanged, the byte-identical accept-MT behavior); a
      non-blank value is split on SEG_JOINER and count-validated.
    - neither given -> "" (accept-as-is).

    On a count mismatch raises ResumeMismatch (run untouched). Callers translate that
    into a CliError / 4xx and leave the run PAUSED + re-resumable.

    `actor` (LLMemory) is attribution only: it labels the capture events written for the
    segments the operator CHANGED (see `_capture_resume_edits`). It is never used for
    any authorization decision.

    EVERY CALLER MUST PASS IT (adversarial review S20). The default exists so a script
    or a test can call this without inventing an identity, but it shipped with NO caller
    passing anything — so every resume-derived memory event was anonymous, and the whole
    provenance layer (whose correction taught this?) was blank on one of its three
    seams, while the editor-save seam beside it was correctly attributed. The three real
    entry points (the /api resume route, `workflow resume`, the web resume form) each
    know the acting identity and pass it. If you add a fourth, pass it too: an
    unattributed event is not a cheaper event, it is a broken receipt.

    LLMemory NOTE ON PLACEMENT: capture happens HERE, after count validation and before
    the return, because this is the only place that holds BOTH halves of the diff — the
    paused blob and the submitted edits. It writes MemoryEvent rows only; the promise
    that this function does not mutate RUN STATE is unchanged."""
    view = paused_review(session, run_id)
    if view is None:
        # Not paused (or gone) — the caller's own not-paused guard should have caught
        # this; treat as a bad request rather than guessing a count.
        raise ResumeMismatch(f"run #{run_id} is not paused (nothing to resume).")
    expected = view["segment_count"]
    if edited_segments is not None:
        if not isinstance(edited_segments, list) or not all(
            isinstance(x, str) for x in edited_segments
        ):
            raise ValueError("edited_segments must be a JSON array of strings.")
        if len(edited_segments) != expected:
            raise ResumeMismatch(
                f"edited segment count {len(edited_segments)} != expected {expected} "
                f"-- the run is unchanged and still paused. Read the boundaries with "
                f"`workflow show-run {run_id}`."
            )
        _capture_resume_edits(session, run_id, view["segments"], edited_segments, actor=actor)
        return SEG_JOINER.join(edited_segments)
    if edited_text is None or not edited_text.strip():
        return ""  # accept-as-is (keep the produced MT); byte-identical path
    edited_text = _normalize_resume_text(edited_text)
    parts = edited_text.split(SEG_JOINER)
    if len(parts) != expected:
        raise ResumeMismatch(
            f"resume text segment count {len(parts)} != expected {expected} "
            f"-- the review delimiters were altered; the run is unchanged and still "
            f"paused. Use `--edited-segments <json>` (edit segments individually) or "
            f"read the boundaries with `workflow show-run {run_id}`."
        )
    # The free-text path carries the same edits, just delimiter-assembled: split it back
    # into the per-segment view the diff needs, so BOTH resume shapes capture identically
    # (a reviewer using the textarea is not a lesser source of evidence than one posting
    # --edited-segments).
    _capture_resume_edits(session, run_id, view["segments"], parts, actor=actor)
    return edited_text


def _branch_cursor(
    step: WorkflowStep,
    cursor: int,
    gate: str | None = None,
    key_index: dict | None = None,
    steps=None,
    guard: "RunGuard | None" = None,
) -> tuple[int, str | None]:
    """Post-step branch-resolution seam (SPEC §5.4). Returns `(next_cursor, hold_cause)`,
    where `hold_cause` is non-None when the run must END IN THE HELD POSTURE here rather
    than merely advance (see `_record_gate_hold`).

    SPEC-conditional-workflows D4 ERRATUM (gate C round 1, H2 -- ratified 2026-09-06):
    a loop-back REFUSED because its target sits at its `max_visits` ceiling used to fall
    through to `cursor + 1`. When the branch being refused is a GATE's `on_fail`, that
    fall-through runs every downstream step -- a `deliver` step included -- on text whose
    last verdict was FAILED, while the run reports `completed`. That is the default shape
    of an exhausted correction loop, not an exotic one (every step inherits
    `workflow_max_visits`), and it is the silent-wrong-output class. So a refused
    `on_fail` loop-back from a FAILED gate now ENDS THE RUN at the gate, held, through the
    same `_end_run_cursor` seam a judge outage uses. An `on_pass` loop-back, a dangling
    target and a non-gate step are all unchanged.

    GATE C ROUND 2, M2 (ratified 2026-09-06). ROUTING IS NOT THE POSTURE. The other
    FAILED endings -- `on_fail: "end"`, `on_fail: "next"`, a branch-less check, a dangling
    target -- also shipped their text at 200 while the run read `completed`, but the cure
    is NOT to stop the cursor here: `on_fail: "next"` into a `run_condition:
    prev_lqa:failed` fixer is the canonical Phase-2 correction shape (SPEC §4.1), and
    ending the run at the gate would delete it. The posture is decided ONCE, at the end of
    the run, off the STANDING VERDICT -- see `_terminal_gate_hold`. Routing below is
    exactly what §5.4 always said; the HOLD is what changed.

    Phase-1/2 default (a step with NO branch, or a step that produced no gate — a plain
    llm_agent, a skipped step): cursor + 1 (linear, byte-identical to `for i in range`).

    Phase 3: a gate-producing step (LQA / QE-gated) with a `branch` routes by its durable
    gate — on_pass/on_fail -> 'next' (cursor+1) | 'end' (finish the run) | {step:key}
    (jump to that step's index; an EARLIER index = a bounded loop-back). A loop-back into
    a step already at its max_visits is REFUSED so the loop ENDS. An unknown/dangling key
    (e.g. a since-deleted step) never crashes and never spins: on a PASS it falls through,
    on a FAIL it is an unrepaired ending like any other."""
    branch = getattr(step, "branch", None)
    if not branch or gate not in ("passed", "failed") or key_index is None:
        return cursor + 1, None
    target = branch.get("on_pass") if gate == "passed" else branch.get("on_fail")
    if target is None or target == "next":
        return cursor + 1, None
    if target == "end":
        return len(steps), None  # >= len(steps) exits the while -> completion
    if isinstance(target, dict) and "step" in target:
        tidx = key_index.get(target["step"])
        if tidx is None:
            return cursor + 1, None  # dangling target (deleted step) -> fail-safe forward
        if guard is not None and guard.at_visit_cap(steps[tidx]):
            # Target at its visit ceiling -> the loop is over. H2: when the loop we are
            # ending is a FAILED gate's repair loop, the standing verdict on this text is
            # FAILED, so ending forward would deliver ungated text. END HELD instead.
            if gate == "failed":
                return _end_run_cursor(steps), GATE_HOLD_LOOP_EXHAUSTED
            return cursor + 1, None
        return tidx, None
    return cursor + 1, None


def _visit_cap_decision(guard: "RunGuard | None", steps, cursor: int, bounds=None):
    """ISSUE #94 -- `max_visits` AS A TRUE PER-STEP EXECUTION CEILING.

    `--max-visits` is documented on four surfaces as a "per-step per-run EXECUTION
    ceiling", and `package_view` renders it to the operator as "Max visits (loop ceiling)".
    It used to be consulted in exactly ONE place -- `_branch_cursor`, for the step a branch
    was jumping INTO -- so it bounded a step only as a loop-back TARGET. A step that
    carried the flag but was not itself a target had no bound of its own, which is
    precisely backwards for the canonical correction loop, where the operator puts the
    ceiling on the GATE (`lqa --branch on_fail=mt --max-visits 1`) and the gate is the
    branch SOURCE. On prod an `lqa` step with `--max-visits 1` executed twice.

    So the ceiling is now also checked BEFORE a step executes, whatever role it plays in
    the graph. Returns `(step, reason, is_gate)` for the first step in the entry set that
    has ALREADY executed its ceiling, else `(None, None, False)`.

    THE ENTRY SET is the step at `cursor`, or -- when `bounds` names a parallel group being
    forked -- every member of that group, so a group member cannot slip past its own
    ceiling on the group's coat-tails.

    END AT A GATE, SKIP AT A PRODUCER (gate C round 1, H3 -- corrects this train's own
    round-1 rule, which ended the run at ANY capped step). The two are not symmetric:

      * a capped GATE must END the run (held). Stepping over it would run the downstream
        steps -- a deliver step included -- on text no gate ever passed, the same
        fall-through hole #56/F12 closed for a judge outage.
      * a capped PRODUCER/FIXER must be SKIPPED and the run continue. Ending there is the
        MORE permissive choice, not the safer one: the producer's last output is already
        in `working[]`/`current_text` and will be committed by the post-loop write-back, so
        ending before the gate delivers text that was never judged at all. Skipping it
        lets the gate run and judge the last produced text -- and when that text is
        byte-identical to what the gate already judged, the #95 no-progress guard carries
        the verdict forward and (on a FAIL) holds. The loop still terminates.

    `max_visits` stays a SOFT cap in the sense that matters (the run COMPLETES; only the
    run-level total-step cap FAILS a run).

    NO STEPRUN ROW IS WRITTEN for either outcome (gate C round 1, M1). A row that records
    a step NOT executing was folded by `run_overlay.build_overlay` as a visit and won the
    node status, so `GET /api/runs/{id}` reported `visits: 2` for a ceiling of 1 -- on the
    exact surface issue #94's own reproduction reads -- and a gate that had executed once
    and FAILED read `skipped`, while `run.context["visits"]` said 1. Both run surfaces now
    agree because `visits` counts executions only. The ceiling event is carried by
    `run.context["loop_ended"]`, the live `step_skipped` event, and (for a gate) the
    run-level hold warning.

    INERT when the guard is inactive -- a plain/linear workflow carries no branch and no
    max_visits, so nothing here is ever consulted and the run is byte-identical."""
    if guard is None or not guard.active:
        return None, None, False
    lo, hi = bounds if bounds else (cursor, cursor)
    for i in range(lo, hi + 1):
        if not (0 <= i < len(steps)):
            continue
        st = steps[i]
        if guard.at_visit_cap(st):
            cap = st.max_visits if st.max_visits is not None else guard.default_max
            if not isinstance(cap, int) or cap < 1:
                cap = guard.default_max  # L2 belt, mirroring RunGuard.at_visit_cap
            is_gate = _is_gate_step(st)
            tail = (
                "so it is not run again and the run ends here, HELD (D4 per-step loop safety)"
                if is_gate
                else (
                    "so it is not run again; the run continues to the next step so the "
                    "gate still judges the text produced so far (D4 per-step loop safety)"
                )
            )
            return (
                st,
                (
                    f"visit_cap_reached: step '{_step_ref(st)}' has already executed its "
                    f"max_visits ceiling of {cap}, {tail}"
                ),
                is_gate,
            )
    return None, None, False


def _eval_run_condition(step: WorkflowStep, run: WorkflowRun, session: Session) -> bool:
    """SPEC §5 pre-step guard evaluation. Returns True = RUN the step, False = SKIP.

    NULL/empty run_condition ALWAYS returns True — the byte-stable default (a plain
    step never reaches the read below; the callers also short-circuit on a NULL
    condition so this is never even entered on a plain run). Reads the DURABLE Phase-1
    signal (StepRun.gate/score) of the most-recent qualifying prior step in THIS run —
    NOT any in-memory tally — so evaluation is identical before and after a human-review
    pause/resume. Structured + allowlisted, NEVER eval'd (context_pkg validate-and-
    sanitize ethos): an unknown ref/op can only reach here past save-time validation,
    and STILL fails safe (returns True = run — never interprets an unknown condition,
    never skips work on a malformed guard)."""
    cond = step.run_condition
    if not cond:
        return True
    ref = cond.get("ref")
    op = cond.get("op")
    value = cond.get("value")
    if (ref not in _RC_REFS and not _is_step_ref(ref)) or op not in _RC_OPS:
        # Off the allowlist (should be impossible past save-time validation) -> fail
        # OPEN (run the step); an unknown condition is never interpreted or executed.
        return True
    prior = _latest_outcome_steprun(session, run, ref)
    if prior is None:
        # No prior outcome to read (the referenced step did not run / produced none).
        # Fail OPEN: run the step — safer than silently skipping and losing work, and
        # save-time validation already rejects a ref with no earlier producing step.
        return True
    if op == "passed":
        return prior.gate == "passed"
    if op == "failed":
        return prior.gate == "failed"
    if prior.status == RunStatus.SKIPPED:
        return False  # Known empty-scope LQA outcome has no score to compare.
    # score ops require a stored numeric score AND a numeric threshold; a missing
    # either side fails OPEN (run) rather than guessing.
    if prior.score is None or not isinstance(value, (int, float)):
        return True
    if op == "score_gte":
        return prior.score >= value
    return prior.score < value  # score_lt


def _refuse_launch_guards(session, workflow_id, doc):
    """Shared fresh-launch precedence, before a capacity refusal on either surface."""
    from app.services import ai_optout

    _refuse_archived(session, workflow_id, doc)
    # AI opt-out (DPAI-08): refuse EAGERLY, before any WorkflowRun row exists.
    ai_optout.refuse_workflow(session, workflow_id, doc)
    _refuse_project_budget(session, doc)


REASON_RESTART = "interrupted_by_restart"


REASON_STALE = "superseded_stale"


REASON_CANCELLED = "cancelled_by_operator"


REASON_WRITE_CONTENTION = "write_contention"


REASON_POOL_EXHAUSTED = "pool_exhausted"


REASON_SUPERSEDED_ABORT = "superseded_by_recovery"


REASON_CRASH = "run_crashed"


REASON_QUEUE_LAUNCH_FAILED = "queue_launch_failed"


SPEND_CAP_STOP_NOTE = "Spend cap reached: queued work stopped. Raise the spend cap before retrying."


FAIL_FAST_STOP_NOTE = (
    "Fail-fast stop: an earlier member failed. Review that failure before retrying."
)


REASON_SPEND_CAP_STOP = "spend_cap_stop"


REASON_FAIL_FAST_STOP = "fail_fast_stop"


_REASON_PATTERNS: tuple = ()


#: MACHINE-READABLE REASON CODES for a run's terminal state (GitHub #106).
#:
#: THE DEFECT THESE CLOSE. A run moved to a terminal state WITHOUT its executor present --
#: reconciled at boot after an OOM kill, swept aside by the staleness floor, cancelled while
#: the app was unresponsive -- has no StepRun to blame, so `failure_shape` and `step_states`
#: are empty by construction and every surface showed a bare `failed` / `cancelled` plus a
#: timestamp. On prod (2026-09-06) four runs ended that way and an operator could not tell
#: "the engine was unreachable" from "the machine was killed and the run was reconciled at
#: boot" -- five different responses behind one word.
#:
#: The RECONCILER is the only thing that can supply the reason, and each one already stamps a
#: distinct sentence on `run.notes` (RESTART_NOTE / STALE_NOTE / CANCEL_NOTE /
#: WRITE_CONTENTION_NOTE / SUPERSEDED_ABORT_NOTE / _crash_note). What was missing is that the
#: sentence never left the DB. `run_reason` turns it into a stable {code, text} pair the API,
#: the CLI and the SSE stream all carry, so a monitor can branch on the CODE and a human
#: reads the TEXT.
#: A run a fan-out QUEUED that never got to start: the admission scheduler raised while
#: promoting it (gate A round 3, HIGH-2). Distinct from `run_crashed` because nothing ran —
#: no provider was called, no segment was touched — and the operator's move is simply to
#: retry that locale/document.
def _reason_patterns() -> tuple:
    """(fragment, code) in PRIORITY order -- the first match wins. Keyed on stable SENTENCE
    FRAGMENTS, not whole strings: STALE_NOTE carries a run id and _crash_note carries an
    exception message.

    THIS TUPLE IS A CLOSED ALLOWLIST, and that is the whole point (gate F round 1, M1).
    `run.notes` is a SHARED, APPENDING column: a tm_match pool that was not readable
    (`orchestrator:6123`), a vendor dead-letter escalation (`vendor_dispatch:1502/:1680`,
    `vendor_rework:609`) and a future informational sentence all land in it. Only the
    sentences written by a TERMINAL RECONCILER answer "why did this run end?", so only they
    appear here -- anything else makes `run_reason` return None rather than publish an
    unrelated sentence under a `Reason:` heading. An earlier cut fell through to an `other`
    code carrying the WHOLE accumulated notes column, which put "tm_match TM pool 5 not
    readable" beside a `failure_shape` naming a dead engine.

    PRECEDENCE BETWEEN THE TWO TERMINAL RECONCILERS (gate F round 1, L5). A run can carry
    both SUPERSEDED_ABORT_NOTE (its own executor discovered, on its way out, that something
    else had already terminated its row) and RESTART_NOTE / STALE_NOTE (the thing that
    terminated it). SUPERSEDED_ABORT_NOTE is deliberately FIRST because it is the LAST event
    in that sequence and the more specific one: it says "this row was terminated under a
    running executor, and that executor's results were discarded", which subsumes and
    explains the earlier sweep. The earlier note is still visible in `notes` on every
    surface; only the CODE a monitor branches on is decided here. WRITE_CONTENTION_NOTE
    outranks both because it is written by the crash handler on the run's own thread and
    names a specific, retryable cause; `Run failed: ` (the generic crash note) is LAST
    because it is the catch-all.

    EVERY FRAGMENT IS DERIVED FROM THE CONSTANT IT MATCHES (gate F round 2, M2). The first
    cut typed two of them out by hand ("This run was already failed by a recovery sweep",
    "Superseded: no activity"), so rewording either constant -- a one-word edit a maintainer
    would make freely -- silently returned that reconciler's runs to `reason: null`, i.e.
    #106 reinstated, with the whole suite green. Now the notes that carry a VARIABLE tail
    (STALE_NOTE's run id, SUPERSEDED_ABORT_NOTE's long second clause,
    WRITE_CONTENTION_NOTE's wrapped sentence) are cut at their own punctuation instead: a
    reword tracks automatically, and if the punctuation itself goes the fragment widens to
    the whole (run-id-bearing) sentence and `verify_run_observability`'s [R8] ratchet fails
    LOUDLY rather than the path going quiet. `_crash_note`'s "Run failed: " is the one
    literal left, because it is that function's own hard-coded prefix and not a constant.

    ACCEPTED, RECORDED ASSUMPTION (gate F round 2, L7): that last fragment is matched
    ANYWHERE in `notes`, so an informational sentence that itself contained "Run failed: "
    would publish as `run_crashed`. It is not anchored because `notes` is an APPENDING
    column -- a crash note is routinely NOT at index 0 (a mid-run informational sentence
    precedes it), so anchoring to the start of the column would break the real path to
    close a hypothetical one. No product path writes that phrase into an informational
    note; if one ever does, give it a distinct constant and classify it in
    `verify_run_observability`'s [R8] ratchet.

    Built on FIRST USE, not at import: CANCEL_NOTE is defined further down this module (with
    cancel_run), so a module-level tuple here would read it before it exists."""
    global _REASON_PATTERNS
    if not _REASON_PATTERNS:
        _REASON_PATTERNS = (
            # POOL_EXHAUSTED_NOTE lives in app.db (the one shared sentence for pool
            # exhaustion, the WRITE_CONTENTION_NOTE pattern). The three crash handlers
            # check pool timeout FIRST, before write contention, so the allowlist does too.
            (POOL_EXHAUSTED_NOTE[:40], REASON_POOL_EXHAUSTED),
            (WRITE_CONTENTION_NOTE[:40], REASON_WRITE_CONTENTION),
            (SUPERSEDED_ABORT_NOTE.split(",", 1)[0], REASON_SUPERSEDED_ABORT),
            (RESTART_NOTE, REASON_RESTART),
            (SPEND_CAP_STOP_NOTE, REASON_SPEND_CAP_STOP),
            (FAIL_FAST_STOP_NOTE, REASON_FAIL_FAST_STOP),
            (QUEUE_LAUNCH_FAILED_NOTE.split(":", 1)[0], REASON_QUEUE_LAUNCH_FAILED),
            (STALE_NOTE.split(";", 1)[0], REASON_STALE),
            (CANCEL_NOTE, REASON_CANCELLED),
            ("Run failed: ", REASON_CRASH),
        )
    return _REASON_PATTERNS


def _failure_reason(run: WorkflowRun, step_runs) -> str:
    """Why a FAILED run failed, in one line, for the 'closed' event (GitHub #44).

    Prefers the LAST step error -- that is the message that actually killed the run
    (prod #74's `APIError ... Context size has been exceeded` on steprun 42) -- and
    falls back to the run's own notes, which is the only thing a run failed OUTSIDE
    the step machinery has (a restart/staleness sweep writes no StepRun at all).
    Bounded: this text is rendered in a browser and is provider-supplied, so it is
    truncated here rather than trusting every consumer to do it.

    P-C5 EXCEPTION: the crash handler's WRITE_CONTENTION_NOTE (and, GitHub #350, its
    sibling POOL_EXHAUSTED_NOTE) outranks the last step error. A multi-step run that
    crashes on a LATER step's `INSERT INTO steprun` lock has already committed StepRuns
    for steps 0..N-1, and a benign informational note on one of those COMPLETED steps
    (HYGIENE_NOTE, an 'LQA safety floor: ...' note, a template-fallback note) sits in
    `sr.error` -- it would otherwise shadow the retryable contention reason and tell the
    operator the wrong thing. This distinct, crash-stamped note is the actual terminal
    cause, so it wins."""
    if run.notes and POOL_EXHAUSTED_NOTE in run.notes:
        return POOL_EXHAUSTED_NOTE[:500]
    if run.notes and WRITE_CONTENTION_NOTE in run.notes:
        return WRITE_CONTENTION_NOTE[:500]
    for sr in reversed(list(step_runs or [])):
        if (sr.error or "").strip():
            return sr.error.strip()[:500]
    return (run.notes or "").strip()[:500]
