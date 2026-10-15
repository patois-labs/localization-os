"""Style-guide A/B measurement harness (SPEC-workflows-redesign Track B, B3).

The SIBLING of context_ab.py — same chassis, a DIFFERENT toggled asset. Its single
purpose (the "ship numbers, not vibes" discipline B2's design note demands): PROVE
whether injecting an APPROVED style guide actually improves the translation, with a
real MEASURED number instead of a marketing claim. B2's own grounded note warns that a
style guide "dumped as prose is weak on its own — its value is realized only paired
with B3's verification"; this harness is how that lift is measured.

WHAT IT DOES
------------
Given a document (+ a target locale), it runs the SAME source segments through
translation TWICE, differing in EXACTLY ONE thing — whether the resolved, rendered
`<style_guide>` block reaches the model:

  * arm "on"  (guide ON)  — the normal prompt with the fenced style-guide block injected
                            (style_guides.resolve_style_guide -> render_style_guide).
  * arm "off" (guide OFF) — the byte-identical prompt with that block SUPPRESSED (empty)
                            — the control.

Everything else is held constant: same provider, same model, same glossary terms, same
TM few-shot examples, same RESOLVED CONTEXT block (context is NOT the asset under test —
holding it constant keeps the delta attributable to the style guide alone), same
segments, same deterministic (temperature-0) MockProvider posture. So any QE delta is
attributable to the style guide and nothing else.

Both arms' outputs are scored by the EXISTING MQM-aligned QE judge (app.services.qe —
NOT re-implemented here). The QE judge already counts a "style" error category; this
harness surfaces that category as the ADHERENCE dimension (the analogue of context_ab's
deterministic DNT-term adherence — style adherence is inherently LLM-judge-shaped, not a
string match, exactly the B3 premise). The result is a structured A-vs-B comparison:
per-segment and aggregate QE scores, the delta, the style-error counts + their delta,
and a one-line verdict.

WHAT IT MUST NOT DO (the measurement, not a translation run — safety rails)
--------------------------------------------------------------------------
This is a MEASUREMENT. It never mutates the document's real translations (identical
contract to context_ab):
  * it NEVER writes Segment.target_text / Segment.status / Segment.qa_flags,
  * it NEVER writes the TM (no ft_store / segment-TM),
  * it NEVER goes through quality.commit_mt_output (the sole target_text writer / refusal
    choke point) — it scores the RAW produced text in memory and throws it away.
Both arms + the QE judge are real LLM calls routed through providers.run_completion, so
the spend cap / failover / mock-awareness / reporting all apply for free (on the offline
MockProvider they are free and deterministic).

HONESTY (mirrors context_ab exactly)
------------------------------------
MockProvider is deterministic AND style-blind — it produces byte-identical output
whether or not a style-guide block is present, so the Mock delta is ~0 BY CONSTRUCTION.
A Mock run is therefore a STRUCTURAL / plumbing test, never a quality signal. The result
carries `is_mock` + a `label` that says so in plain words, and the verdict on a Mock run
never claims an improvement. Only a run on a LIVE provider produces a real quality
measurement.

Documented in: docs/architecture/style-guides.md
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import Document, Project, Segment

# Reuse context_ab's tuned, documented sampling + anti-vibe constants verbatim (the
# measurement math is asset-agnostic — only the TOGGLED asset differs). Importing them
# keeps ONE source of truth for the noise floor / min-N so the two harnesses can never
# drift on what counts as a trustworthy signal.
from app.services.context_ab import (
    DEFAULT_MAX_SEGMENTS,
    MIN_VERDICT_N,
    NOISE_FLOOR_POINTS,
    VERDICT_EPSILON,
    ABError,
    _spec_and_qe_spec,
    _translate_once,
    clamp_max_segments,
)

# Imported at module level (not lazily) so a test can monkeypatch
# `style_ab.run_completion` to capture the two arms' assembled prompts — the QE judge
# uses qe.run_completion, so the two seams stay independently patchable (identical to
# the context_ab convention).


@dataclass
class StyleArmResult:
    """Aggregate scores for one arm (style guide ON or OFF)."""

    scored: int = 0  # segments the judge gave a real score
    skipped: int = 0  # segments the judge could not score
    mean_qe: float | None = None  # mean QE over scored segments (0-100), or None
    style_errors: int = 0  # total judge "style"-category errors, scored segs
    mean_style_errors: float | None = None  # style_errors / scored (LOWER = better), or None

    def as_dict(self) -> dict:
        return {
            "scored": self.scored,
            "skipped": self.skipped,
            "mean_qe": self.mean_qe,
            "style_errors": self.style_errors,
            "mean_style_errors": self.mean_style_errors,
        }


@dataclass
class StyleABResult:
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
    guide_present: bool  # a non-empty guide block resolved for arm ON
    guide_id: int | None  # the resolved guide's id (audit / echo), or None
    guide_name: str  # the resolved guide's name, or ""
    guide_block: str  # the rendered <style_guide> block (arm ON input)
    on: StyleArmResult = field(default_factory=StyleArmResult)
    off: StyleArmResult = field(default_factory=StyleArmResult)
    segments: list = field(default_factory=list)

    # --- derived views --------------------------------------------------------
    @property
    def label(self) -> str:
        """Plain-words honesty tag: is this a real quality signal or a structural test?"""
        if self.is_mock:
            return (
                "offline mock -- NOT a real quality signal; structural/plumbing test "
                "only (MockProvider is deterministic and style-blind, so both arms "
                "produce identical output)"
            )
        return f"live measurement on provider '{self.provider}'"

    @property
    def qe_delta(self) -> float | None:
        if self.on.mean_qe is None or self.off.mean_qe is None:
            return None
        return round(self.on.mean_qe - self.off.mean_qe, 2)

    @property
    def style_errors_delta(self) -> float | None:
        """ON minus OFF mean style-error count. NEGATIVE = the guide REDUCED style errors
        (better adherence); positive = more. None when either arm scored nothing."""
        if self.on.mean_style_errors is None or self.off.mean_style_errors is None:
            return None
        return round(self.on.mean_style_errors - self.off.mean_style_errors, 4)

    @property
    def scored_n(self) -> int:
        """The EFFECTIVE sample size behind the delta — the smaller of the two arms'
        scored counts (identical rationale to context_ab.scored_n)."""
        return min(self.on.scored, self.off.scored)

    @property
    def verdict(self) -> str:
        """One honest line — the anti-vibe guard. Never claims quality on a Mock run; on
        a live provider it only claims a lift once ENOUGH segments were scored
        (MIN_VERDICT_N) AND the mean QE delta clears the noise floor. Same guards +
        wording shape as context_ab.verdict, phrased for the style guide."""
        if not self.guide_present:
            return (
                "no APPROVED style guide resolved for this document -- nothing to A/B "
                "(create + approve a guide at this customer/content-type/pair first)"
            )
        if self.is_mock:
            return (
                "offline mock: structural test passed (both arms ran, the style guide "
                "differed, no data mutated) -- run on a LIVE provider for a real "
                "quality number"
            )
        d = self.qe_delta
        if d is None:
            return "insufficient scored segments for a verdict (judge skipped too many)"
        n = self.scored_n
        if n < MIN_VERDICT_N:
            return (
                "inconclusive -- sample too small (N=%d scored; want >= %d for a "  # noqa: UP031
                "reliable signal). Raise --max-segments. Note: temperature is not "
                "pinned, so each arm carries the provider's sampling variance -- a "
                "few segments cannot average it out (observed delta %+.1f pts)"
                % (n, MIN_VERDICT_N, d)
            )
        mag = abs(d)
        if mag < VERDICT_EPSILON:
            return "no measurable change (QE delta %+.1f over N=%d scored)" % (d, n)  # noqa: UP031
        if mag < NOISE_FLOOR_POINTS:
            return (
                "within the noise floor -- not a reliable signal: the style guide "  # noqa: UP031
                "scored %+.1f pts (%.1f -> %.1f over N=%d scored). QE penalties "
                "quantize in 5-20pt steps and temperature is not pinned; raise "
                "--max-segments or widen the gap to confirm"
                % (d, self.off.mean_qe, self.on.mean_qe, n)
            )
        if d > 0:
            return "the style guide improved QE by %.1f points (%.1f -> %.1f over N=%d scored)" % (  # noqa: UP031
                d,
                self.off.mean_qe,
                self.on.mean_qe,
                n,
            )
        return "the style guide REGRESSED QE by %.1f points (%.1f -> %.1f over N=%d scored)" % (  # noqa: UP031
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
            "guide_present": self.guide_present,
            "guide_id": self.guide_id,
            "guide_name": self.guide_name,
            "guide_block": self.guide_block,
            "on": self.on.as_dict(),
            "off": self.off.as_dict(),
            "delta": {"mean_qe": self.qe_delta, "mean_style_errors": self.style_errors_delta},
            "verdict": self.verdict,
            "segments": self.segments,
        }


def _has_text(source) -> bool:
    """A segment the A/B arms can translate: non-empty and not tags-only (#705 gate: a unit
    with only inline tags / one placeholder is copied on every path, so it would spend two
    arm calls and a judge call on identical output and dilute the measured delta)."""
    from app.services.tags_only import is_tags_only

    return bool((source or "").strip()) and not is_tags_only(source)


def run_ab(
    session: Session,
    document: Document,
    locale: str | None = None,
    *,
    provider=None,
    max_segments=DEFAULT_MAX_SEGMENTS,
    org_id=None,
) -> StyleABResult:
    """Run the style-guide A/B measurement over `document` and return a StyleABResult.

    Isolation contract (identical to context_ab.run_ab): arm ON and arm OFF share the
    SAME spec, glossary terms, TM few-shot examples, resolved CONTEXT block and segments;
    they differ ONLY in whether the rendered style-guide block is present. No Segment /
    TM / status is written (a pure read + in-memory score). `org_id` (multi-org) scopes
    BOTH the context and the style-guide resolution exactly like the prompt-path callers
    so a foreign org's asset never leaks in.

    Raises ABError for no-such-project / no translatable segments / a dead pinned engine.
    """
    # AI opt-out (DPAI-08): refuse at the service, so the web, /api, CLI and MCP
    # surfaces are all covered (the document id arrives in a body or a flag).
    from app.services import ai_optout, context_pkg, glossary, qe, style_guides, tm
    from app.services.prompts import translation_prompt
    from app.services.providers import DeadEngineError, MockProvider, get_provider, provider_is_live

    ai_optout.refuse_for_document(session, document, "the style A/B comparison")
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
    is_mock = not provider_is_live(spec.provider)

    # Provider guard on the ARM-TRANSLATE seam (identical to context_ab): a dead/unknown
    # pinned engine ref is converted to the clean ABError contract every surface handles,
    # rather than a raw 500 on the exact "real measurement" path the caller pinned. A
    # keyless REAL provider does NOT raise (it resolves to the mock).
    try:
        get_provider(spec.provider)
    except DeadEngineError as exc:
        raise ABError(
            f"provider '{spec.provider}' is not available for a measurement: {exc}"
        ) from exc

    # The CONTEXT block is HELD CONSTANT across both arms (it is not the asset under
    # test). Resolved once for the doc's scope, exactly as the real translate path does.
    eff_pkg, _prov = context_pkg.resolve(
        session,
        document=document,
        project=project,
        org_id=org_id if org_id is not None else getattr(project, "org_id", None),
    )
    ctx_block = context_pkg.render_block(eff_pkg)

    # The STYLE GUIDE is the TOGGLED asset. Resolve + render ONCE for the doc's scope
    # (customer x content_type + optional pair), the SAME resolution the run path uses.
    guide = style_guides.resolve_style_guide(
        session,
        org_id=org_id if org_id is not None else getattr(project, "org_id", None),
        customer_id=getattr(project, "customer_id", None),
        content_type_id=getattr(project, "content_type_id", None),
        source_locale=src,
        target_locale=chosen,
    )
    guide_block = style_guides.render_style_guide(guide)
    guide_present = bool(guide_block)

    # Front matter composition mirrors translate.py: context THEN style guide, blank-line
    # separated, each already independently fenced. Arm ON gets [ctx, guide]; arm OFF gets
    # [ctx] only. When no context resolves, arm OFF's front matter is "" (None) -> the
    # byte-identical guide-less prompt.
    front_on = "\n\n".join([b for b in (ctx_block, guide_block) if b])
    front_off = ctx_block or ""

    # TM few-shot examples: retrieved ONCE per segment and used IDENTICALLY in both arms.
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

    # A tolerant provider for glossary sense-disambiguation only (a dead ref must not 500
    # a measurement — degrade to mock for the hint, exactly like context_ab / the grid).
    try:
        gloss_provider = get_provider(spec.provider)
    except DeadEngineError:
        gloss_provider = MockProvider()

    result = StyleABResult(
        document_id=document.id,
        locale=chosen,
        provider=spec.provider,
        model=spec.model,
        qe_provider=qe_spec.provider,
        is_mock=is_mock,
        n=len(sample),
        n_available=n_available,
        max_segments=cap,
        guide_present=guide_present,
        guide_id=(guide.id if guide is not None else None),
        guide_name=(guide.name if guide is not None else ""),
        guide_block=guide_block,
    )

    for seg in sample:
        source = seg.source_text
        # Glossary terms: computed ONCE, shared by both arms (identical constraints ->
        # the only difference stays the style-guide block).
        terms = glossary.match_terms(
            session, project.id, source, src, chosen, provider=gloss_provider
        )
        tm_block = context_pkg.render_tm_examples(tm_examples_map.get(source, []))

        # Build the two prompts. The ONLY difference: brief_block carries the style guide
        # (after the constant context) for arm ON, and NOT for arm OFF.
        sys_on, prompt_on, payload_on = translation_prompt(
            source,
            src,
            chosen,
            terms,
            brief_block=front_on or None,
            tm_examples_block=tm_block or None,
        )
        sys_off, prompt_off, payload_off = translation_prompt(
            source,
            src,
            chosen,
            terms,
            brief_block=front_off or None,
            tm_examples_block=tm_block or None,
        )

        text_on = _translate_once(spec, prompt_on, sys_on, payload_on)
        text_off = _translate_once(spec, prompt_off, sys_off, payload_off)

        # Score BOTH arms with the EXISTING MQM judge (never re-implemented). The judge
        # scores raw text in memory — no Segment write, no choke point.
        card_on = qe.score_segment(qe_spec, source, text_on, src, chosen)
        card_off = qe.score_segment(qe_spec, source, text_off, src, chosen)

        _tally(result.on, card_on)
        _tally(result.off, card_off)

        result.segments.append(
            {
                "index": seg.order_index,
                "source": source,
                "on": {
                    "target": text_on,
                    "score": card_on.get("score"),
                    "skipped": card_on.get("skipped"),
                    "rationale": card_on.get("rationale"),
                    "style_errors": _style_count(card_on),
                },
                "off": {
                    "target": text_off,
                    "score": card_off.get("score"),
                    "skipped": card_off.get("skipped"),
                    "rationale": card_off.get("rationale"),
                    "style_errors": _style_count(card_off),
                },
            }
        )

    _finalize_arm(result.on)
    _finalize_arm(result.off)
    return result


def _style_count(card: dict) -> int:
    """The judge's "style"-category error count for one segment (0 when skipped / absent).
    This is the style-adherence signal — the QE judge already tallies MQM categories, so
    the harness reuses that count rather than inventing a second judge."""
    if card.get("skipped"):
        return 0
    return int((card.get("categories") or {}).get("style", 0) or 0)


def _tally(arm: StyleArmResult, card: dict) -> None:
    """Fold one segment's judge card into an arm's running totals. A skipped judge is
    counted but moves neither the QE sum nor the style-error sum."""
    if card.get("skipped"):
        arm.skipped += 1
    else:
        arm.scored += 1
        # accumulate the raw sums (converted to means in _finalize_arm).
        arm.mean_qe = (arm.mean_qe or 0) + (card.get("score") or 0)
        arm.style_errors += _style_count(card)


def _finalize_arm(arm: StyleArmResult) -> None:
    """Turn the running QE + style-error sums into means over scored segments."""
    if arm.scored:
        arm.mean_qe = round(arm.mean_qe / arm.scored, 2)
        arm.mean_style_errors = round(arm.style_errors / arm.scored, 4)
    else:
        arm.mean_qe = None
        arm.mean_style_errors = None
