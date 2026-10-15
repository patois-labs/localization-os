"""MQM-aligned per-segment Quality-Estimation (QE) score + the routing GATE.

Roadmap gap #1 (QE/MQM wired into a routing GATE) + #6 (LLM-judge QA with
rationale) from the connector-first roadmap. The
consensus HITL pattern across Phrase QPS / XTM Intelligent Score / Smartling LQE:
score EVERY produced segment, route only the low-confidence ones to a human.

This module is the JUDGE. It is a thin, cheap LLM-judge pass that runs AFTER a
workflow step produces a segment's target:

  * `score_segment(spec, source, target, src_locale, tgt_locale)` -> a QE dict
    {score:0-100, rationale:str, categories:{...}, skipped:bool, error:str|None}.
    ONE run_completion call (billed on the normal seam, counted against the spend
    cap, appears in reports). The judge asks for MQM-aligned category counts
    (accuracy / fluency / terminology / style — a compact subset of lqa.py's
    MQM_DIMENSIONS) plus a one-line rationale, and computes 0-100 from the counts.

DESIGN / soft-degrade contract (the judge NEVER blocks production):
  * A DEAD engine ref (DeadEngineError out of get_provider inside run_completion)
    degrades to `skipped=True` — EXACTLY lqa._llm_errors' convention (it returns []
    for a dead engine rather than propagating). The gate then lets the segment
    proceed WITHOUT a score, counted in `qe_skipped`. (An explicit `qe_provider`
    naming a dead engine therefore soft-skips the SCORE, never the run — the same
    posture lqa takes for its own LLM-polish step.)
  * A provider ERROR (run_completion returns error/no text) -> `skipped=True`.
  * JUNK JSON / an out-of-range score -> `skipped=True`.
  In every skip case the segment proceeds; only a clean, in-range parse produces a
  real score that the gate compares against the threshold.

PII: the CALLER passes REDACTED source+target when the step redacts (same rule as
the adaptive feedback buffer) — this module never redacts; it scores whatever text
it is handed, so raw PII must be scrubbed BEFORE it reaches the judge prompt.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import logging

from app.services import llm_json
from app.services.providers import DeadEngineError, ProviderSpec, run_completion, usage_token_counts

log = logging.getLogger("loc.qe")

# The MQM-aligned category subset the judge counts (a compact, cheap slice of
# lqa.MQM_DIMENSIONS — accuracy/fluency/terminology/style is the industry-standard
# QE quadrant). Each count is a non-negative int; the score is 100 minus a weighted
# penalty so more/worse errors drop the score.
QE_CATEGORIES = ("accuracy", "fluency", "terminology", "style")

# Per-error penalty by category (accuracy errors hurt most; style least). Tuned so a
# clean segment scores 100 and a handful of errors lands it in the review band. Kept
# deterministic so the offline MockProvider path is stable in tests.
_CATEGORY_PENALTY = {"accuracy": 20, "terminology": 15, "fluency": 10, "style": 5}

DEFAULT_THRESHOLD = 70

_JUDGE_SYSTEM = "You are a translation quality-estimation judge. Output only JSON."


def clamp_threshold(n) -> int:
    """Coerce a stored/CLI/form threshold to a sane int in [0, 100] (default on junk)."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return max(0, min(100, v))


def _memory_clause(memory_block: str) -> str:
    """Judge-recall PLUMBING (Train D, SPEC-llmemory-phase2 §7.4, LAW 5). "" and
    byte-identical unless the default-OFF `llmemory_judge_recall_enabled` flag is on AND a
    caller supplied a block. No live caller passes one, so the QE judge stays memory-blind
    by construction — the injection point is IDENTIFIED, not wired. LAW 5: turn the flag
    on ONLY with Train E's four-arm harness measuring."""
    block = (memory_block or "").strip()
    if not block:
        return ""
    from app import extensions
    from app.services import prompts

    if not extensions.memory.judge_recall_enabled():
        return ""
    # #362: the judge prompt is one message, so the reference-data sentence follows the block.
    return f"{block}\n{prompts.MEMORY_FENCE_INSTRUCTION}\n\n"


def _judge_prompt(
    source: str, target: str, src_locale: str, tgt_locale: str, memory_block: str = ""
) -> str:
    cats = ", ".join(QE_CATEGORIES)
    return (
        f"Estimate the quality of this {src_locale}->{tgt_locale} translation using "
        f"the MQM error typology. Count errors in each category ({cats}) and give a "
        "SINGLE short rationale sentence.\n\n"
        # Judge-recall plumbing (Train D §7.4/LAW 5): "" and byte-identical by default.
        f"{_memory_clause(memory_block)}"
        f"Source: {source}\nTranslation: {target}\n\n"
        "Return ONLY JSON of the form "
        '{"accuracy":0,"fluency":0,"terminology":0,"style":0,'
        '"rationale":"one short sentence"}'
    )


def _score_from_categories(cats: dict) -> tuple[int, dict]:
    """100 minus the weighted error penalty, floored at 0. Returns (score, counts)."""
    counts, penalty = {}, 0
    for c in QE_CATEGORIES:
        try:
            n = max(0, int(cats.get(c, 0) or 0))
        except (TypeError, ValueError):
            n = 0
        counts[c] = n
        penalty += n * _CATEGORY_PENALTY[c]
    return max(0, 100 - penalty), counts


# The literal placeholder rationale printed in _judge_prompt's template (line ~98):
#   Return ONLY JSON of the form {..."rationale":"one short sentence"}
# That template is ITSELF valid JSON with all-zero counts, so a model that parrots it back
# hands llm_json.loads a real dict scoring 100 — which its verdict-preference rule would
# return AHEAD of the model's actual estimate later in the reply (R2-3, sibling of the LQA
# echo). The all-zero counts are INDISTINGUISHABLE from a genuine clean verdict; the only
# reliable echo marker is this literal placeholder rationale (a real judge never emits the
# instruction text as its rationale). The SAME hole opens for a STRAY NON-ESTIMATE dict — an
# `Example: {}`, a quoted `{"a":1}`, a rationale-only `{"rationale":...}`, or a category-NAMED
# key carrying a NON-COUNT value (`{"style":"formal"}`, `{"accuracy":"lots"}`,
# `{"terminology":null}`, `{"accuracy":[]}`) in the preamble is also a real dict the
# verdict-preference rule returns first, and `_score_from_categories` reads its absent/uncoercible
# counts as all-zero -> a free 100 (R2-3 sibling, families emptyobj/straydict).
# BOTH are closed the same way: `_reject_qe_non_estimate` (the `reject` belt) skips a template
# echo OR a dict carrying no COUNT-valued category key, so the real estimate after it wins; and
# a post-parse backstop catches the CLEAN echo/stray-only reply that reaches us via the parser's
# fast path before the belt runs -> skip as junk_json, never a free 100.
_TEMPLATE_RATIONALE = "one short sentence"


def _norm_rationale(v) -> str:
    """Stripped/lowered rationale with trailing sentence punctuation removed, so an echo that
    merely appends a period/exclamation/ellipsis ("one short sentence.") is still recognized
    as the template placeholder rather than evading the comparison."""
    return str(v or "").strip().lower().rstrip(".!… ")


def _is_count(v) -> bool:
    """True iff `v` is a usable QE category COUNT: `int(v)` succeeds. DELIBERATELY stricter
    than `_score_from_categories`' `int(cats.get(c, 0) or 0)` — there the `or 0` is a scoring
    NICETY that swallows None/""/[] to a benign zero AFTER a shape has already been accepted as
    an estimate; HERE it would let `{"terminology":null}` / `{"accuracy":[]}` masquerade as an
    estimate (a free 100), so we do NOT swallow: `int(None)` / `int([])` / `int("lots")` /
    `int("formal")` all raise -> not a count, while `int(0)` / `int(3)` / `int("3")` /
    `int(3.5)` pass -> a real all-zero verdict and the recorded partial-category verdict both
    stay accepted.

    BOOLEANS are NOT counts (gate round 2): `bool` subclasses `int`, so `int(False)`/`int(True)`
    would (mis)coerce to 0/1 and let `{"accuracy":false}` score a free 100 (and `{"accuracy":
    true}` an 80). A JSON `true`/`false` is never an MQM error count -> excluded explicitly
    FIRST, before the int() attempt."""
    if isinstance(v, bool):
        return False
    try:
        int(v)
    except (TypeError, ValueError):
        return False
    return True


def _reject_qe_non_estimate(cand) -> bool:
    """`reject` predicate for llm_json.loads: True when `cand` is a dict that is NOT a real
    quality estimate — rejected for EITHER R2-3 reason:

      * a TEMPLATE ECHO — _judge_prompt's own template parroted back (its literal placeholder
        rationale); or
      * a STRAY NON-ESTIMATE dict carrying NO count-valued QE_CATEGORIES key — an
        `Example: {}` / quoted `{"a":1}` aside, a rationale-only `{"rationale":...}`, or a
        category-named key with a non-count value (`{"style":"formal"}`, `{"accuracy":"lots"}`,
        `{"terminology":null}`, `{"accuracy":[]}`) — R2-3 sibling, families emptyobj/straydict.

    llm_json's verdict-preference would return either dict AHEAD of the real estimate later in
    the reply, where its all-zero / absent / uncoercible counts score a free 100.

    The estimate bar is "at least ONE QE_CATEGORIES key whose value is a COUNT" (see
    `_is_count`) — NOT all four: a completed survey recorded a REAL verdict shape missing one
    category key (`{"accuracy":3,"fluency":0,"style":0,"rationale":"bad"}`, pinned in
    verify_judge_json §6a(c)), so it MUST stay accepted. A dict with no count-valued category
    key estimated nothing -> fail closed (the template-echo doctrine).

    Category-key matching is CASE-SENSITIVE lowercase (finding 6, deliberate): the prompt
    prints lowercase keys, so an uppercase-keyed reply (`{"Accuracy":3,...}`) has no matching
    count here -> it fails closed to a soft qe skip (junk_json), the SAFE direction (lqa
    normalizes severity case; qe does not, and an uppercase-keyed reply is not worth a special
    case when the fail-closed outcome is a harmless skip, not a bogus score).

    KNOWN RESIDUAL (finding 4, pre-existing, safe direction, OUT of this fix's dict-family
    scope): a stray dict WRAPPED IN A LIST (`[{"a":1}]`) before a real estimate is returned by
    llm_json's rule 2 (a list with >=1 dict IS verdict-shaped), so this dict predicate never
    sees it; `score_segment` then fails CLOSED on the non-dict (`junk_json`), never a false
    100, and the real estimate after it does NOT land — documented + pinned in §8, not a
    desired endpoint."""
    if not isinstance(cand, dict):
        return False
    if _norm_rationale(cand.get("rationale")) == _TEMPLATE_RATIONALE:
        return True  # the prompt's template rationale echoed back
    # No count-valued category key -> not an estimate (a stray `{}`/`{"a":1}`, a rationale-only
    # dict, or a category key with a non-count value). "At least ONE count key" is the bar.
    return not any(_is_count(cand.get(c)) for c in QE_CATEGORIES)


def _skipped(
    reason: str,
    error: str | None = None,
    cost_usd: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> dict:
    # cost/tokens default 0 (dead-engine / provider-error / empty made no paid call);
    # the junk_json path DID spend, so it passes the captured figures through so the
    # step-visit's stamped StepRun still reflects that spend (feat/workflow-spend).
    return {
        "score": None,
        "rationale": "",
        "categories": {},
        "skipped": True,
        "reason": reason,
        "error": error,
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def score_segment(
    spec: ProviderSpec,
    source: str,
    target: str,
    source_locale: str,
    target_locale: str,
    tap_meta=None,
    memory_block: str = "",
    images: list[bytes] | None = None,
) -> dict:
    """Run the ONE judge call over a produced (source, target) segment pair.

    `tap_meta` (Prompt Tap): an OPTIONAL correlation dict threaded UNTOUCHED to
    run_completion so the judge call is attributable in the tap viewer/CLI/document
    filters (workflow callers pass the workflow/run/step/doc/segment identity; other
    callers pass nothing -> honest meta:None, backward-compatible).

    Returns a QE dict. `skipped=True` (score None) whenever the judge could not be
    trusted — a dead engine ref, a provider error, or junk output — so the gate lets
    the segment proceed uncounted-against-quality but counted in `qe_skipped`. A
    successful parse returns skipped=False with an integer score 0-100, a rationale,
    and the per-category counts.

    An empty target is not worth a paid call: it scores 0 with no LLM round-trip (an
    empty target is unambiguously below any threshold, so it always routes to review).

    `memory_block` (Train D §7.4, judge-recall plumbing, LAW 5): an OPTIONAL memory block
    for the judge. DEFAULT "" and byte-identical; inert unless `llmemory_judge_recall_enabled`
    is on (`_memory_clause`). No live caller passes it — the QE judge stays memory-blind.

    `images` (Batch 14 T2, SPEC §6.5): OPTIONAL per-string image context for the judge,
    threaded UNTOUCHED to run_completion so the judge evaluates the translation on the
    SAME visual information the generator had. The orchestrator passes it ONLY when the
    segment's GENERATION leg actually sent images (it inherits the generation verdict,
    D2-QE-ACCOUNTING) — so a non-vision generation never causes a surprise pixel egress to
    a vision QE engine. DEFAULT None = byte-identical (the judge stays image-blind).
    """
    tgt = (target or "").strip()
    if not tgt:
        return {
            "score": 0,
            "rationale": "empty translation",
            "categories": {},
            "skipped": False,
            "reason": "empty",
            "error": None,
        }
    # Judge-recall plumbing (Train D §7.4): threaded but INERT by default — no caller
    # passes a block, and `_memory_clause` is "" unless the flag is on. LAW 5: blind judge.
    prompt = _judge_prompt(source, tgt, source_locale, target_locale, memory_block=memory_block)
    try:
        res = run_completion(
            spec,
            prompt,
            system=_JUDGE_SYSTEM,
            task="qe",
            payload={"source": source, "target": tgt, "target_locale": target_locale},
            tap_meta=tap_meta,
            images=images,
        )
    except DeadEngineError as exc:
        # Follow lqa._llm_errors: a dead engine ref soft-degrades to a no-score
        # (the QE judge is a nicety, never a run-blocker). The gate counts it as
        # qe_skipped and the segment proceeds without a score.
        log.info("QE judge skipped (dead engine): %s", exc)
        from app.services import engines

        pid = engines._parse_engine_ref(spec.provider)
        prof = engines.get_profile(pid) if pid is not None else None
        reason = "engine_disabled" if prof is not None and not prof.enabled else "not_configured"
        return _skipped(reason)
    # Real per-step spend (feat/workflow-spend): capture the judge call's EXACT ledger
    # cost + tokens BEFORE any soft-skip guard, so every skip path that ALREADY SPENT
    # (a priced call that returned whitespace-only text, or junk JSON) still threads its
    # real cost onto the result the orchestrator folds into the StepRun. run_completion
    # records spend only on a SUCCESSFUL call, so an error result carries cost_usd 0.0
    # here — honest: nothing was charged, so the "provider_error" skip rightly stamps 0.
    _judge_tin, _judge_tout = usage_token_counts(res.get("usage"))
    _judge_cost = res.get("cost_usd") or 0.0
    if res.get("error") or not (res.get("text") or "").strip():
        return _skipped("provider_error", res.get("error"), _judge_cost, _judge_tin, _judge_tout)
    try:
        # Tolerant parse (QA-2): the reference live local model (meta-llama-3-8b)
        # wraps its JSON in a prose preamble / ```json fence, which a bare json.loads
        # rejected -> EVERY segment soft-skipped as junk_json and the QE gate became a
        # silent no-op. The extractor recovers the wrapped object; genuine junk still
        # raises a JSONDecodeError (a ValueError subclass) and soft-skips exactly as
        # before. The score/threshold logic below is unchanged.
        # `reject=_reject_qe_non_estimate` (R2-3 + straydict sibling): skip a parroted template
        # OR a stray dict carrying no category counts, so a real estimate after it wins; a
        # prose-wrapped non-estimate-only leaves no candidate -> loads raises -> junk_json. The
        # CLEAN fast-path case (a lone stray/echo dict) bypasses the belt by design and is caught
        # by the two post-parse backstops below.
        data = llm_json.loads(res["text"], reject=_reject_qe_non_estimate)
        if not isinstance(data, dict):
            raise ValueError("judge did not return a JSON object")
        # Template-echo backstop (R2-3): a CLEAN echo-only reply is returned by the parser's
        # fast path BEFORE the reject belt runs. A judge that only parrots the template did not
        # estimate quality -> junk_json (fail closed), never a free score-100 pass.
        if _norm_rationale(data.get("rationale")) == _TEMPLATE_RATIONALE:
            raise ValueError("judge echoed the QE template rather than estimating quality")
        # R2-3 SIBLING (straydict) backstop, next to the template-echo one: a CLEAN reply that
        # is nothing but a stray dict (`{}`, `{"note":"looks fine"}`, `{"style":"formal"}`,
        # `{"accuracy":[]}`) also reaches us via the parser's fast path BEFORE the reject belt.
        # `_score_from_categories` reads every count via `int(cats.get(c, 0) or 0)`, which
        # swallows a missing/None/[]/"formal" value to a benign zero -> all zero -> a free score
        # 100. So the bar here is the SAME as the reject belt's: at least one COUNT-valued
        # category key (`_is_count`), NOT mere key presence — a dict with no coercible count
        # estimated nothing -> junk_json (fail closed), never a free 100.
        if not any(_is_count(data.get(c)) for c in QE_CATEGORIES):
            raise ValueError("judge returned no QE category counts (not an estimate)")
    except (ValueError, TypeError) as exc:
        log.info("QE judge produced junk JSON; skipping: %s", exc)
        # This soft-skip ALREADY spent (the priced judge call returned; a live-provider
        # refusal IS junk_json) — thread its real cost/tokens through, never drop them.
        return _skipped("junk_json", str(exc), _judge_cost, _judge_tin, _judge_tout)
    score, counts = _score_from_categories(data)
    rationale = str(data.get("rationale") or "").strip()[:300]
    return {
        "score": score,
        "rationale": rationale,
        "categories": counts,
        "skipped": False,
        "reason": None,
        "error": None,
        # Real per-step spend (feat/workflow-spend): the EXACT ledger cost + tokens
        # this judge call accrued, so the orchestrator can fold QE-judge spend into
        # the step-visit's stamped StepRun cost (the judge call is part of the step).
        # The EXACT ledger cost/tokens captured above (also threaded onto every
        # spent soft-skip path, so no judge spend is ever dropped).
        "cost_usd": _judge_cost,
        "tokens_in": _judge_tin,
        "tokens_out": _judge_tout,
    }


# --- persistence into Segment.qa_flags ---------------------------------------
#
# STORAGE HOME (no new column): Segment.qa_flags is a JSON list that already exists
# on the model but is written by nothing today — the perfect home for the QE badge.
# We store ONE entry with type=="qe" that the CAT editor surfaces alongside the live
# structural qa checks. The badge carries the score + rationale + gate decision so a
# reviewer sees WHY a segment was routed. Idempotent: re-scoring replaces any prior
# qe entry (never accumulates duplicates).
QE_FLAG_TYPE = "qe"
QE_AUTOFIX_FLAG_TYPE = "qe_autofix"  # == qe_autofix.FLAG_TYPE (qe cannot import it: cycle)


def skip_message(card: dict) -> str:
    from app.services.qe_jev import redact

    reason = card.get("reason")
    error = str(card.get("error") or "")
    words = {
        "not_configured": "judge not configured",
        "jev_disabled": "engine disabled",
        "engine_disabled": "engine disabled",
        "dead_engine": "engine disabled" if "disabled" in error.lower() else "judge not configured",
        "low_confidence": "judge was unsure",
        "junk_json": "judge returned an invalid answer",
    }
    return redact(words.get(reason) or error or "judge not configured")[:500]


def qe_flag(
    score: int | None,
    rationale: str,
    threshold: int,
    passed: bool,
    skipped: bool,
    reason=None,
    error=None,
) -> dict:
    """The single qa_flags entry describing a QE result, shaped like a qa.check_segment
    flag ({type,severity,message}) so the editor renders it with the same badge code,
    plus QE-specific fields (score/rationale/threshold/passed) for the detail view."""
    if skipped:
        sev, msg = "minor", "QE skipped: " + skip_message({"reason": reason, "error": error})
    elif passed:
        sev = "minor"
        msg = f"QE {score}/100 >= {threshold} — passed"
    else:
        sev = "major"
        msg = (
            "QE structural check failed (judge did not score) - routed to review"
            if score is None
            else f"QE {score}/100: structural check failed - routed to review"
            if score is not None and score >= threshold
            else f"QE {score}/100 < {threshold} — routed to review"
        )
    if rationale:
        msg = f"{msg}: {rationale}"
    return {
        "type": QE_FLAG_TYPE,
        "severity": sev,
        "message": msg,
        "score": score,
        "rationale": rationale,
        "threshold": threshold,
        "passed": bool(passed),
        "skipped": bool(skipped),
    }


def merge_qe_flag(existing: list | None, entry: dict) -> list:
    """Return qa_flags with any prior QE entry replaced by `entry` (structural qa
    flags, if ever persisted here, are preserved). A fresh list each call so SQLAlchemy
    sees the JSON column as changed (it does not track in-place mutation)."""
    out = [
        f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") == QE_FLAG_TYPE)
    ]
    out.append(entry)
    return out


def clear_qe_flag(existing: list | None) -> list:
    """Return qa_flags with any QE entry REMOVED (sibling of merge_qe_flag).

    Review finding: a segment routed to review by the QE gate keeps its stale
    'routed to review' badge in the editor even after a reviewer fixes it — save
    (-> EDITED) and approve (-> APPROVED) change the segment's status but never
    touched qa_flags, so the QE entry (with its old score/rationale) lingered
    forever. Both write sites should call this so the badge disappears once a
    human has acted on the segment. A fresh list each call, same reason as
    merge_qe_flag (SQLAlchemy does not track in-place JSON mutation).

    The sibling `qe_autofix` record (Train B, the auto-fix rounds behind that verdict)
    goes with it: once a person has acted, the badge describing the automated attempt is
    as stale as the verdict it explained."""
    return [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") in (QE_FLAG_TYPE, QE_AUTOFIX_FLAG_TYPE))
    ]
