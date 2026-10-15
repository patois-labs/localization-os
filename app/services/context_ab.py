"""Context A/B measurement harness (SPEC-context-metadata phase 4 — Measurement).

The LAST piece of the whole Context Package project. Its single purpose (research
principle §7): PROVE whether the context feature improves translation quality with a
real MEASURED number instead of a marketing claim. Every vendor publishes vibes; we
publish a receipt.

WHAT IT DOES
------------
Given a document (+ a target locale), it runs the SAME source segments through
translation TWICE, differing in EXACTLY ONE thing — whether the resolved
TranslationContext block reaches the model:

  * arm "on"  (context ON)  — the normal prompt with the rendered `<translation_context>`
                              block injected (context_pkg.resolve -> render_block).
  * arm "off" (context OFF) — the byte-identical prompt with that block SUPPRESSED
                              (empty) — the control.

Everything else is held constant: same provider, same model, same glossary terms, same
TM few-shot examples, same segments, same deterministic (temperature-0) MockProvider
posture. So any QE delta is attributable to context and nothing else.

Both arms' outputs are scored by the EXISTING MQM-aligned QE judge (app.services.qe —
NOT re-implemented here), plus a deterministic terminology-adherence dimension reusing
the DNT/glossary check (quality.dnt_violations). The result is a structured A-vs-B
comparison: per-segment and aggregate QE scores, the delta, per-dimension sub-scores,
and a one-line verdict.

WHAT IT MUST NOT DO (the measurement, not a translation run — safety rails)
--------------------------------------------------------------------------
This is a MEASUREMENT. It never mutates the document's real translations:
  * it NEVER writes Segment.target_text / Segment.status / Segment.qa_flags,
  * it NEVER writes the TM (no ft_store / segment-TM),
  * it NEVER goes through the refusal choke point's write path (quality.commit_mt_output
    is the sole target_text writer; the harness deliberately does NOT call it — it
    scores the RAW produced text in memory and throws it away).
Both arms + the QE judge are real LLM calls routed through providers.run_completion, so
the spend cap / failover / mock-awareness / reporting all apply for free (on the offline
MockProvider they are free and deterministic).

HONESTY (the "ship numbers not vibes" discipline applied to the harness itself)
-------------------------------------------------------------------------------
MockProvider is deterministic AND context-blind — it produces byte-identical output
whether or not a context block is present, so the Mock delta is ~0 BY CONSTRUCTION. A
Mock run is therefore a STRUCTURAL / plumbing test, never a quality signal. The result
carries `is_mock` + a `label` that says so in plain words, and the verdict on a Mock run
never claims an improvement. Only a run on a LIVE provider produces a real quality
measurement. The result always reports N (segments compared), the provider + model, and
whether it ran on Mock.

Documented in: docs/architecture/context-and-adaptation.md
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import Document, Project, Segment

# Imported at module level (not lazily) so a test can monkeypatch
# `context_ab.run_completion` to capture the two arms' assembled prompts — the QE
# judge uses qe.run_completion, so the two seams stay independently patchable.
from app.services.providers import run_completion

# How many segments to A/B by default. An A/B run is 2 real LLM translations + 2 judge
# calls PER SEGMENT, so a 10k-segment doc would be 40k calls — capped hard by default;
# the caller can raise it. Segments are sampled deterministically (first-N by
# order_index) so a re-run compares the same units.
DEFAULT_MAX_SEGMENTS = 20
MAX_SEGMENTS_CEILING = 200

# QE delta (points) below which we report "no measurable change" rather than claim a
# win/regression — a hair of noise on a live provider is not a signal.
VERDICT_EPSILON = 0.5

# The capstone's whole reason to exist is to NOT be a vibe, so the verdict must not
# over-claim on sampling noise. Two guards below the "improved/regressed" claim:
#
#  * MIN_VERDICT_N — a live win/regression is only claimed once ENOUGH segments were
#    scored. Each arm is a SINGLE generation and temperature is NEVER pinned anywhere in
#    this codebase, so each arm carries the provider's default sampling variance; the QE
#    judge also quantizes (penalties move in discrete 5/10/15/20-pt steps, so ONE
#    category flip on ONE segment is a 5-20pt swing). Averaging over few segments does
#    not smooth that out. Below the floor the verdict is "inconclusive — sample too
#    small". ~12 is a pragmatic floor (research picks 10-20); raise --max-segments to
#    clear it.
#  * NOISE_FLOOR_POINTS — even at adequate N, a mean delta this small sits inside the
#    judge's own quantization + the unpinned-sampling variance, so it is annotated
#    "within the noise floor — not a reliable signal" rather than sold as a win.
MIN_VERDICT_N = 12
NOISE_FLOOR_POINTS = 2.0


@dataclass
class ArmResult:
    """Aggregate scores for one arm (context ON or OFF)."""

    scored: int = 0  # segments the judge gave a real score
    skipped: int = 0  # segments the judge could not score
    mean_qe: float | None = None  # mean QE over scored segments (0-100), or None
    term_applicable: int = 0  # (segment, DNT-term) pairs present in source
    term_violations: int = 0  # DNT terms dropped in this arm's target
    term_adherence: float | None = None  # 1 - violations/applicable (0..1), or None

    def as_dict(self) -> dict:
        return {
            "scored": self.scored,
            "skipped": self.skipped,
            "mean_qe": self.mean_qe,
            "term_applicable": self.term_applicable,
            "term_violations": self.term_violations,
            "term_adherence": self.term_adherence,
        }


@dataclass
class ABResult:
    """The full A-vs-B comparison returned to CLI / web / API."""

    document_id: int
    locale: str
    provider: str
    model: str | None
    qe_provider: str
    is_mock: bool
    n: int  # segments actually compared
    n_available: int  # translatable segments in the doc/locale
    max_segments: int
    context_present: bool  # a non-empty context block resolved for arm ON
    context_block: str
    on: ArmResult = field(default_factory=ArmResult)
    off: ArmResult = field(default_factory=ArmResult)
    segments: list = field(default_factory=list)

    # --- derived views --------------------------------------------------------
    @property
    def label(self) -> str:
        """Plain-words honesty tag: is this a real quality signal or a structural test?"""
        if self.is_mock:
            return (
                "offline mock -- NOT a real quality signal; structural/plumbing test "
                "only (MockProvider is deterministic and context-blind, so both arms "
                "produce identical output)"
            )
        return f"live measurement on provider '{self.provider}'"

    @property
    def qe_delta(self) -> float | None:
        if self.on.mean_qe is None or self.off.mean_qe is None:
            return None
        return round(self.on.mean_qe - self.off.mean_qe, 2)

    @property
    def term_adherence_delta(self) -> float | None:
        if self.on.term_adherence is None or self.off.term_adherence is None:
            return None
        return round(self.on.term_adherence - self.off.term_adherence, 4)

    @property
    def scored_n(self) -> int:
        """The EFFECTIVE sample size behind the delta — the smaller of the two arms'
        scored counts (the delta is only as reliable as the arm the judge scored least).
        The min-N gate below uses this, not the sampled N, so a run where the judge
        skipped most segments cannot claim a win on a handful of scores."""
        return min(self.on.scored, self.off.scored)

    @property
    def verdict(self) -> str:
        """One honest line — the anti-vibe guard. Never claims quality on a Mock run;
        on a live provider it only claims a win/regression once ENOUGH segments were
        scored (MIN_VERDICT_N) AND the mean delta clears the noise floor. Because
        temperature is not pinned, each arm is a single noisy generation — so a small
        delta at small N is annotated as noise, never sold as a signal."""
        if not self.context_present:
            return (
                "no context package resolved for this document -- nothing to A/B "
                "(set a document context override or a matching context card first)"
            )
        if self.is_mock:
            return (
                "offline mock: structural test passed (both arms ran, context "
                "differed, no data mutated) -- run on a LIVE provider for a real "
                "quality number"
            )
        d = self.qe_delta
        if d is None:
            return "insufficient scored segments for a verdict (judge skipped too many)"
        n = self.scored_n
        # (a) MIN-N GATE: too few scored segments to trust any delta as a signal.
        if n < MIN_VERDICT_N:
            return (
                "inconclusive -- sample too small (N=%d scored; want >= %d for a "  # noqa: UP031
                "reliable signal). Raise --max-segments. Note: temperature is not "
                "pinned, so each arm carries the provider's sampling variance -- a "
                "few segments cannot average it out (observed delta %+.1f pts)"
                % (n, MIN_VERDICT_N, d)
            )
        mag = abs(d)
        # (b) below the epsilon: genuinely flat.
        if mag < VERDICT_EPSILON:
            return "no measurable change (QE delta %+.1f over N=%d scored)" % (d, n)  # noqa: UP031
        # (c) NOISE FLOOR: a delta this small sits inside the judge's quantization + the
        # unpinned-sampling variance — report the direction but do NOT sell it as a win.
        if mag < NOISE_FLOOR_POINTS:
            return (
                "within the noise floor -- not a reliable signal: context scored "  # noqa: UP031
                "%+.1f pts (%.1f -> %.1f over N=%d scored). QE penalties quantize in "
                "5-20pt steps and temperature is not pinned; raise --max-segments or "
                "widen the gap to confirm" % (d, self.off.mean_qe, self.on.mean_qe, n)
            )
        # (d) a real, above-noise signal at adequate N.
        if d > 0:
            return "context improved QE by %.1f points (%.1f -> %.1f over N=%d scored)" % (  # noqa: UP031
                d,
                self.off.mean_qe,
                self.on.mean_qe,
                n,
            )
        return "context REGRESSED QE by %.1f points (%.1f -> %.1f over N=%d scored)" % (  # noqa: UP031
            -d,
            self.off.mean_qe,
            self.on.mean_qe,
            n,
        )

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "locale": self.locale,
            "provider": self.provider,
            "model": self.model,
            "qe_provider": self.qe_provider,
            "is_mock": self.is_mock,
            "label": self.label,
            "n": self.n,
            "n_available": self.n_available,
            "max_segments": self.max_segments,
            "context_present": self.context_present,
            "context_block": self.context_block,
            "on": self.on.as_dict(),
            "off": self.off.as_dict(),
            "delta": {"mean_qe": self.qe_delta, "term_adherence": self.term_adherence_delta},
            "verdict": self.verdict,
            "segments": self.segments,
        }


class ABError(ValueError):
    """A well-formed reason the A/B could not run (no such document, no translatable
    segments) — surfaced as a clean error on CLI / web / API, never a 500."""


def _has_text(source) -> bool:
    """A segment the A/B arms can translate: non-empty and not tags-only (#705 gate: a unit
    with only inline tags / one placeholder is copied on every path, so it would spend two
    arm calls and a judge call on identical output and dilute the measured delta)."""
    from app.services.tags_only import is_tags_only

    return bool((source or "").strip()) and not is_tags_only(source)


def clamp_max_segments(n) -> int:
    """Coerce a caller/CLI/form max-segments to [1, MAX_SEGMENTS_CEILING] (default on
    junk). Mirrors qe.clamp_threshold / context_pkg.clamp_examples_window."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SEGMENTS
    return max(1, min(MAX_SEGMENTS_CEILING, v))


def _spec_and_qe_spec(session: Session, project: Project, locale: str, provider):
    """Resolve the ONE translation spec both arms share + the QE-judge spec.

    Both arms use the SAME spec (that is the whole point — the arms may differ ONLY in
    context). `provider`, when given, pins an explicit engine (a caller override, e.g.
    a live provider for a real measurement); else the project's per-pair provider is
    used (byte-identical to the normal translate path). The judge reuses the same
    provider, cheap-by-default (mirrors orchestrator._qe_spec_for_step)."""
    from app import config
    from app.services import translate
    from app.services.providers import ProviderSpec

    if provider:
        spec = translate.spec_for_provider(provider)
    else:
        spec = translate.spec_for_pair(session, project, locale)
    # Judge: same provider, no failover (a nicety soft-degrades to a skip on any error).
    qe_spec = ProviderSpec(provider=spec.provider, model=config.DEFAULT_MODELS.get(spec.provider))
    return spec, qe_spec


def _translate_once(spec, prompt, system, payload):
    """One run_completion translation, returning the RAW produced text (or "").

    Deliberately does NOT go through quality.commit_mt_output (the sole target_text
    writer / refusal choke point): the harness measures, it never writes. A produced
    text that is a refusal/hedge is still SCORED as-is (a hedge will score low, which
    is itself signal), never committed anywhere."""
    res = run_completion(spec, prompt, system=system, task="translate", payload=payload)
    return res.get("text") or ""


def estimate_ab(
    session: Session,
    document: Document,
    locale: str | None = None,
    *,
    provider=None,
    max_segments=DEFAULT_MAX_SEGMENTS,
) -> dict:
    """What a `run_ab` over the same arguments WOULD send, before any provider call (#531).

    Same locale choice, same deterministic first-N sample and same refusals as `run_ab`
    (AI opt-out, no segments), but nothing is translated or scored. Each sampled segment costs
    four provider calls (translate with and without context, then a QE judge per arm).
    Token counts are character-derived (chars/4), like every other pre-flight estimate.
    Shared by `context ab` and `style-guide ab`, whose arms have the same call shape."""
    from app.services import ai_optout
    from app.services.providers_common import est_output_tokens
    from app.services.segment import est_tokens

    ai_optout.refuse_for_document(session, document, "the A/B comparison")
    project = session.get(Project, document.project_id)
    if project is None:
        raise ABError(f"document #{document.id} has no project")
    seg_rows = session.exec(select(Segment).where(Segment.document_id == document.id)).all()
    locales = sorted({s.target_locale for s in seg_rows})
    if locale and locale not in locales:
        raise ABError(
            "document #{} has no segments for locale '{}' (has: {})".format(
                document.id, locale, ", ".join(locales) or "none"
            )
        )
    if not locales:
        raise ABError(f"document #{document.id} has no segments to A/B")
    chosen = locale or locales[0]
    translatable = sorted(
        [s for s in seg_rows if s.target_locale == chosen and _has_text(s.source_text)],
        key=lambda s: s.order_index,
    )
    if not translatable:
        raise ABError(f"document #{document.id} has no translatable segments for '{chosen}'")
    sample = translatable[: clamp_max_segments(max_segments)]
    spec, _qe_spec = _spec_and_qe_spec(session, project, chosen, provider)
    src_tokens = sum(est_tokens(s.source_text) for s in sample)
    out_tokens = est_output_tokens(src_tokens)
    # Per arm: one translate call (prompt = source + a fixed instruction/context overhead) and
    # one judge call (source + produced translation + the rubric).
    overhead = 400
    n = len(sample)
    return {
        "provider": spec.provider,
        "model": getattr(spec, "model", None) or "",
        "locale": chosen,
        "segments": n,
        "calls": 4 * n,
        "input_tokens": 2 * (src_tokens + n * overhead)
        + 2 * (src_tokens + out_tokens + n * overhead),
        "output_tokens": 2 * out_tokens + 2 * n * 150,
    }


def run_ab(
    session: Session,
    document: Document,
    locale: str | None = None,
    *,
    provider=None,
    max_segments=DEFAULT_MAX_SEGMENTS,
    org_id=None,
) -> ABResult:
    """Run the context A/B measurement over `document` and return an ABResult.

    Isolation contract: arm ON and arm OFF share the SAME spec, glossary terms, TM
    few-shot examples and segments; they differ ONLY in whether the rendered context
    block is present. No Segment / TM / status is written (a pure read + in-memory
    score). `org_id` (multi-org) scopes the context resolution exactly like the
    prompt-path callers so a foreign org's card never leaks in.

    Raises ABError for no-such-project / no translatable segments."""
    # AI opt-out (DPAI-08): refuse at the service, so the web, /api, CLI and MCP
    # surfaces are all covered (the document id arrives in a body or a flag).
    from app.services import ai_optout, context_pkg, glossary, qe, tm
    from app.services.prompts import translation_prompt
    from app.services.providers import DeadEngineError, MockProvider, get_provider, provider_is_live
    from app.services.quality import dnt_violations

    ai_optout.refuse_for_document(session, document, "the context A/B comparison")
    project = session.get(Project, document.project_id)
    if project is None:
        raise ABError(f"document #{document.id} has no project")
    src = project.source_locale

    # Choose the locale (explicit, else the doc's first available target locale).
    seg_rows = session.exec(select(Segment).where(Segment.document_id == document.id)).all()
    locales = sorted({s.target_locale for s in seg_rows})
    if locale and locale in locales:
        chosen = locale
    elif locale and locale not in locales:
        raise ABError(
            "document #{} has no segments for locale '{}' (has: {})".format(
                document.id, locale, ", ".join(locales) or "none"
            )
        )
    elif locales:
        chosen = locales[0]
    else:
        raise ABError(f"document #{document.id} has no segments to A/B")

    # Translatable segments = non-empty source, in order_index order. Sample the first
    # N deterministically (so a re-run compares the same units).
    cap = clamp_max_segments(max_segments)
    translatable = sorted(
        [s for s in seg_rows if s.target_locale == chosen and _has_text(s.source_text)],
        key=lambda s: s.order_index,
    )
    n_available = len(translatable)
    if not translatable:
        raise ABError(f"document #{document.id} has no translatable segments for '{chosen}'")
    sample = translatable[:cap]

    spec, qe_spec = _spec_and_qe_spec(session, project, chosen, provider)
    # Honest liveness label: a keyless/dead provider resolves to the offline mock, so
    # `provider_is_live` is the single authority for whether this is a REAL measurement.
    is_mock = not provider_is_live(spec.provider)

    # MED-1 — provider guard on the ARM-TRANSLATE seam. `run_completion` calls
    # get_provider() OUTSIDE its own try/except, so a dead engine ref (engine:<id> that
    # is disabled/deleted/keyless) or an UNKNOWN provider name (UnknownProviderError,
    # which SUBCLASSES DeadEngineError) would otherwise crash the harness with a raw
    # traceback — a 500 on the exact "real measurement" path the caller pinned. Probe
    # the resolution ONCE here, before any spend, and convert it to the clean ABError
    # contract every surface (web 4xx, CLI nonzero) already handles. A keyless REAL
    # provider does NOT raise (it resolves to the mock) — only a genuinely dead/unknown
    # ref does, which is exactly the case a measurement must refuse rather than silently
    # run on the mock while the caller believes they pinned a live engine.
    try:
        get_provider(spec.provider)
    except DeadEngineError as exc:
        raise ABError(
            f"provider '{spec.provider}' is not available for a measurement: {exc}"
        ) from exc

    # Resolve the effective context package ONCE for the doc's scope (identical for
    # every segment — the same stable prefix the real translate path renders). Arm ON
    # gets this block; arm OFF gets "".
    eff_pkg, _prov = context_pkg.resolve(
        session,
        document=document,
        project=project,
        org_id=org_id if org_id is not None else getattr(project, "org_id", None),
    )
    ctx_block = context_pkg.render_block(eff_pkg)
    context_present = bool(ctx_block)

    # TM few-shot examples: retrieved ONCE per segment and used IDENTICALLY in both
    # arms (they are NOT the context being toggled — holding them constant keeps the
    # delta attributable to the context block alone). Reuses the same gated retrieval
    # the real MT path uses.
    sources = [s.source_text for s in sample]
    tm_examples_map = tm.example_matches_batch(
        session,
        project.id,
        sources,
        src,
        chosen,
        floor=context_pkg.TM_EXAMPLES_FLOOR,
        window=context_pkg.TM_EXAMPLES_DEFAULT_WINDOW,
    )

    # A tolerant provider for glossary sense-disambiguation only (a dead ref must not
    # 500 a measurement — degrade to mock for the hint, exactly like the editor grid).
    try:
        gloss_provider = get_provider(spec.provider)
    except DeadEngineError:
        gloss_provider = MockProvider()

    result = ABResult(
        document_id=document.id,
        locale=chosen,
        provider=spec.provider,
        model=spec.model,
        qe_provider=qe_spec.provider,
        is_mock=is_mock,
        n=len(sample),
        n_available=n_available,
        max_segments=cap,
        context_present=context_present,
        context_block=ctx_block,
    )

    for seg in sample:
        source = seg.source_text
        # Glossary terms: computed ONCE, shared by both arms (identical constraints ->
        # the only difference stays the context block). Feeds the DNT dimension too.
        terms = glossary.match_terms(
            session, project.id, source, src, chosen, provider=gloss_provider
        )
        tm_block = context_pkg.render_tm_examples(tm_examples_map.get(source, []))

        # Build the two prompts. The ONLY difference: brief_block carries the context
        # block for arm ON, None for arm OFF. Everything else (glossary terms, TM
        # examples) is byte-identical between the arms.
        sys_on, prompt_on, payload_on = translation_prompt(
            source,
            src,
            chosen,
            terms,
            brief_block=ctx_block or None,
            tm_examples_block=tm_block or None,
        )
        sys_off, prompt_off, payload_off = translation_prompt(
            source, src, chosen, terms, brief_block=None, tm_examples_block=tm_block or None
        )

        text_on = _translate_once(spec, prompt_on, sys_on, payload_on)
        text_off = _translate_once(spec, prompt_off, sys_off, payload_off)

        # Score BOTH arms with the EXISTING MQM judge (never re-implemented). The judge
        # scores raw text in memory — no Segment write, no choke point.
        card_on = qe.score_segment(qe_spec, source, text_on, src, chosen)
        card_off = qe.score_segment(qe_spec, source, text_off, src, chosen)

        # Deterministic terminology-adherence dimension (reuse the DNT/glossary check).
        # `applicable` = DNT terms actually present in THIS source; a violation = a DNT
        # term dropped from the arm's target. dnt_violations reports every DNT term
        # present-in-source-but-absent-from-target, so counting against a target that
        # contains NO term (the single-space sentinel — dnt_violations short-circuits on
        # an EMPTY target, hence a space) yields exactly the present-in-source count.
        dnt_terms = [t for t in terms if isinstance(t, dict) and t.get("dnt")]
        applicable = len(dnt_violations(source, " ", dnt_terms))
        viol_on = dnt_violations(source, text_on, dnt_terms)
        viol_off = dnt_violations(source, text_off, dnt_terms)

        _tally(result.on, card_on, applicable, len(viol_on))
        _tally(result.off, card_off, applicable, len(viol_off))

        result.segments.append(
            {
                "index": seg.order_index,
                "source": source,
                "on": {
                    "target": text_on,
                    "score": card_on.get("score"),
                    "skipped": card_on.get("skipped"),
                    "rationale": card_on.get("rationale"),
                    "term_violations": viol_on,
                },
                "off": {
                    "target": text_off,
                    "score": card_off.get("score"),
                    "skipped": card_off.get("skipped"),
                    "rationale": card_off.get("rationale"),
                    "term_violations": viol_off,
                },
            }
        )

    _finalize_arm(result.on)
    _finalize_arm(result.off)
    return result


def _tally(arm: ArmResult, card: dict, applicable: int, violations: int) -> None:
    """Fold one segment's judge card + term-adherence counts into an arm's running
    totals. A skipped judge is counted but does not move the QE sum."""
    if card.get("skipped"):
        arm.skipped += 1
    else:
        arm.scored += 1
        # accumulate the raw sum in `mean_qe` (converted to a mean in _finalize_arm).
        arm.mean_qe = (arm.mean_qe or 0) + (card.get("score") or 0)
    arm.term_applicable += applicable
    arm.term_violations += violations


def _finalize_arm(arm: ArmResult) -> None:
    """Turn the running QE sum into a mean and compute the term-adherence rate."""
    arm.mean_qe = round(arm.mean_qe / arm.scored, 2) if arm.scored else None
    arm.term_adherence = (
        round(1 - arm.term_violations / arm.term_applicable, 4) if arm.term_applicable else None
    )
