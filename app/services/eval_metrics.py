"""Evaluations Lab - the SCORER REGISTRY and the Tier-0 scorers.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports TIER_DETERMINISTIC, TIER_JUDGE, TIER_NEURAL,
TIER_LABELS, MODULE_VERSION, NO_REFERENCE_REASON, UnknownScorer, ScoreResult, ok_result,
not_applicable, error_result, _LOCALE_TOKENIZER, DEFAULT_TOKENIZER, _ASIAN_TER, _lang,
tokenizer_for_locale, _SB_CACHE, _sacrebleu, sacrebleu_version, _WORD_RE, WORD_TOKENIZER,
word_tokens, _levenshtein, _rapidfuzz_version, _edit_word, _edit_char, _edit_pct, QA_VERSION,
_pass_fail, _qa_tag_damage, _qa_number, _qa_meta, _qa_dnt, _qa_glossary, _qa_char_limit,
JUDGE_VERSION, JUDGE_OUTPUT_TOKENS, JUDGE_PROMPT_OVERHEAD_TOKENS, _judge_spec, _MODEL_FAMILIES,
_MODEL_FAMILY_TOKENS, _NOT_A_FAMILY, _ID_TOKEN_SPLIT, _ID_ALPHA_RUN, normalize_model_id,
_family_tokens, _resolve_family, model_family, _format_mqm_reasoning, _error_spans, _judge_identity,
is_self_preference, self_preference_warnings, _judge_mqm, _judge_qe from
app.services.eval_metrics_the_result_type, app.services.eval_metrics_judge_qe_qe_score_segmen.

PROPOSAL-evaluations-lab sections 5 and 6. This module is PURE COMPUTATION: it opens no
session, touches no table, calls no provider and reaches no network. Give it three
strings and a context dict, get a `ScoreResult` back. That is deliberate - it makes the
whole scoring layer unit-testable without a database and impossible to accidentally
turn into a production write path (proposal section 3, rule 2).

WHY A REGISTRY AND NOT A FUNCTION PER METRIC
--------------------------------------------
Every surface (web run launcher, CLI `eval run start`, /api) needs the SAME answer to
"what can I score with, and which of those apply to this dataset?". A registry means
they ENUMERATE rather than hardcode, so Builder B adding `judge_mqm` or a later Tier-2
COMET entry lights it up everywhere at once and nothing anywhere carries a stale list.

  eval_metrics.list_scorers()                  -> every registered scorer
  eval_metrics.applicable_scorers(has_reference=False) -> the ones that CAN run

THE REFERENCE-AVAILABILITY RULE (section 6) - THE LOAD-BEARING PART
--------------------------------------------------------------------
Most real content has no reference. So every scorer declares `needs_reference`, and a
reference-needing scorer confronted with `reference_text is None`:

    DOES NOT RUN, and returns state NOT_APPLICABLE with value None.

It does NOT return 0.0. It does NOT return None-as-a-score. "No reference available"
and "the translation is terrible" are different facts about the world, and a panel that
renders them identically teaches its users to distrust every number on it. A 0.0 in a
leaderboard column is a claim; NOT_APPLICABLE is an absence. `EvalScoreState` carries
the distinction all the way into the database (nullable `value`), and the surfaces are
expected to HIDE such a column rather than draw a bar of length zero.

ORIENTATION (`higher_is_better`) - ALSO LOAD-BEARING
-----------------------------------------------------
BLEU and chrF go UP when quality goes up. TER and the edit-distance family go DOWN.
A comparison layer that assumes "bigger delta = better" ranks TER exactly backwards and
promotes the worse candidate while displaying a green arrow. So orientation is a
REGISTRY FACT that travels with the metric: `higher_is_better(metric_id)`, consumed by
app/services/eval_stats.py when it computes an oriented delta and a verdict. No caller
should ever hardcode a direction, and tests/verify_evaluations_core.py proves a mutant
that flips it is caught.

PROVENANCE (section 6)
-----------------------
`unlabeled BLEU is not comparable` is the entire reason sacreBLEU exists. So every
scored value carries, in its `detail`, the metric id, the metric VERSION, the sacreBLEU
SIGNATURE STRING where one exists, and the TOKENIZER that ran. `metric_signature()`
assembles the same information once per run for `EvalRun.metric_signature`.

TOKENIZERS AND CJK. sacreBLEU's default `13a` tokenizer is a whitespace/punctuation
tokenizer built for European scripts; running it on Chinese, Japanese or Korean yields
a nearly meaningless BLEU because whole clauses become single tokens. So the tokenizer
is SELECTED FROM THE TARGET LOCALE (`tokenizer_for_locale`) and RECORDED. zh uses
sacreBLEU's `zh` tokenizer. ja/ko use `char`: sacreBLEU's preferred `ja-mecab` /
`ko-mecab` need optional native dependencies (mecab-python3, mecab-ko) that this repo
deliberately does not install, and a character tokenizer is the standard
dependency-free fallback for those scripts - far better than 13a and, crucially,
LABELLED, so a reader knows what produced the number.

TIERS
-----
  TIER_DETERMINISTIC (0) - implemented HERE: sacreBLEU BLEU/chrF/chrF++/TER, rapidfuzz
                           edit distance (word/char/percent), and the existing
                           deterministic QA checks from app/services/quality.py.
  TIER_JUDGE (1)         - LLM-as-judge (lqa.score_text / qe.score_segment). NOT here.
                           Builder B registers them with `register()`; the shape below
                           is already what they need (reference-free, higher-is-better,
                           `detail` carrying the judge's reasoning and WHICH MODEL
                           judged - a bare number from a judge is never enough).
  TIER_NEURAL (2)        - opt-in COMET / MetricX, capability-checked, Phase 2.

REUSE, NEVER REIMPLEMENT. The QA scorers CALL `app.services.quality`'s existing pure
functions. A second copy of the tag-damage or number-violation logic that drifts from
the one the production write seam uses would make the Lab measure a quality bar the
product does not actually enforce.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models import EvalScoreState
from app.services import quality
from app.services.eval_metrics_judge_qe_qe_score_segmen import (
    _judge_identity,
    _judge_mqm,
    _judge_qe,
    _judge_qe_jev,
    is_self_preference,
    self_preference_warnings,
)
from app.services.eval_metrics_the_result_type import (
    _ASIAN_TER,
    _ID_ALPHA_RUN,
    _ID_TOKEN_SPLIT,
    _LOCALE_TOKENIZER,
    _MODEL_FAMILIES,
    _MODEL_FAMILY_TOKENS,
    _NOT_A_FAMILY,
    _SB_CACHE,
    _WORD_RE,
    DEFAULT_TOKENIZER,
    EXACT_CELLS,
    JUDGE_OUTPUT_TOKENS,
    JUDGE_PROMPT_OVERHEAD_TOKENS,
    JUDGE_VERSION,
    MODULE_VERSION,
    NO_REFERENCE_REASON,
    QA_VERSION,
    TIER_DETERMINISTIC,
    TIER_JUDGE,
    TIER_LABELS,
    TIER_NEURAL,
    WORD_TOKENIZER,
    ScoreResult,
    UnknownScorer,
    _edit_char,
    _edit_pct,
    _edit_word,
    _error_spans,
    _family_tokens,
    _format_mqm_reasoning,
    _judge_spec,
    _lang,
    _levenshtein,
    _pass_fail,
    _qa_char_limit,
    _qa_dnt,
    _qa_glossary,
    _qa_meta,
    _qa_number,
    _qa_tag_damage,
    _rapidfuzz_version,
    _resolve_family,
    _sacrebleu,
    bounded_distance,
    error_result,
    model_family,
    normalize_model_id,
    not_applicable,
    ok_result,
    sacrebleu_version,
    tokenizer_for_locale,
    word_tokens,
)

__all__ = [
    "DEFAULT_TOKENIZER",
    "EXACT_CELLS",
    "JUDGE_OUTPUT_TOKENS",
    "JUDGE_PROMPT_OVERHEAD_TOKENS",
    "JUDGE_VERSION",
    "MODULE_VERSION",
    "NO_REFERENCE_REASON",
    "QA_VERSION",
    "TIER_DETERMINISTIC",
    "TIER_JUDGE",
    "TIER_LABELS",
    "TIER_NEURAL",
    "WORD_TOKENIZER",
    "_ASIAN_TER",
    "_ID_ALPHA_RUN",
    "_ID_TOKEN_SPLIT",
    "_LOCALE_TOKENIZER",
    "_MODEL_FAMILIES",
    "_MODEL_FAMILY_TOKENS",
    "_NOT_A_FAMILY",
    "_SB_CACHE",
    "_WORD_RE",
    "EvalScoreState",
    "ScoreResult",
    "UnknownScorer",
    "_edit_char",
    "_edit_pct",
    "_edit_word",
    "_error_spans",
    "_family_tokens",
    "_format_mqm_reasoning",
    "_judge_identity",
    "_judge_mqm",
    "_judge_qe",
    "_judge_spec",
    "_lang",
    "_levenshtein",
    "_pass_fail",
    "_qa_char_limit",
    "_qa_dnt",
    "_qa_glossary",
    "_qa_meta",
    "_qa_number",
    "_qa_tag_damage",
    "_rapidfuzz_version",
    "_resolve_family",
    "_sacrebleu",
    "bounded_distance",
    "error_result",
    "field",
    "is_self_preference",
    "model_family",
    "normalize_model_id",
    "not_applicable",
    "ok_result",
    "quality",
    "re",
    "sacrebleu_version",
    "self_preference_warnings",
    "tokenizer_for_locale",
    "word_tokens",
]


# --- the scorer descriptor --------------------------------------------------- #
@dataclass(frozen=True)
class Scorer:
    """Everything a surface needs to know about one metric WITHOUT running it.

    `fn(source_text, output_text, reference_text, ctx) -> ScoreResult`. The dispatcher
    (`score()`) has ALREADY enforced the reference rule before calling `fn`, so a
    reference-needing scorer's body may assume `reference_text` is a non-None string.

    `sig_fn(target_locale) -> str` produces the provenance signature. For the sacreBLEU
    metrics that is sacreBLEU's own signature string (`nrefs:1|case:mixed|...|version:
    2.6.0`), which is the whole point of the library; for the others it is an
    equivalently explicit descriptor so no score is ever unlabeled."""

    id: str
    label: str
    tier: int
    needs_reference: bool
    higher_is_better: bool
    unit: str  # "score" | "percent" | "edits" | "pass"
    version: str
    description: str
    fn: Callable[[str, str, str | None, dict], ScoreResult]
    sig_fn: Callable[[str], str] | None = None

    # The honest marker a signature failure produces. It is a BACKSTOP, never the
    # contract: the sacreBLEU metrics are PRIMED at construction (see `_primed`) so
    # their real signature is available from the first call, and any scorer whose
    # signature genuinely cannot be computed should say so in these words rather
    # than 500 a page. If this string ever shows up in a stored score's provenance,
    # that is a BUG in the scorer, not an acceptable state.
    SIGNATURE_UNAVAILABLE = "signature unavailable (%s: %s)"

    def signature(self, target_locale: str = "") -> str:
        """The provenance string. NEVER RAISES.

        A scorer descriptor is asked for its signature by every menu, every /api
        payload and every run's metric_signature - i.e. on read paths that must not
        be able to 500 because one metric's library is in an unexpected state. A
        failure here degrades to an explicit marker naming the exception, which is
        both debuggable and honest; it does not silently emit a plausible-looking
        signature, because a WRONG label is worse than a missing one."""
        try:
            if self.sig_fn is not None:
                return self.sig_fn(target_locale or "")
            return f"{self.id}|version:{self.version}"
        except Exception as exc:
            return self.SIGNATURE_UNAVAILABLE % (type(exc).__name__, exc)

    def as_dict(self, target_locale: str = "") -> dict:
        """JSON-safe descriptor for /api, the CLI and EvalRun.metric_signature."""
        return {
            "id": self.id,
            "label": self.label,
            "tier": self.tier,
            "tier_label": TIER_LABELS.get(self.tier, str(self.tier)),
            "needs_reference": self.needs_reference,
            "higher_is_better": self.higher_is_better,
            "unit": self.unit,
            "version": self.version,
            "description": self.description,
            "signature": self.signature(target_locale),
        }


_REGISTRY: dict[str, Scorer] = {}


def register(scorer: Scorer, *, replace: bool = False) -> Scorer:
    """Add a scorer. Builder B's judges and a future COMET entry come in HERE.

    Duplicate ids raise unless `replace=True` (which exists for tests and for an
    install-time capability check that swaps a stub for a real implementation)."""
    if not replace and scorer.id in _REGISTRY:
        raise ValueError(f"scorer already registered: {scorer.id}")
    _REGISTRY[scorer.id] = scorer
    return scorer


def unregister(metric_id: str) -> None:
    _REGISTRY.pop(metric_id, None)


def has(metric_id: str) -> bool:
    return metric_id in _REGISTRY


def get(metric_id: str) -> Scorer:
    try:
        return _REGISTRY[metric_id]
    except KeyError:
        raise UnknownScorer(metric_id) from None


def list_scorers(*, tier: int | None = None, needs_reference: bool | None = None) -> list[Scorer]:
    """Every registered scorer, ordered (tier, id) so surfaces render stably."""
    out = list(_REGISTRY.values())
    if tier is not None:
        out = [s for s in out if s.tier == tier]
    if needs_reference is not None:
        out = [s for s in out if s.needs_reference is needs_reference]
    return sorted(out, key=lambda s: (s.tier, s.id))


def applicable_scorers(has_reference: bool, *, tier: int | None = None) -> list[Scorer]:
    """The scorers that CAN produce a value for a dataset with / without references.

    This is what a run launcher offers. With `has_reference=False` the reference-based
    half simply is not on the menu - which is the section-6 rule applied one level
    earlier than the scorer itself, so a user never even selects a metric that will
    come back entirely NOT_APPLICABLE."""
    out = list_scorers(tier=tier)
    if has_reference:
        return out
    return [s for s in out if not s.needs_reference]


def default_scorers(has_reference: bool) -> list[str]:
    """A sensible pre-ticked Tier-0 selection for a new run."""
    return [s.id for s in applicable_scorers(has_reference, tier=TIER_DETERMINISTIC)]


def higher_is_better(metric_id: str) -> bool:
    """Orientation lookup. eval_stats and every comparison surface MUST use this
    rather than assuming a direction - TER and the edit-distance family are inverted."""
    return get(metric_id).higher_is_better


def needs_reference(metric_id: str) -> bool:
    return get(metric_id).needs_reference


def _primed(metric):
    """Return `metric` after ONE throwaway sentence_score, so `get_signature()` works.

    THE BUG THIS FIXES (found by Builder C on the surfaces branch). A freshly
    constructed sacreBLEU metric does not know how many references it will be given,
    so its signature carries `nrefs` as unset and `get_signature()` raises
    `ValueError: Number of references unknown, please evaluate the metric first.`
    Every provenance path - `Scorer.signature()`, `Scorer.as_dict()`,
    `metric_signature()`, and therefore `evaluations.scorer_menu()` - asks for the
    signature BEFORE anything has been scored. The result was a 500 on the
    dataset-detail page and the CLI/API `show` routes the FIRST time a
    reference-carrying dataset was opened in a fresh process. Not an edge case: the
    ordinary first use.

    THE FIX IS TO PRIME, NOT TO SUPPRESS. One `sentence_score("a", ["a"])` at
    cache-fill time teaches the object `nrefs = 1`, which is exactly and always what
    the Lab does - `_sacrebleu_scorer` scores against a ONE-element reference list,
    because an EvalDatasetItem carries exactly one `reference_text`. So the primed
    signature is not a placeholder standing in until the real one arrives; it IS the
    real one, correct from the first call and unchanged by every subsequent score.
    That matters because section 6 makes the signature load-bearing provenance: an
    unlabeled BLEU is not comparable to anything, which is the entire reason
    sacreBLEU exists, and a "signature unavailable" marker written into a stored
    score would be exactly the unlabeled number the design forbids.

    The priming score itself is discarded and touches no state a later score reads
    (sacreBLEU's sentence_score is stateless apart from this signature metadata)."""
    try:  # noqa: SIM105
        metric.sentence_score("a", ["a"])
    except Exception:  # noqa: S110 -- priming score is discarded; real scores surface faults
        pass
    return metric


def _bleu(tok: str):
    key = ("bleu", tok)
    if key not in _SB_CACHE:
        from sacrebleu.metrics import BLEU

        # effective_order=True is REQUIRED for sentence-level BLEU: without it a short
        # segment with no 4-gram match scores a hard 0 and sacreBLEU itself warns.
        _SB_CACHE[key] = _primed(BLEU(tokenize=tok, effective_order=True))
    return _SB_CACHE[key]


def _chrf(word_order: int):
    key = ("chrf", word_order)
    if key not in _SB_CACHE:
        from sacrebleu.metrics import CHRF

        _SB_CACHE[key] = _primed(CHRF(word_order=word_order))
    return _SB_CACHE[key]


def _ter(asian: bool):
    key = ("ter", asian)
    if key not in _SB_CACHE:
        from sacrebleu.metrics import TER

        _SB_CACHE[key] = _primed(TER(asian_support=asian))
    return _SB_CACHE[key]


def _metric_for(metric_id: str, target_locale: str):
    tok = tokenizer_for_locale(target_locale)
    if metric_id == "bleu":
        return _bleu(tok), tok
    if metric_id == "chrf":
        return _chrf(0), "n/a (character n-grams)"
    if metric_id == "chrf_pp":
        return _chrf(2), "n/a (character + word n-grams)"
    if metric_id == "ter":
        return _ter(_lang(target_locale) in _ASIAN_TER), "tercom"
    raise UnknownScorer(metric_id)


def _sacrebleu_scorer(metric_id: str):
    def _run(source_text, output_text, reference_text, ctx) -> ScoreResult:
        metric, tok = _metric_for(metric_id, ctx.get("target_locale", ""))
        res = metric.sentence_score(output_text or "", [reference_text or ""])
        return ok_result(
            metric_id,
            res.score,
            metric=metric_id,
            version=f"sacrebleu {sacrebleu_version()}",
            signature=str(metric.get_signature()),
            tokenizer=tok,
        )

    return _run


def _sacrebleu_signature(metric_id: str):
    def _sig(target_locale: str) -> str:
        metric, _tok = _metric_for(metric_id, target_locale)
        return str(metric.get_signature())

    return _sig


# --- registration ------------------------------------------------------------ #
def _register_tier0() -> None:
    sb = "sacrebleu (resolved at first use)"
    register(
        Scorer(
            id="bleu",
            label="BLEU",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            higher_is_better=True,
            unit="score",
            version=sb,
            fn=_sacrebleu_scorer("bleu"),
            sig_fn=_sacrebleu_signature("bleu"),
            description=(
                "sacreBLEU sentence BLEU (effective order). n-gram precision "
                "against the reference; tokenizer is chosen from the target "
                "locale and recorded. Higher is better."
            ),
        )
    )
    register(
        Scorer(
            id="chrf",
            label="chrF",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            higher_is_better=True,
            unit="score",
            version=sb,
            fn=_sacrebleu_scorer("chrf"),
            sig_fn=_sacrebleu_signature("chrf"),
            description=(
                "sacreBLEU chrF: character n-gram F-score. Tokenizer-free, so it "
                "behaves far better than BLEU on morphologically rich and "
                "non-space-delimited languages. Higher is better."
            ),
        )
    )
    register(
        Scorer(
            id="chrf_pp",
            label="chrF++",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            higher_is_better=True,
            unit="score",
            version=sb,
            fn=_sacrebleu_scorer("chrf_pp"),
            sig_fn=_sacrebleu_signature("chrf_pp"),
            description=(
                "sacreBLEU chrF++ (chrF plus word bigrams, word_order=2): better "
                "correlation with human judgement than chrF. Higher is better."
            ),
        )
    )
    register(
        Scorer(
            id="ter",
            label="TER",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            # INVERTED. Every comparison must read this flag; see the module docstring.
            higher_is_better=False,
            unit="percent",
            version=sb,
            fn=_sacrebleu_scorer("ter"),
            sig_fn=_sacrebleu_signature("ter"),
            description=(
                "sacreBLEU TER: translation edit rate, the share of edits needed "
                "to turn the output into the reference. LOWER IS BETTER."
            ),
        )
    )
    register(
        Scorer(
            id="edit_distance_word",
            label="Edit distance (words)",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            higher_is_better=False,
            unit="edits",
            version="rapidfuzz",
            fn=_edit_word,
            sig_fn=lambda _loc: (
                f"levenshtein|level:word|tok:{WORD_TOKENIZER}|{_rapidfuzz_version()}"
            ),
            description=(
                "Levenshtein distance in word tokens between output and reference. LOWER IS BETTER."
            ),
        )
    )
    register(
        Scorer(
            id="edit_distance_char",
            label="Edit distance (chars)",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            higher_is_better=False,
            unit="edits",
            version="rapidfuzz",
            fn=_edit_char,
            sig_fn=lambda _loc: f"levenshtein|level:char|{_rapidfuzz_version()}",
            description=(
                "Levenshtein distance in characters between output and reference. LOWER IS BETTER."
            ),
        )
    )
    register(
        Scorer(
            id="edit_distance_pct",
            label="Edit distance %",
            tier=TIER_DETERMINISTIC,
            needs_reference=True,
            higher_is_better=False,
            unit="percent",
            version="rapidfuzz",
            fn=_edit_pct,
            sig_fn=lambda _loc: f"levenshtein|level:char|normalized|{_rapidfuzz_version()}",
            description=(
                "Character edit distance as a percentage of the longer string - "
                "the post-editing effort signal. LOWER IS BETTER."
            ),
        )
    )
    for sid, label, fn, desc in (
        (
            "qa_tag_damage",
            "QA: tag integrity",
            _qa_tag_damage,
            "quality.tag_damage: inline tags dropped, added, duplicated, reordered, "
            "malformed, or a residual masking sentinel. 1 = clean.",
        ),
        (
            "qa_number_violation",
            "QA: numbers",
            _qa_number,
            "quality.number_violation: a value-changing numeric divergence between "
            "source and output. 1 = clean.",
        ),
        (
            "qa_meta_commentary",
            "QA: no meta-commentary",
            _qa_meta,
            "quality.meta_commentary: a translator note or model commentary span leaked "
            "into the output. 1 = clean.",
        ),
        (
            "qa_dnt",
            "QA: do-not-translate",
            _qa_dnt,
            "quality.dnt_violations: a DNT term present in the source is missing from "
            "the output. 1 = clean. Not applicable with no glossary in scope.",
        ),
        (
            "qa_glossary",
            "QA: glossary terms",
            _qa_glossary,
            "quality.glossary_term_violations: a required target term is missing though "
            "its source term appeared. 1 = clean. Not applicable with no glossary.",
        ),
        (
            "qa_char_limit",
            "QA: char limit",
            _qa_char_limit,
            "Output length against ctx['char_limit']. 1 = within limit. Not applicable "
            "when no limit is configured.",
        ),
    ):
        register(
            Scorer(
                id=sid,
                label=label,
                tier=TIER_DETERMINISTIC,
                needs_reference=False,
                higher_is_better=True,
                unit="pass",
                version=QA_VERSION,
                fn=fn,
                description=desc,
                sig_fn=(lambda _loc, _s=sid: f"{_s}|impl:{QA_VERSION}"),
            )
        )


_register_tier0()


# --- the dispatcher ---------------------------------------------------------- #
def score(
    metric_id: str,
    *,
    source_text: str = "",
    output_text: str = "",
    reference_text: str | None = None,
    ctx: dict | None = None,
) -> ScoreResult:
    """Run ONE scorer. THE reference gate lives here, once, for every metric.

    A reference-needing scorer with `reference_text is None` returns NOT_APPLICABLE
    and its `fn` is never called - so no individual scorer can get this wrong, and a
    scorer Builder B adds inherits the rule for free by declaring needs_reference.

    An EMPTY STRING reference is treated as ABSENT: a dataset row whose reference
    column round-tripped through a CSV as "" is not a reference, and scoring an
    output against nothing would produce a confident 0.0 - exactly the number section
    6 forbids. Scorer exceptions become ERROR results (also value None), never a 0,
    so one broken metric cannot take down a whole run."""
    sc = get(metric_id)
    ref = (
        reference_text
        if (reference_text is not None and str(reference_text).strip() != "")
        else None
    )
    if sc.needs_reference and ref is None:
        return not_applicable(
            metric_id,
            NO_REFERENCE_REASON,
            metric=metric_id,
            version=sc.version,
            needs_reference=True,
        )
    try:
        return sc.fn(source_text or "", output_text or "", ref, dict(ctx or {}))
    except Exception as exc:
        return error_result(
            metric_id,
            f"{type(exc).__name__}: {exc}",
            metric=metric_id,
            version=sc.version,
        )


def score_many(
    metric_ids: Sequence[str],
    *,
    source_text: str = "",
    output_text: str = "",
    reference_text: str | None = None,
    ctx: dict | None = None,
) -> dict[str, ScoreResult]:
    """Run several scorers over one item. Order-preserving dict, one entry per id."""
    return {
        mid: score(
            mid,
            source_text=source_text,
            output_text=output_text,
            reference_text=reference_text,
            ctx=ctx,
        )
        for mid in metric_ids
    }


# `aggregate(results)` USED TO LIVE HERE and had zero callers in the entire tree.
# It is deleted rather than wired up, deliberately: the Lab's per-item scores are
# aggregated in exactly one place - `eval_runner.merged_score_map` /
# `eval_stats.mean`, on the paired arrays the bootstrap then resamples - and a second
# mean-of-OK-values helper sitting in the registry is an invitation for some future
# surface to average scores WITHOUT the pairing, the minimum sample size or the
# interval. The one thing this product must not grow is a second, quieter way to
# produce a headline number.


def metric_signature(
    metric_ids: Sequence[str], *, target_locale: str = "", extra: dict | None = None
) -> dict:
    """The provenance blob stamped on `EvalRun.metric_signature` (proposal section 6).

    Records, for every scorer the run used: id, label, tier, orientation, version and
    the SIGNATURE STRING - so a number from this run stays interpretable after a
    sacreBLEU upgrade changes tokenization, after the default tokenizer changes, and
    after everyone who ran the experiment has forgotten what they selected.

    `extra` folds in run-level provenance the metrics layer cannot know (Builder B
    passes the judge model here, Phase 2 the COMET checkpoint)."""
    scorers = {}
    for mid in metric_ids:
        try:
            scorers[mid] = get(mid).as_dict(target_locale)
        except UnknownScorer:
            scorers[mid] = {"id": mid, "error": "scorer not registered"}
    sig = {
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_locale": target_locale or "",
        "tokenizer": tokenizer_for_locale(target_locale),
        "scorers": scorers,
    }
    if extra:
        sig.update(extra)
    return sig


def _register_judges() -> None:
    """Register the Tier-1 judges. Idempotent (replace=True) so a reload, or a test
    that re-imports this module, cannot raise on a duplicate id."""
    register(
        Scorer(
            id="judge_mqm",
            label="LQA judge (MQM)",
            tier=TIER_JUDGE,
            needs_reference=False,
            higher_is_better=True,
            unit="score",
            version=JUDGE_VERSION,
            fn=_judge_mqm,
            sig_fn=lambda _loc: f"judge_mqm|impl:lqa.score_text|version:{JUDGE_VERSION}",
            description=(
                "LLM-as-judge: app.services.lqa.score_text scores the output "
                "against the MQM error typology (dimension + severity + span "
                "per finding), reference-free, 0-100. COSTS MONEY: one provider "
                "call per item. The judging model is recorded on every score."
            ),
        ),
        replace=True,
    )
    register(
        Scorer(
            id="judge_qe",
            label="QE judge",
            tier=TIER_JUDGE,
            needs_reference=False,
            higher_is_better=True,
            unit="score",
            version=JUDGE_VERSION,
            fn=_judge_qe,
            sig_fn=lambda _loc: f"judge_qe|impl:qe.score_segment|version:{JUDGE_VERSION}",
            description=(
                "LLM-as-judge: app.services.qe.score_segment, the MQM-aligned "
                "quality-estimation judge, reference-free, 0-100 with a "
                "rationale and per-category counts. COSTS MONEY: one provider "
                "call per item. A judge that cannot score returns "
                "NOT_APPLICABLE, never a 0."
            ),
        ),
        replace=True,
    )
    register(
        Scorer(
            id="judge_qe_jev",
            label="Jev QE judge (POC)",
            tier=TIER_JUDGE,
            needs_reference=False,
            higher_is_better=True,
            unit="score",
            version=JUDGE_VERSION,
            fn=_judge_qe_jev,
            sig_fn=lambda _loc: f"judge_qe_jev|impl:qe_jev.score_segment|version:{JUDGE_VERSION}",
            description=(
                "Jev hosted text-only POC: app.services.qe_jev.score_segment, the MQM-aligned "
                "quality-estimation judge, reference-free, 0-100 with a "
                "rationale and per-category counts. COSTS MONEY: one provider "
                "call per item. A judge that cannot score returns "
                "NOT_APPLICABLE, never a 0."
            ),
        ),
        replace=True,
    )


_register_judges()


def judge_scorer_ids() -> list[str]:
    """The registered Tier-1 ids - what eval_runner prices as paid judge calls."""
    return [s.id for s in list_scorers(tier=TIER_JUDGE)]
