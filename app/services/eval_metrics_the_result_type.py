"""eval_metrics: the result type

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.models import EvalScoreState
from app.services import quality

TIER_DETERMINISTIC = 0


TIER_JUDGE = 1


TIER_NEURAL = 2


TIER_LABELS = {
    TIER_DETERMINISTIC: "deterministic",
    TIER_JUDGE: "llm-judge",
    TIER_NEURAL: "neural",
}


MODULE_VERSION = "1"


NO_REFERENCE_REASON = "no reference available for this item"


# --- tiers ------------------------------------------------------------------- #
# Bumped when the DETERMINISTIC wrappers in this module change how they compute a
# value (not when a wrapped library version changes - that is carried separately in
# each scorer's own version string).
# The reason string a reference-needing scorer reports when there is no reference.
class UnknownScorer(KeyError):
    """Raised by get()/score() for a metric id that is not registered."""


# --- the result type --------------------------------------------------------- #
@dataclass(frozen=True)
class ScoreResult:
    """One scorer's verdict on one (source, output, reference) triple.

    `state` is an `EvalScoreState` member and is the FIRST thing a caller must read:

      OK             - `value` is a real number, safe to average, chart and rank
                       (respecting `higher_is_better`).
      NOT_APPLICABLE - the scorer declined; `value` IS None. The overwhelmingly common
                       cause is a reference-based metric on a reference-free item.
                       NEVER coerce this to 0. Exclude it from means, hide the cell.
      ERROR          - the scorer raised; `value` IS None and `detail["error"]` says
                       why. Also never a 0.

    `detail` always carries provenance (`metric`, `version`, and where applicable
    `signature` and `tokenizer`) plus whatever explanation the scorer can give -
    the QA checks put their human-readable reason in `reason`."""

    scorer: str
    state: str
    value: float | None
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == EvalScoreState.OK

    @property
    def not_applicable(self) -> bool:
        return self.state == EvalScoreState.NOT_APPLICABLE

    @property
    def errored(self) -> bool:
        return self.state == EvalScoreState.ERROR


def ok_result(scorer: str, value: float, **detail) -> ScoreResult:
    return ScoreResult(
        scorer=scorer, state=EvalScoreState.OK, value=float(value), detail=dict(detail)
    )


def not_applicable(scorer: str, reason: str, **detail) -> ScoreResult:
    """The ONLY correct answer when a scorer cannot legitimately run. value is None."""
    d = dict(detail)
    d["reason"] = reason
    return ScoreResult(scorer=scorer, state=EvalScoreState.NOT_APPLICABLE, value=None, detail=d)


def error_result(scorer: str, error: str, **detail) -> ScoreResult:
    d = dict(detail)
    d["error"] = error
    return ScoreResult(scorer=scorer, state=EvalScoreState.ERROR, value=None, detail=d)


_LOCALE_TOKENIZER = {
    "zh": "zh",
    "cmn": "zh",
    "yue": "zh",
    "ja": "char",
    "ko": "char",
}


DEFAULT_TOKENIZER = "13a"


_ASIAN_TER = frozenset({"zh", "cmn", "yue", "ja", "ko", "th"})


# --- tokenizer selection ----------------------------------------------------- #
# sacreBLEU tokenizer per target script. See the module docstring for why ja/ko get
# `char` rather than sacreBLEU's mecab tokenizers.
# Locales for which TER's `asian_support` normalization is enabled.
def _lang(locale: str | None) -> str:
    """Primary subtag of a locale, lowercased ('zh-Hans' -> 'zh', 'pt_BR' -> 'pt')."""
    if not locale:
        return ""
    return re.split(r"[-_]", str(locale).strip(), maxsplit=1)[0].lower()


def tokenizer_for_locale(locale: str | None) -> str:
    """The sacreBLEU tokenizer id to use for a target locale.

    RECORDED on every score and in the run's metric_signature. Two BLEU numbers
    produced with different tokenizers are not comparable, so an unlabeled one is
    worthless - this function exists so the label is never guessed."""
    return _LOCALE_TOKENIZER.get(_lang(locale), DEFAULT_TOKENIZER)


_SB_CACHE: dict = {}


# --- sacreBLEU (Tier 0, reference-based) ------------------------------------- #
def _sacrebleu():
    import sacrebleu  # local import: keeps app boot free of the dependency

    return sacrebleu


def sacrebleu_version() -> str:
    return _sacrebleu().__version__


_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


WORD_TOKENIZER = "eval_metrics.word_re/1"


# --- rapidfuzz edit distance (Tier 0, reference-based) ----------------------- #
# rapidfuzz is ALREADY a direct dependency (fuzzy TM matching); nothing new installs.
# Version the tokenization too: a word-level edit distance computed with a different
# tokenizer is a different number, and the sacreBLEU lesson is that unlabeled numbers
# get compared anyway.
def word_tokens(text: str | None) -> list[str]:
    """Word + punctuation tokens. Punctuation is its OWN token so a missing comma
    costs exactly one edit rather than corrupting the word it hangs off."""
    return _WORD_RE.findall(text or "")


def _levenshtein():
    from rapidfuzz.distance import Levenshtein

    return Levenshtein


def _rapidfuzz_version() -> str:
    import rapidfuzz

    return f"rapidfuzz {rapidfuzz.__version__}"


# #611 / #599: Levenshtein is O(len(a) * len(b)) and rapidfuzz holds the GIL for the whole
# call, so one very long output or reference stalls every thread. Up to this many cells
# (20k x 20k characters, about 0.05 s) the full computation runs unchanged; past it, a
# banded search. Shared with app/services/postedit.py (whose docstring promises parity).
EXACT_CELLS = 400_000_000


def bounded_distance(a, b, exact_cells: int = EXACT_CELLS) -> tuple[int, bool]:
    """(distance, exact) for two strings or token lists, in bounded time.

    Within `exact_cells`: the plain full computation. Past it: rapidfuzz's banded search
    (`score_cutoff`), cost length x band, band widened 4x per round up to the budget. EXACT
    whenever the true distance fits the band (a long text with a small edit); a near-total
    rewrite of a huge text returns the proven lower bound max(band + 1, |len(a) - len(b)|)
    with exact=False, never the full quadratic pass."""
    Lev = _levenshtein()
    n, m = len(a), len(b)
    if n * m <= exact_cells:
        return Lev.distance(a, b), True
    k_max = max(1, exact_cells // max(n, m))
    k = min(64, k_max)
    while True:
        d = Lev.distance(a, b, score_cutoff=k)
        if d <= k:
            return d, True
        if k >= k_max:
            return max(k_max + 1, abs(n - m)), False
        k = min(k * 4, k_max)


def _edit_word(source_text, output_text, reference_text, ctx) -> ScoreResult:
    a, b = word_tokens(output_text), word_tokens(reference_text)
    dist, exact = bounded_distance(a, b)
    extra = {} if exact else {"lower_bound": True}
    return ok_result(
        "edit_distance_word",
        dist,
        metric="edit_distance_word",
        version=_rapidfuzz_version(),
        tokenizer=WORD_TOKENIZER,
        signature=f"levenshtein|level:word|tok:{WORD_TOKENIZER}|{_rapidfuzz_version()}",
        output_tokens=len(a),
        reference_tokens=len(b),
        **extra,
    )


def _edit_char(source_text, output_text, reference_text, ctx) -> ScoreResult:
    dist, exact = bounded_distance(output_text or "", reference_text or "")
    extra = {} if exact else {"lower_bound": True}
    return ok_result(
        "edit_distance_char",
        dist,
        metric="edit_distance_char",
        version=_rapidfuzz_version(),
        tokenizer="none (raw characters)",
        signature=f"levenshtein|level:char|{_rapidfuzz_version()}",
        **extra,
    )


def _edit_pct(source_text, output_text, reference_text, ctx) -> ScoreResult:
    """Character edit distance as a PERCENTAGE of the longer string (0..100).

    This is the MTPE-effort signal: roughly "what share of this segment would a
    post-editor have to touch". Pricing bands over it are a configurable overlay and
    are deliberately NOT hardcoded anywhere in this module."""
    a, b = output_text or "", reference_text or ""
    if len(a) * len(b) <= EXACT_CELLS:
        pct, extra = _levenshtein().normalized_distance(a, b) * 100.0, {}
    else:
        dist, exact = bounded_distance(a, b)
        pct = dist / max(len(a), len(b)) * 100.0
        extra = {} if exact else {"lower_bound": True}
    return ok_result(
        "edit_distance_pct",
        pct,
        metric="edit_distance_pct",
        version=_rapidfuzz_version(),
        tokenizer="none (raw characters)",
        signature=f"levenshtein|level:char|normalized|{_rapidfuzz_version()}",
        **extra,
    )


QA_VERSION = "quality.py/2"


# --- deterministic QA checks (Tier 0, reference-FREE) ------------------------ #
# These WRAP app/services/quality.py's existing pure functions. They do not
# reimplement them: the Lab must measure the exact bar the production write seam
# enforces, or a candidate can "win" the Lab and still be rejected in production.
#
# Convention: 1.0 = clean (check passed), 0.0 = violated, higher_is_better=True, and
# the human-readable reason lands in detail["reason"]. NOT_APPLICABLE is used where
# the check has nothing to check against (no glossary in scope, no char limit set) -
# reporting a free 1.0 there would inflate every aggregate with checks that never ran.
def _pass_fail(scorer: str, reason: str, **detail) -> ScoreResult:
    return ok_result(
        scorer,
        0.0 if reason else 1.0,
        metric=scorer,
        version=QA_VERSION,
        signature=f"{scorer}|impl:{QA_VERSION}",
        reason=reason or "",
        passed=not reason,
        **detail,
    )


def _qa_tag_damage(source_text, output_text, reference_text, ctx) -> ScoreResult:
    return _pass_fail("qa_tag_damage", quality.tag_damage(source_text, output_text))


def _qa_number(source_text, output_text, reference_text, ctx) -> ScoreResult:
    return _pass_fail(
        "qa_number_violation",
        quality.number_violation(source_text, output_text, ctx.get("target_locale") or None),
    )


def _qa_meta(source_text, output_text, reference_text, ctx) -> ScoreResult:
    return _pass_fail("qa_meta_commentary", quality.meta_commentary(source_text, output_text))


def _qa_dnt(source_text, output_text, reference_text, ctx) -> ScoreResult:
    terms = ctx.get("glossary_terms") or []
    if not terms:
        return not_applicable(
            "qa_dnt", "no glossary/DNT terms in scope", metric="qa_dnt", version=QA_VERSION
        )
    viol = quality.dnt_violations(source_text, output_text, terms)
    return _pass_fail(
        "qa_dnt",
        ("DNT terms dropped: {}".format(", ".join(viol))) if viol else "",
        violations=list(viol),
    )


def _qa_glossary(source_text, output_text, reference_text, ctx) -> ScoreResult:
    terms = ctx.get("glossary_terms") or []
    if not terms:
        return not_applicable(
            "qa_glossary", "no glossary terms in scope", metric="qa_glossary", version=QA_VERSION
        )
    missed = quality.glossary_term_violations(
        source_text, output_text, terms, target_locale=ctx.get("target_locale") or None
    )
    reason = ""
    if missed:
        reason = "required terms missing: {}".format(
            ", ".join(
                "{} -> {}".format(m.get("source_term", ""), m.get("target_term", ""))
                for m in missed
            )
        )
    return _pass_fail("qa_glossary", reason, violations=list(missed))


def _qa_char_limit(source_text, output_text, reference_text, ctx) -> ScoreResult:
    """Length ceiling check (proposal section 5's "length/char-limit check").

    DELEGATES to `quality.char_limit_violation` -- the SAME predicate the production write
    seam enforces (spec section 6), so the Lab and production can never disagree about
    whether a value fits. This was the ONE Lab QA scorer that did not delegate to
    `quality` (contrast `_qa_number`/`_qa_meta`/`_qa_dnt`/`_qa_glossary`); lifting it
    restores the file's own pattern. It carries a small, deliberate behaviour change for
    the Lab -- the shared predicate counts NFC code points where this scorer used bare
    `len()`, so a decomposed accent now counts as one character exactly as a precomposed
    one does (pinned by a test).

    The limit comes from `ctx["char_limit"]`, or from the item's own `meta["char_limit"]`
    which a surface may fold into ctx per item. A limit that is None / non-integer / <= 0
    is NOT_APPLICABLE, never a free pass -- `quality.coerce_char_limit` and this scorer
    agree on that, exactly as the old bare `int(...) <= 0` did."""
    limit = quality.coerce_char_limit(ctx.get("char_limit"))
    if limit is None:
        return not_applicable(
            "qa_char_limit", "no char limit configured", metric="qa_char_limit", version=QA_VERSION
        )
    reason = quality.char_limit_violation(output_text, limit)
    return _pass_fail("qa_char_limit", reason, length=quality.char_count(output_text), limit=limit)


JUDGE_VERSION = "judge-wrappers/2"


JUDGE_OUTPUT_TOKENS = 180


JUDGE_PROMPT_OVERHEAD_TOKENS = 220


# ============================================================================== #
# TIER 1 - LLM-AS-JUDGE (work package (b))
# ============================================================================== #
# PROPOSAL-evaluations-lab section 5 ("Tier 1 - LLM-as-judge, reusing what we
# have") and section 7 ("judge-bias guard").
#
# THESE TWO SCORERS COST REAL MONEY. Everything above this line is local
# arithmetic; everything below makes a provider call per item. That is why they
# carry `tier=TIER_JUDGE`: `default_scorers()` pre-ticks Tier 0 ONLY, so a judge is
# never selected by accident, and `estimate_cost` in app/services/eval_runner.py
# prices exactly the scorers whose tier is TIER_JUDGE.
#
# REUSE, NEVER REIMPLEMENT (the same law the QA wrappers above obey). `judge_mqm`
# wraps `lqa.score_text` -- the MQM judge already calibrated against human MQM at
# F1 0.795 -- and `judge_qe` wraps `qe.score_segment`. A second judge prompt living
# in the Lab would measure a quality bar the product does not actually enforce, and
# would drift from it silently.
#
# THE PROVIDER SPEC ARRIVES THROUGH `ctx`, NOT THROUGH THE SIGNATURE. The registry's
# fn contract is `fn(source_text, output_text, reference_text, ctx)` and it is not
# this work package's to change, so the judge engine travels in `ctx["judge_spec"]`
# (a providers.ProviderSpec) or, failing that, `ctx["judge_provider"]` /
# `ctx["judge_model"]`. eval_runner assembles that ctx once per run.
#
# NEVER A BARE NUMBER. Both scorers put the judge's REASONING and its per-error
# SPANS into `detail`, plus WHICH MODEL JUDGED (`judge_provider` / `judge_model` /
# `judge_family`). Section 6 requires the judging model as provenance and section 7
# requires it disclosed on screen; a stored 82.0 with no reasoning and no judge
# identity is not actionable and not comparable to anything.
#
# A SKIPPED JUDGE IS NOT A ZERO. `qe.score_segment` soft-degrades (dead engine,
# provider error, junk JSON) rather than raising. That returns NOT_APPLICABLE here,
# with the reason -- exactly the section-6 discipline the reference rule already
# encodes. Scoring a judge failure as 0.0 would defame a candidate for an outage.
# Judge output is bounded and short (a small JSON error list), so a fixed output
# estimate is honest enough for a PRE-RUN cost estimate. Exposed rather than buried
# so eval_runner can show its arithmetic (section 12.1: "with the token math
# visible").
def _judge_spec(ctx: dict):
    """The ProviderSpec the judge call runs on.

    Precedence: an explicit `ctx["judge_spec"]` (what eval_runner passes) beats
    `ctx["judge_provider"]`/`ctx["judge_model"]`, which beat the install defaults.
    Built lazily so this module keeps importing with no provider machinery."""
    spec = ctx.get("judge_spec")
    if spec is not None:
        return spec
    from app import config
    from app.services.providers import ProviderSpec

    provider = ctx.get("judge_provider") or config.DEFAULT_PROVIDER
    model = ctx.get("judge_model") or config.DEFAULT_MODELS.get(provider)
    return ProviderSpec(provider=provider, model=model)


_MODEL_FAMILIES: dict[str, str] = {
    "claude": "claude",
    "anthropic": "claude",
    "gpt": "openai",
    "openai": "openai",
    "o1-": "openai",
    "o3-": "openai",
    "gemini": "google",
    "google": "google",
    "gemma": "google",
    "palm": "google",
    "command": "cohere",
    "cohere": "cohere",
    "llama": "llama",
    "mistral": "mistral",
    "ministral": "mistral",
    "mixtral": "mistral",
    "codestral": "mistral",
    "qwen": "qwen",
    "deepseek": "deepseek",
    "granite": "ibm",
    "olmo": "allenai",
    "kimi": "moonshot",
    "chatglm": "zhipu",
    "mock": "mock",
}


_MODEL_FAMILY_TOKENS: dict[str, str] = {
    "phi": "microsoft",
    "glm": "zhipu",
    "yi": "01ai",
}


_NOT_A_FAMILY: frozenset = frozenset({"local", "openai_compatible", "custom", "engine", "ollama"})


_ID_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


_ID_ALPHA_RUN = re.compile(r"[a-z]+|[0-9]+")


# --- the judge-bias guard (section 7) ---------------------------------------- #
# Self-preference bias in LLM judges is documented: a model scores its own family's
# output higher. In an eval product that silently RIGS a comparison -- the candidate
# sharing the judge's family wins on a bias the user never sees. We cannot remove
# the bias, so the product's job is to DETECT and DISCLOSE it.
#
# Matching is on FAMILY, not exact model id, because claude-sonnet judging
# claude-haiku is the same failure mode as claude-sonnet judging itself.
# Longest-substring-wins, the same discipline app/services/pricing.py uses on model
# ids, so `claude-opus-4-8` and a dated snapshot both land on "claude".
#: Fragments matched as SUBSTRINGS (longest key wins) against a model id or provider name. The
#: value is the VENDOR whose weights the model is, because that is what judge bias follows: an
#: open-weight line resolves to the vendor that trained it, exactly as `gemini`/`palm` already
#: resolve to "google". Two spellings of the same weights MUST land on the same value -- that is
#: why `gemma` maps to "google" and not to "gemma" (stress-0906 #92): `google/gemma-4-12b` matches
#: the longer key "google", so a "gemma" family would have reported the LM Studio and the Ollama
#: spelling of one model as two families, and claimed separation where there is none.
#:
#: The same rule is why there is NO `nemotron` key (round-1 gate MED-1). Nemotron is NVIDIA's
#: fine-tune OF Llama weights, so `nvidia/llama-3.1-nemotron-70b-instruct` and `llama-3.1-70b`
#: are the same weights lineage; a "nvidia" family would have fabricated SEPARATION between a
#: Nemotron judge and a Llama candidate -- the #92 fail-open under a new key. The id carries
#: `llama`, so it resolves to the base line, and a Nemotron spelling that carries no base name
#: resolves to "" (unknown is honest).
# round-1 gate LOW-3: `_family_tokens` yields only MAXIMAL letter runs, so `chatglm3-6b`
# never produces the token `glm` and reported "" while `glm-4.6` reported "zhipu" -- two
# spellings of one vendor's line, the shape of #92. The named compound is a substring key;
# a general "token ENDS WITH a short key" rule is refused because it is exactly what would
# make `delphi-2` resolve to microsoft.
#: Fragments too SHORT to match as substrings without false hits ("phi" inside "delphi", "yi"
#: inside an unrelated id). They must match a whole TOKEN of the id, and they are consulted only
#: after the substring table finds nothing, so a longer, more specific key always wins.
#: Names that describe HOW a model is hosted, not WHOSE weights it runs. `engine_family()` falls
#: back to a profile's KIND when the profile names no model, and "openai_compatible" is the wire
#: PROTOCOL that OpenRouter, Groq, LM Studio and Ollama all speak -- reading it as the "openai"
#: family would fabricate a same-family warning between a real GPT judge and a local Gemma. A
#: fabricated warning is worse than silence: it trains operators to ignore the real ones.
#:
#: These are matched against the WHOLE id, never against a path segment (round-1 gate MED-2): a
#: hosting KIND is a kind, and `google/custom` / `anthropic/local` / `openai/engine` are ids whose
#: ORG prefix names the vendor. Treating the segment as a kind discarded those four ids.
#: `ollama` is here (round-1 gate LOW-4) because it is the one hosting name that resolves to a
#: WRONG family rather than to silence: the substring `llama` lives inside it, and beats `qwen`.
def normalize_model_id(model_or_provider) -> str:
    """A model id reduced to its BASE NAME: lowercased, an Ollama/quantization TAG dropped
    (`gemma4:e4b-it-qat` -> `gemma4`) and a provider/org/repo PREFIX dropped
    (`hf.co/unsloth/gemma-4-12b-gguf` -> `gemma-4-12b-gguf`).

    It is the FIRST string `model_family` resolves, and the raw id is the fallback (round-1 gate
    MED-2). That ordering is the whole point: a prefix names the org or the HOST, and a host name
    can SHADOW the model's own family token -- `ollama/qwen3` and `ollama/phi-4` both carry the
    substring `llama`, which is longer than `qwen` and outranks the `phi` token tier, so resolving
    the raw id first reports the wrong vendor. Resolving the base name first reports qwen and
    microsoft. The raw id is still tried when the base name identifies nothing, so `google/t5-xxl`
    keeps resolving through its `google/` prefix.

    A weights-file suffix (`.gguf`, `.safetensors`) is deliberately NOT stripped: no such suffix
    contains or shadows a family key, so stripping it would be a normalization that never changes
    an answer -- and an inert step reads as a defence that is not there."""
    m = (model_or_provider or "").strip().lower().strip("/")
    if not m:
        return ""
    m = m.split(":", 1)[0].strip()  # ollama tag / quant label
    if "/" in m:
        m = m.rsplit("/", 1)[-1].strip()  # provider / org / repo prefix
    return m.strip()


def _family_tokens(text: str) -> set:
    """The id split into matchable tokens, including the letter/digit runs INSIDE a token, so
    `gemma4` yields `gemma` and `phi-4` yields `phi`."""
    out: set = set()
    for chunk in _ID_TOKEN_SPLIT.split(text or ""):
        if not chunk:
            continue
        out.add(chunk)
        out.update(_ID_ALPHA_RUN.findall(chunk))
    return out


def _resolve_family(hay: str) -> str:
    """One candidate string resolved against the two tiers: longest-substring-wins over
    `_MODEL_FAMILIES`, and only if that finds nothing, the whole-token tier. "" when neither
    tier recognizes it."""
    if not hay:
        return ""
    best_key = None
    best_family = ""
    for key, family in _MODEL_FAMILIES.items():
        if key in hay and (best_key is None or len(key) > len(best_key)):
            best_key, best_family = key, family
    if best_key is not None:
        return best_family
    tokens = _family_tokens(hay)
    hit = None
    for key, family in _MODEL_FAMILY_TOKENS.items():
        if key in tokens and (hit is None or len(key) > len(hit[0])):
            hit = (key, family)
    return hit[1] if hit else ""


def model_family(model_or_provider) -> str:
    """The judge-bias FAMILY of a model id or provider name; "" when unrecognized.

    "" is deliberate and load-bearing: an unknown id must NOT be matched against
    another unknown id and reported as a self-preference collision. Silence on
    something we cannot identify is honest; a fabricated warning would train users
    to ignore the real ones.

    stress-0906 #92: the table is matched against the model's normalized BASE NAME first and the
    raw id second, so the Ollama naming convention (`gemma4:e4b-it-qat`) and a bare open-weight id
    (`gemma-3-27b-it`, `translategemma-12b-it`) resolve to the same family as the prefixed
    `google/gemma-4-12b`, and a HOST prefix cannot shadow the model's own family token
    (`ollama/qwen3` is qwen, not llama -- round-1 gate MED-2). This ONE resolver backs the
    rules-mode family warning, `engine_family` on both the lens and the Advanced side,
    `is_self_preference` and `self_preference_warnings`."""
    m = (model_or_provider or "").strip().lower()
    if not m:
        return ""
    # The hosting-KIND test is against the WHOLE id (MED-2): `google/custom` is a model id whose
    # org prefix names the vendor, not a "custom" engine kind.
    if m in _NOT_A_FAMILY:
        return ""
    return _resolve_family(normalize_model_id(m)) or _resolve_family(m)


# --- judge_mqm: lqa.score_text (MQM categories / severities / spans) --------- #
def _format_mqm_reasoning(errors: Sequence[dict]) -> str:
    """The judge's findings as ONE readable line-per-error string.

    The comparison view shows this instead of a bare number. Kept as plain text
    (not a re-derived structure) so what the reviewer reads is what the judge
    actually said."""
    lines = []
    for e in errors or ():
        if not isinstance(e, dict):
            continue
        dim = str(e.get("dimension") or e.get("category") or "?")
        sev = str(e.get("severity") or "?")
        note = str(e.get("note") or "").strip()
        span = str(e.get("span") or "").strip()
        frag = f"{sev}/{dim}"
        if span:
            frag += f" on '{span}'"
        if note:
            frag += f": {note}"
        lines.append(frag)
    return "\n".join(lines) if lines else "no MQM errors found"


def _error_spans(errors: Sequence[dict]) -> list[dict]:
    """The per-error records the UI highlights: dimension, severity, span, note."""
    out = []
    for e in errors or ():
        if not isinstance(e, dict):
            continue
        out.append(
            {
                "dimension": str(e.get("dimension") or e.get("category") or ""),
                "severity": str(e.get("severity") or ""),
                "span": str(e.get("span") or ""),
                "note": str(e.get("note") or ""),
            }
        )
    return out
