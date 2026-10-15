"""Workflow QE auto-fix: an LLM post-editor for segments that fail a Jev QE gate.

Opt-in per workflow step (WorkflowStep.qe_autofix). The orchestrator calls into this
module right after a segment FAILS the step's QE gate, and only when the step's judge
is a Jev judge (qe_jev.is_judge_ref). The loop itself is qe_jev_loop.refine, with three
injected seams: the step's resolved JevTarget as the scorer, the gate's own card as the
round-0 baseline (no duplicate judge call) and a safety guard (`make_accept`) that
rejects any fixer version the normal output would not have been allowed to keep.

This module decides nothing about what is committed; the orchestrator does (see
orchestrator._autofix_seg). It owns the pure pieces: the skip rules, the fixer spec,
the guard chain and the `qe_autofix` qa_flags record the reviewer reads.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import logging

from app import config
from app.services import lqa_safety, qe, qe_jev_loop, quality, refusal, spend
from app.services.providers import ProviderSpec

log = logging.getLogger("loc.qe_autofix")

FLAG_TYPE = qe.QE_AUTOFIX_FLAG_TYPE  # "qe_autofix"
MIN_ROUNDS = 1
MAX_ROUNDS = 5
DEFAULT_ROUNDS = 3
TEXT_CAP = 2000  # per-round text kept in the flag (the reviewer's history)

# skipped_reason values (also the words the editor shows)
SKIP_JUDGE_NOT_JEV = "judge_not_jev"
SKIP_UNSURE = "unsure"
SKIP_REDACTION = "redaction"
SKIP_SPEND_CAP = "spend_cap"
SKIP_REFUSED = "refused"
SKIP_NO_FIXER = "no_llm_fixer"

_SKIP_WORDS = {
    SKIP_JUDGE_NOT_JEV: "the QE judge is not a Jev judge",
    SKIP_UNSURE: "the judge was unsure, so the segment goes to review without a rewrite",
    SKIP_REDACTION: "the step redacts PII (not supported in v1)",
    SKIP_SPEND_CAP: "the spend cap was reached",
    SKIP_REFUSED: "the output was withheld as a refusal",
    SKIP_NO_FIXER: "the post-editor is not an LLM (pick an LLM post-editor for this step)",
}


def clamp_rounds(n) -> int:
    """Fixer rounds, clamped to 1..5; junk or blank -> the default 3."""
    try:
        value = int(str(n).strip())
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS
    return max(MIN_ROUNDS, min(MAX_ROUNDS, value))


def enabled_for(step) -> bool:
    """The step opted in AND its QE gate is on. False = the orchestrator never enters
    the loop, records nothing, and the run is byte-identical to a pre-Train-B run."""
    return bool(getattr(step, "qe_gate", False)) and bool(getattr(step, "qe_autofix", False))


def configuration_warnings(step) -> list[str]:
    """Save/read warnings for configurations that cannot score or auto-fix."""
    from app.services import engines, qe_jev

    def get(name, default=None):
        return step.get(name, default) if isinstance(step, dict) else getattr(step, name, default)

    warnings = []
    ref = get("qe_provider") or get("provider") or config.DEFAULT_PROVIDER
    pid = engines._parse_engine_ref(ref or "")
    prof = engines.get_profile(pid) if pid is not None else None
    if get("qe_gate") and prof is not None and not prof.enabled:
        warnings.append(f"QE judge {ref} is disabled; the gate will skip scoring.")
    if get("qe_autofix"):
        if not get("qe_gate"):
            warnings.append("QE auto-fix cannot run: the QE gate is off.")
        elif not qe_jev.is_judge_ref(ref):
            warnings.append("QE auto-fix cannot run: the QE judge is not a Jev judge.")
    return warnings


def display_status(step) -> str:
    get = step.get if isinstance(step, dict) else lambda k, d=None: getattr(step, k, d)
    if not get("qe_autofix"):
        return "auto-fix off"
    reasons = [w for w in configuration_warnings(step) if "cannot run" in w]
    return reasons[0] if reasons else "auto-fix on"


def confident_failures(card):
    """Dimensions safe to fix even when another dimension is unsure."""
    return [
        c
        for c, count in (card.get("categories") or {}).items()
        if c not in card.get("unsure", []) and count >= 2
    ] + [f["dimension"] for f in card.get("structural_findings", []) if f["severity"] == "major"]


def skip_reason(card: dict, *, judge_is_jev: bool, redact: bool, cap_tripped: bool, refused: bool):
    """Why a FAILED segment must not enter the loop, or None when it may. Order matters
    only for which single reason is recorded; each one alone is a hard skip."""
    if not judge_is_jev:
        return SKIP_JUDGE_NOT_JEV
    if card.get("unsure") and not confident_failures(card):
        return SKIP_UNSURE
    if redact:
        # v1 limit: the loop would have to carry the PII restore map out of the worker.
        log.info("autofix_skipped: redaction")
        return SKIP_REDACTION
    if refused:
        return SKIP_REFUSED
    if cap_tripped:
        return SKIP_SPEND_CAP
    return None


def effective_fixer_ref(explicit, own) -> str:
    """The ref the post-editor resolves to: `qe_autofix_provider`, else the step's own
    provider, else the install default (exactly what the step itself would run on)."""
    return (explicit or "").strip() or (own or "").strip() or (config.DEFAULT_PROVIDER or "")


def fixer_name_known(ref: str) -> bool:
    """Whether `ref` names a provider that exists: an `engine:<id>` ref (the role check
    owns its kind), the mock, a built-in, or a registered provider (the test/hot-swap
    seam providers_hub.get_provider honours first). Everything else is a typo."""
    from app.services import engines
    from app.services.providers import UnknownProviderError, get_provider

    raw = (ref or "").strip().lower()
    if not raw:
        return False
    pid = engines._parse_engine_ref(raw)
    if pid is not None:
        return engines.get_profile(pid) is not None
    try:
        get_provider(raw)
    except UnknownProviderError:
        return False
    except Exception:
        return True  # known but not usable right now (e.g. keyless): not a typo
    return True


def fixer_is_llm(ref: str) -> bool:
    """Whether `ref` can post-edit (a prompt LLM: engines.ROLE_KINDS["fixer"])."""
    from app.services import engines

    return _fixer_capable(ref, engines)


def no_llm_fixer_message(ref: str) -> str:
    return (
        f"Auto-fix needs an LLM post-editor, and {ref!r} is not an LLM (machine "
        "translation and quality judges cannot post-edit). Pick an LLM as the auto-fix "
        "post-editor, or turn auto-fix off."
    )


def fixer_spec(step):
    """The post-editor's ProviderSpec for `step`. Returns (spec | None, reason | None).

    The fixer is `qe_autofix_provider`, else the step's own provider, else the install
    default, and it must be a prompt-capable LLM (engines.ROLE_KINDS["fixer"]). Owner
    decision (local-first, no silent engine switches): when that ref is NOT an LLM (an
    NMT step engine inherited through a NULL fixer), there is no fixer at all, never a
    quiet swap to another engine. The caller records skipped_reason "no_llm_fixer" with
    the reason text and the segment goes to review. The save paths already refuse this
    shape (workflows._assert_autofix_fixer); this is the run-time backstop."""
    explicit = (getattr(step, "qe_autofix_provider", None) or "").strip()
    own = (getattr(step, "provider", None) or "").strip()
    provider = effective_fixer_ref(explicit, own)
    if not fixer_is_llm(provider):
        return None, no_llm_fixer_message(provider)
    if provider == own and not explicit:
        model = getattr(step, "model", None) or config.DEFAULT_MODELS.get(provider)
    else:
        model = config.DEFAULT_MODELS.get(provider)
    return (
        ProviderSpec(
            provider=provider,
            model=model,
            retry_max=getattr(step, "retry_max", 2),
            backoff_base=getattr(step, "backoff_base", 0.5),
        ),
        None,
    )


def _fixer_capable(ref: str, engines) -> bool:
    raw = (ref or "").strip()
    if not raw or raw.lower() == "jev":
        return False
    kind = engines.ref_kind(raw)
    if kind is None:
        # A built-in (claude / gemini / openai / local / mock): all prompt LLMs. A dead
        # engine ref also lands here and fails loud at call time (fixer_failed).
        return True
    return engines.role_allows(kind, "fixer")


def make_accept(source, before, terms, target_locale, *, unit_guard: bool):
    """The guard every fixer version must pass before it is even scored. It chains the
    checks the normal output of a step is held to, so a fix can never bypass one:
      * refusal.is_refusal: a hedge or refusal is not a translation;
      * quality.tag_damage vs the SOURCE: every inline tag token must survive;
      * _looks_like_unit_translation (when the step's over-translation guard is on): no
        page dump, fence, table or runaway length;
      * lqa_safety.introduced_violations vs the text being fixed: no glossary or DNT
        term the current text honours may be broken (the fix-guard delta).
    Returns a callable text -> reason | None."""
    from app.services.orchestrator_terminology_backen import _looks_like_unit_translation

    def accept(text):
        if refusal.is_refusal(text):
            return "refusal"
        damage = quality.tag_damage(source, text)
        if damage:
            return f"tag_damage: {damage}"
        if unit_guard:
            usable, shape = _looks_like_unit_translation(text, source)
            if not usable:
                return f"over_translation: {shape}"
        new_dnt, new_gloss = lqa_safety.introduced_violations(
            source, before, text, terms, target_locale=target_locale
        )
        if new_dnt or new_gloss:
            names = [str(t) for t in new_dnt] + [str(m.get("source_term")) for m in new_gloss]
            return "glossary_violation: " + ", ".join(names[:5])
        return None

    return accept


def cap_blocked() -> bool:
    """The REAL install spend cap (not a per-visit latch): True when it is on and this
    period's spend has reached it. Records the one-per-period trip audit like the
    provider seam does."""
    blocked, _spent, _cap = spend.would_block()
    if blocked:
        spend.note_trip()
    return blocked


def loop_is_priced(spec, judge) -> bool:
    """Whether any call this loop makes can cost money: a priced (hosted) Jev judge, or
    a post-editor that is not spend-exempt (spend.is_exempt: the mock and the self-hosted
    `local` kind cost nothing). The rule (review B4): a PRICED loop honours the install
    spend cap before it starts and between rounds; a fully local loop (jev_local judge +
    a $0 local fixer) keeps running when the cap is tripped, because it spends nothing,
    exactly like every other $0 call the cap exempts. A fixer that cannot be resolved
    counts as priced (the conservative side of a kill switch)."""
    if judge is not None and getattr(judge, "priced", False):
        return True
    try:
        from app.services.providers import get_provider

        return not spend.is_exempt(get_provider(spec.provider))
    except Exception:
        return True


CAP_STOP_MESSAGE = "Spend cap reached: auto-fix stopped before the next round."


def run_loop(
    source,
    target,
    source_locale,
    target_locale,
    *,
    spec,
    judge,
    baseline_card,
    threshold,
    max_rounds,
    cross_check,
    accept,
    tap_meta,
    priced=False,
    cancelled=None,
    glossary_terms=(),
):
    """Run qe_jev_loop.refine for one failed segment. `judge` is the step's resolved
    JevTarget; every judge call goes through it with the round number in tap_meta. A
    judge card skipped by the spend cap raises, so the loop stops with spend_cap
    instead of spending a fixer call it can no longer score. A `priced` loop re-checks
    the real cap before every fixer round. Unsure dimensions remain review-only;
    the loop can continue while confident major failures remain."""
    from app.services import qe_jev

    def before_round():
        return CAP_STOP_MESSAGE if priced and cap_blocked() else None

    def scorer(text, tap_meta=None):
        card = qe_jev.score_segment(
            source,
            text,
            source_locale,
            target_locale,
            tap_meta=tap_meta,
            judge=judge,
            glossary_terms=glossary_terms,
        )
        if (card.get("skipped") or card.get("judge_skipped")) and "spend cap" in str(
            card.get("error") or ""
        ).lower():
            raise RuntimeError(str(card.get("error")))
        return card

    result = qe_jev_loop.refine(
        source,
        target,
        source_locale,
        target_locale,
        spec=spec,
        target_score=threshold,
        max_tries=clamp_rounds(max_rounds) + 1,  # rounds are FIXER rounds; + baseline
        cross_check=bool(cross_check),
        tap_meta=tap_meta,
        scorer=scorer,
        baseline_card=baseline_card,
        accept=accept,
        stop_on_unsure=True,
        rescore_unchanged=False,  # review gate L3: an identical reply is not re-judged
        before_round=before_round,
        cancelled=cancelled,
    )
    # A cancel during the final judge/cross-check still owns the stop reason,
    # even when that in-flight call returned a passing card and no round follows.
    if cancelled is not None and cancelled():
        result["stop_reason"] = "cancelled"
    return result


def loop_spend(result: dict, baseline_card: dict) -> tuple[float, int, int]:
    """(cost, tokens_in, tokens_out) the LOOP added, excluding the baseline judge call
    the gate already booked into the step's QE spend."""
    totals = result.get("totals") or {}
    return (
        max(0.0, (totals.get("cost_usd") or 0.0) - (baseline_card.get("cost_usd") or 0.0)),
        max(0, (totals.get("tokens_in") or 0) - (baseline_card.get("tokens_in") or 0)),
        max(0, (totals.get("tokens_out") or 0) - (baseline_card.get("tokens_out") or 0)),
    )


def _cap(text) -> str:
    text = text if isinstance(text, str) else ""
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + " [truncated]"


def round_rows(result: dict) -> list[dict]:
    """The reviewer's history: one compact row per version (round 0 = the original)."""
    out = []
    for row in result.get("rounds") or []:
        card = row.get("jev") or {}
        item = {
            "round": row.get("round"),
            "score": None if card.get("skipped") else card.get("score"),
            "categories": dict(card.get("categories") or {}),
            "unsure": list(card.get("unsure") or []),
            "rationale": str(card.get("rationale") or card.get("reason") or "")[:300],
            **{k: card[k] for k in ("structural_findings", "structural_blocked") if k in card},
            "text": _cap(row.get("translation")),
        }
        if row.get("status"):
            item["status"] = row["status"]
        if row.get("error_stage"):
            item["error_stage"] = row["error_stage"]
        if row.get("error"):
            item["error"] = str(row["error"])[:300]
        if row.get("rejected_reason"):
            item["rejected_reason"] = str(row["rejected_reason"])[:300]
            item["rejected_text"] = _cap(row.get("rejected_translation"))
        llm = row.get("llm")
        if llm:
            item["cross_check_score"] = None if llm.get("skipped") else llm.get("score")
        out.append(item)
    return out


def record(
    *,
    outcome: str,
    judge_ref: str = "",
    fixer_ref: str = "",
    fixer_model: str | None = None,
    result: dict | None = None,
    cost_usd: float = 0.0,
    kept: str = "original",
    original_score=None,
    final_score=None,
    max_rounds: int = DEFAULT_ROUNDS,
    cross_check: bool = False,
    warning: str | None = None,
    skipped_reason: str | None = None,
    unsure: list | None = None,
) -> dict:
    """The `qe_autofix` qa_flags entry, shaped like every other flag ({type, severity,
    message}) plus the history. outcome: "fixed" | "needs_review" | "skipped"."""
    rounds = round_rows(result) if result else []
    fixer_rounds = max(0, len(rounds) - 1)
    if outcome == "fixed":
        label = f"auto-fixed in {fixer_rounds} round{'s' if fixer_rounds != 1 else ''}"
        severity = "minor"
        message = f"QE auto-fix: {label} ({original_score} -> {final_score})"
    elif outcome == "skipped":
        label = "auto-fix skipped"
        severity = "minor"
        message = f"QE auto-fix skipped: {_SKIP_WORDS.get(skipped_reason, skipped_reason)}"
        if skipped_reason == SKIP_UNSURE and unsure:
            dimensions = ", ".join(str(d) for d in unsure)
            message = (
                f"QE auto-fix skipped: {dimensions} {'was' if len(unsure) == 1 else 'were'} unsure"
            )
    else:
        label = f"auto-fix tried {fixer_rounds} round{'s' if fixer_rounds != 1 else ''}"
        severity = "major"
        message = (
            f"QE auto-fix: {label}, still below the gate; routed to review "
            f"(kept the {'best attempt' if kept == 'best' else 'original'})"
        )
        if (result or {}).get("stop_reason") == "cancelled":
            label = "auto-fix cancelled"
            message = "QE auto-fix cancelled; no further provider calls started."
        elif (result or {}).get("stop_reason") == "fixer_failed":
            from app.services.qe_jev import redact

            last = rounds[-1] if rounds else {}
            if last.get("rejected_reason"):
                label = "post-editor version rejected"
                reason = last["rejected_reason"]
            else:
                label = (
                    "judge failed" if last.get("error_stage") == "judge" else "post-editor failed"
                )
                reason = last.get("error") or "no version produced"
            message = f"QE auto-fix: {label}: {redact(reason)}; routed to review"
    if warning:
        message += f". {warning}"
    review_dimensions = (result or {}).get("review_dimensions", [])
    if review_dimensions:
        message += ". Left unchanged for review: " + ", ".join(review_dimensions) + " was unsure"
    return {
        "type": FLAG_TYPE,
        "severity": severity,
        "message": message,
        "label": label,
        "outcome": outcome,
        "judge": judge_ref,
        "fixer": {"provider": fixer_ref, "model": fixer_model},
        "max_rounds": clamp_rounds(max_rounds),
        "cross_check": bool(cross_check),
        "rounds_used": fixer_rounds,
        "rounds": rounds,
        "stop_reason": (result or {}).get("stop_reason"),
        "kept": kept,
        "original_score": original_score,
        "final_score": final_score,
        "cost_usd": round(float(cost_usd or 0.0), 6),
        "warning": warning,
        "skipped_reason": skipped_reason,
        "review_dimensions": review_dimensions,
    }


def public_flag(flag: dict, *, session=None) -> dict:
    """Stored QE detail without credentials or provider configuration."""
    from app.models import EngineProfile
    from app.services import engines
    from app.services.qe_jev import redact

    fields = [
        "type",
        "severity",
        "message",
        "score",
        "rationale",
        "threshold",
        "passed",
        "skipped",
        "undecided",
        "reason",
        "error",
        "categories",
        "unsure",
        "dimensions",
        "structural_findings",
        "structural_blocked",
        "judge_rationale",
        "judge_skipped",
        "meaning_promoted",
        "judge",
        "cost_usd",
        "tokens_in",
        "tokens_out",
        "label",
        "outcome",
        "fixer",
        "max_rounds",
        "cross_check",
        "rounds_used",
        "rounds",
        "stop_reason",
        "kept",
        "original_score",
        "final_score",
        "warning",
        "skipped_reason",
        "review_dimensions",
    ]
    result = {key: flag[key] for key in fields if key in flag}
    if isinstance(result.get("fixer"), dict):
        result["fixer"] = {k: v for k, v in result["fixer"].items() if k in ("provider", "model")}
    if isinstance(result.get("rounds"), list):
        result["rounds"] = [
            {
                k: v
                for k, v in row.items()
                if k
                in (
                    "round",
                    "score",
                    "categories",
                    "unsure",
                    "rationale",
                    "text",
                    "cross_check_score",
                    "status",
                    "error_stage",
                    "error",
                    "rejected_reason",
                    "rejected_text",
                    "structural_findings",
                    "structural_blocked",
                )
            }
            for row in result["rounds"]
            if isinstance(row, dict)
        ]
    result = redact(result)
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "TYPESAFE_API_KEY",
        "LOCAL_API_KEY",
    ):
        result = redact(result, getattr(config, name, "") or "")
    fixer = flag.get("fixer") if isinstance(flag.get("fixer"), dict) else {}
    refs = (flag.get("judge"), fixer.get("provider"))
    for ref in refs:
        pid = engines._parse_engine_ref(ref if isinstance(ref, str) else "")
        # Readers already hold a session. Reuse it instead of checking out a
        # second pool connection while the document query holds the first.
        prof = None
        if pid is not None:
            prof = (
                session.get(EngineProfile, pid) if session is not None else engines.get_profile(pid)
            )
        if prof is not None:
            result = redact(result, engines.dec_key(prof.api_key_enc))
            result = redact(result, engines.dec_cfg_secret(prof.config))
    # Redaction can expand a short credential. Apply the stored text limit last.
    for row in result.get("rounds") or []:
        for key in ("text", "rejected_text"):
            if key in row:
                row[key] = _cap(row[key])
    return result


def visit_counts(cards, records) -> dict:
    """Final verdicts for this visit, independent of mutable segment flags."""
    q = dict.fromkeys(("scored", "passed", "failed", "undecided", "skipped"), 0)
    q["skipped_by_reason"] = {}
    af = dict.fromkeys(("attempted", "fixed", "still_failing", "skipped", "rounds_used"), 0)
    af.update(skipped_by_reason={}, cost_usd=0.0)
    for card in cards:
        if card.get("undecided"):
            q["undecided"] += 1
        elif card.get("skipped"):
            q["skipped"] += 1
            reason = str(card.get("reason") or "unknown")
            q["skipped_by_reason"][reason] = q["skipped_by_reason"].get(reason, 0) + 1
        else:
            q["scored"] += 1
            q["passed" if card.get("passed") else "failed"] += 1
    for record in records:
        if record.get("outcome") == "skipped":
            af["skipped"] += 1
            reason = str(record.get("skipped_reason") or "unknown")
            af["skipped_by_reason"][reason] = af["skipped_by_reason"].get(reason, 0) + 1
        else:
            af["attempted"] += 1
            af["fixed" if record.get("outcome") == "fixed" else "still_failing"] += 1
        af["rounds_used"] += record.get("rounds_used") or 0
        af["cost_usd"] += record.get("cost_usd") or 0.0
    af["cost_usd"] = round(af["cost_usd"], 6)
    return {"qe": q, "autofix": af}


def has_flag(existing: list | None) -> bool:
    return any(isinstance(f, dict) and f.get("type") == FLAG_TYPE for f in (existing or []))


def merge_flag(existing: list | None, entry: dict) -> list:
    """qa_flags with any prior auto-fix entry replaced by `entry` (a fresh list, so the
    JSON column is seen as changed)."""
    return [*clear_flag(existing), entry]


def clear_flag(existing: list | None) -> list:
    return [f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") == FLAG_TYPE)]


# What the workflow keeps after a loop, in words. pick_winner below IS the rule; these
# lines sit beside it so the judge Inspect page (qe_jev.describe_rule) states exactly what
# it does, and verify_jev_inspector pins each line against pick_winner's behaviour.
KEEP_RULE = (
    "A rewrite that passes the gate replaces the text, and the segment passes.",
    "Otherwise the best rewrite Jev was sure of (no unsure dimension) that scores strictly "
    "higher than the original replaces the text for the reviewer, and the segment still goes "
    "to review.",
    "When a confident major is repaired alongside an unsure dimension, the improved rewrite "
    "may be kept with that dimension still flagged for review; it cannot pass.",
    "Otherwise the original text stays, and the segment goes to review.",
)


# What the gate does with an UNDECIDED card, in words, beside gate_decision (the rule) so
# the Inspect page (qe_jev.describe_rule) states what orchestrator._score_seg really does:
# an undecided card is counted as QE skipped, carries passed=None (so the segment is NOT
# routed to NEEDS_REVIEW) and gets a "QE undecided" info flag (the editor's qe badge
# carries it as its tooltip). verify_jev_inspector pins
# the rendered text; verify_jev_engines pins the behaviour (status stays MT).
UNDECIDED_RULE = (
    "Score at or above the threshold with an unsure dimension: undecided. It is not a pass, "
    "but the segment carries on unchanged (it is not sent to review), its QE flag reads "
    '"QE undecided", and it is counted as QE skipped.'
)


def pick_winner(result: dict, orig_score, threshold: int):
    """(winner_row | None, fixed) after a loop, the rule KEEP_RULE states. The best
    passing version wins (the earliest on a tie) and the segment is fixed; failing that,
    the best CONFIDENT version whose score is strictly higher than the original's is kept
    for the reviewer (review gate M1: an unsure score is inflated by construction); else
    None (the original stays). An unsure stop fixes nothing (review B1)."""
    orig_score = orig_score if orig_score is not None else -1
    scored = [row for row in result["rounds"][1:] if not row["jev"]["skipped"]]
    deferred = result.get("review_dimensions", [])
    passing = (
        []
        if result.get("stop_reason") == "unsure"
        else [row for row in scored if gate_decision(row["jev"], threshold) == "passed"]
    )
    # max() keeps the FIRST of equal scores, so the earliest version wins a tie.
    winner = max(passing, key=lambda row: row["jev"]["score"]) if passing else None
    if winner is None:
        better = [
            row
            for row in scored
            if not (set(row["jev"].get("unsure", [])) - set(deferred))
            and not row["jev"].get("structural_blocked")
            and (
                (
                    row["jev"].get("score") is None
                    and deferred
                    and row["jev"].get("reason") == "low_confidence"
                )
                or (row["jev"].get("score") is not None and row["jev"]["score"] > orig_score)
                or (
                    result["rounds"][0]["jev"].get("structural_blocked")
                    and row["jev"].get("score") is not None
                    and row["jev"]["score"] >= orig_score
                )
            )
        ]
        winner = (
            max(
                better,
                key=lambda row: row["jev"]["score"] if row["jev"].get("score") is not None else -1,
            )
            if better
            else None
        )
    fixed = bool(passing) and gate_decision(winner["jev"], threshold) == "passed"
    return winner, fixed


def gate_decision(card: dict, threshold: int) -> str:
    """The QE gate's verdict on one card, pure: "skipped" (the judge did not score),
    "undecided" (at or above the bar but a dimension was unsure), "passed" or "failed".
    The single rule both the gate (orchestrator._score_seg) and the auto-fix re-judge
    apply, so a fixed version is held to exactly the bar the original failed."""
    if card.get("structural_blocked"):
        return "failed"
    if card["skipped"]:
        return "skipped"
    if card.get("score") is None:
        return "undecided" if card.get("unsure") else "skipped"
    passed = card["score"] >= threshold
    if passed and card.get("unsure"):
        return "undecided"
    return "passed" if passed else "failed"
