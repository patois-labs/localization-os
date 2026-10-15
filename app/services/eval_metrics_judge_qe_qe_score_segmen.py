"""eval_metrics: judge_qe: qe.score_segment (reference-free mqm-aligned qe)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.eval_metrics_the_result_type import (
    JUDGE_VERSION,
    ScoreResult,
    _error_spans,
    _format_mqm_reasoning,
    _judge_spec,
    model_family,
    not_applicable,
    ok_result,
)


def _judge_identity(spec) -> dict:
    """The WHICH-MODEL-JUDGED block stamped into every judge ScoreResult.detail."""
    provider = str(getattr(spec, "provider", "") or "")
    model = str(getattr(spec, "model", "") or "")
    return {
        "judge_provider": provider,
        "judge_model": model,
        "judge_family": model_family(model or provider),
    }


def is_self_preference(
    judge_model, candidate_model, *, judge_provider: str = "", candidate_provider: str = ""
) -> bool:
    """True when the judge and the candidate are the SAME model family.

    Falls back to the PROVIDER name when a model id is blank/unrecognized (an
    `engine:<id>` config often carries no model id at this layer), because
    "unrecognized" must not become a free pass on the one check that exists."""
    jf = model_family(judge_model) or model_family(judge_provider)
    cf = model_family(candidate_model) or model_family(candidate_provider)
    return bool(jf) and bool(cf) and jf == cf


def self_preference_warnings(
    *, judge_model=None, judge_provider: str = "", candidates: Sequence[dict] = ()
) -> list[dict]:
    """One warning dict per candidate the judge shares a family with.

    `candidates` are `{"id"|"name", "model", "provider"}` dicts (whatever a surface
    already has). The return is a LIST, empty when there is nothing to say, so a
    surface renders `for w in warnings` and shows nothing in the clean case.
    ADVISORY, not blocking -- the same posture as promotion support: the run is
    allowed, the bias is disclosed."""
    jf = model_family(judge_model) or model_family(judge_provider)
    out: list[dict] = []
    if not jf:
        return out
    for cand in candidates or []:
        cm = str((cand or {}).get("model") or "")
        cp = str((cand or {}).get("provider") or "")
        if not is_self_preference(
            judge_model, cm, judge_provider=judge_provider, candidate_provider=cp
        ):
            continue
        label = str((cand or {}).get("name") or (cand or {}).get("id") or cm or cp)
        out.append(
            {
                "candidate": label,
                "candidate_id": (cand or {}).get("id"),
                "candidate_model": cm,
                "candidate_provider": cp,
                "judge_model": str(judge_model or ""),
                "judge_provider": judge_provider,
                "family": jf,
                "message": (
                    "the judge ({}) and candidate '{}' ({}) are the SAME model "
                    "family '{}' - LLM judges are documented to favour their own "
                    "family, so this comparison may be biased in that "
                    "candidate's favour".format(
                        judge_model or judge_provider, label, cm or cp or "unknown", jf
                    )
                ),
            }
        )
    return out


def _judge_mqm(source_text, output_text, reference_text, ctx) -> ScoreResult:
    from app.services import lqa

    spec = _judge_spec(ctx)
    ident = _judge_identity(spec)
    if not (output_text or "").strip():
        # An empty output is not a quality verdict and is not worth a paid call.
        return not_applicable(
            "judge_mqm",
            "empty output - nothing to judge",
            metric="judge_mqm",
            version=JUDGE_VERSION,
            **ident,
        )
    card = lqa.score_text(
        source_text,
        output_text,
        ctx.get("source_locale") or "",
        ctx.get("target_locale") or "",
        ctx.get("rulebook") or "",
        provider_name=spec.provider,
        style_guide=ctx.get("style_guide") or "",
        tap_meta=ctx.get("tap_meta"),
        spec=spec,
        glossary_terms=ctx.get("glossary_terms") or None,
    )
    # #56/F6: a CONFIGURED-LIVE judge that did not deliver a verdict is NOT a score of
    # ~100 — that would credit the candidate for the judge's outage (exactly the sibling
    # _judge_qe skip-guard reasoning). `lqa.score_text` marks such a card
    # `llm_judge="unavailable"`; return the not_applicable state instead of booking `ok`.
    if card.get("llm_judge") == lqa.JUDGE_UNAVAILABLE:
        return not_applicable(
            "judge_mqm",
            "judge could not score this item (llm judge unavailable)",
            metric="judge_mqm",
            version=JUDGE_VERSION,
            cost_usd=float(card.get("cost_usd") or 0.0),
            tokens_in=int(card.get("tokens_in") or 0),
            tokens_out=int(card.get("tokens_out") or 0),
            **ident,
        )
    errors = card.get("errors") or []
    return ok_result(
        "judge_mqm",
        float(card.get("score") or 0.0),
        metric="judge_mqm",
        version=JUDGE_VERSION,
        signature="judge_mqm|impl:lqa.score_text|judge:{}/{}".format(
            ident["judge_provider"], ident["judge_model"] or "default"
        ),
        reasoning=_format_mqm_reasoning(errors),
        errors=_error_spans(errors),
        error_count=len(errors),
        gate=str(card.get("gate") or ""),
        passed=bool(card.get("passed")),
        adjudications=card.get("adjudications"),
        cost_usd=float(card.get("cost_usd") or 0.0),
        tokens_in=int(card.get("tokens_in") or 0),
        tokens_out=int(card.get("tokens_out") or 0),
        **ident,
    )


# --- judge_qe: qe.score_segment (reference-free MQM-aligned QE) -------------- #
def _judge_qe(source_text, output_text, reference_text, ctx) -> ScoreResult:
    from app.services import qe

    spec = _judge_spec(ctx)
    ident = _judge_identity(spec)
    card = qe.score_segment(
        spec,
        source_text,
        output_text,
        ctx.get("source_locale") or "",
        ctx.get("target_locale") or "",
        tap_meta=ctx.get("tap_meta"),
    )
    cost = {
        "cost_usd": float(card.get("cost_usd") or 0.0),
        "tokens_in": int(card.get("tokens_in") or 0),
        "tokens_out": int(card.get("tokens_out") or 0),
    }
    if card.get("skipped"):
        # NOT a zero. The judge could not produce a trustworthy verdict; scoring
        # that as 0.0 would blame the candidate for the judge's outage.
        return not_applicable(
            "judge_qe",
            "judge could not score this item (%s)" % (card.get("reason") or "skipped"),
            metric="judge_qe",
            version=JUDGE_VERSION,
            judge_error=str(card.get("error") or ""),
            **ident,
            **cost,
        )
    return ok_result(
        "judge_qe",
        float(card.get("score") or 0.0),
        metric="judge_qe",
        version=JUDGE_VERSION,
        signature="judge_qe|impl:qe.score_segment|judge:{}/{}".format(
            ident["judge_provider"], ident["judge_model"] or "default"
        ),
        reasoning=str(card.get("rationale") or "").strip() or "no rationale returned",
        categories=dict(card.get("categories") or {}),
        **ident,
        **cost,
    )


def _judge_qe_jev(source_text, output_text, reference_text, ctx) -> ScoreResult:
    """The Jev judge scorer. `ctx["jev_ref"]` (optional) names the judge: "jev" or an
    `engine:<id>` of kind jev / jev_local. Absent, it is the legacy env target
    (TYPESAFE_*), byte-identical to before. A dead or non-judge ref is not_applicable
    (the judge could not score), never a zero."""
    from app.services import qe_jev
    from app.services.providers import DeadEngineError, ProviderSpec

    ref = (ctx.get("jev_ref") or "").strip()
    try:
        judge = qe_jev.resolve_target(ref) if ref else qe_jev.target_from_env()
    except (DeadEngineError, ValueError) as exc:
        spec = ProviderSpec(provider=ref, model=None)
        return not_applicable(
            "judge_qe_jev",
            "judge could not score this item (provider_error)",
            metric="judge_qe_jev",
            version=JUDGE_VERSION,
            judge_error=str(exc),
            **_judge_identity(spec),
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
        )
    spec = ProviderSpec(provider=judge.provider_ref, model=judge.model)
    ident = _judge_identity(spec)
    card = qe_jev.score_segment(
        source_text,
        output_text,
        ctx.get("source_locale") or "",
        ctx.get("target_locale") or "",
        tap_meta=ctx.get("tap_meta"),
        judge=None if judge.legacy else judge,
    )
    cost = {
        "cost_usd": float(card.get("cost_usd") or 0.0),
        "tokens_in": int(card.get("tokens_in") or 0),
        "tokens_out": int(card.get("tokens_out") or 0),
    }
    if card.get("skipped"):
        # NOT a zero. The judge could not produce a trustworthy verdict; scoring
        # that as 0.0 would blame the candidate for the judge's outage.
        return not_applicable(
            "judge_qe_jev",
            "judge could not score this item (%s)" % (card.get("reason") or "skipped"),
            metric="judge_qe_jev",
            version=JUDGE_VERSION,
            judge_error=str(card.get("error") or ""),
            **ident,
            **cost,
        )
    return ok_result(
        "judge_qe_jev",
        0.0 if card.get("structural_blocked") else float(card.get("score") or 0.0),
        metric="judge_qe_jev",
        version=JUDGE_VERSION,
        signature="judge_qe_jev|impl:qe_jev.score_segment|judge:{}/{}".format(
            ident["judge_provider"], ident["judge_model"] or "default"
        ),
        reasoning=str(card.get("rationale") or "").strip() or "no rationale returned",
        categories=dict(card.get("categories") or {}),
        structural_blocked=bool(card.get("structural_blocked")),
        structural_findings=card.get("structural_findings") or [],
        judge_score=card.get("score"),
        **ident,
        **cost,
    )
