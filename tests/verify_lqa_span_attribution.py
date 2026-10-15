"""#163: attribute LQA findings on the judge's structured span, not on quotes scraped
from its prose -- with the prose scrape kept as the fallback that must never delete."""

import os
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_span_attrib_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, StepType, TransMode, Workflow, WorkflowStep
from app.services import lqa, orchestrator

init_db()

# Segment 0 owns "flagship arm". Segment 1 is the tagline the fixer damaged in prod run
# #211 -- the case that motivated #160 and, through it, this train.
SOURCES = [
    "Meet our flagship arm.",
    "Acme Robotics: precision that works beside you",
]

# The note whose PROSE is unattributable: _QUOTED_RE treats an apostrophe as both an
# opener and a closer, so this yields the fragment "s register doesn" -- 16 characters
# that occur in no segment. Its structured span, however, is real text from segment 0.
APOSTROPHE = "1. [Style/minor] The target's register doesn't match the brand's voice."
APOSTROPHE_SPAN = "flagship arm"


def block(*lines):
    return "\n".join(['<lqa_findings score="80" gate="needs_review">', *lines, "</lqa_findings>"])


# --- 1. the extracted primitive ------------------------------------------------------
def test_fragment_occurs_is_the_one_definition():
    occurs = orchestrator._fragment_occurs
    assert occurs("flagship arm", SOURCES[0], "")
    assert not occurs("flagship arm", SOURCES[1], "")
    # word-bounded on the edges, so a longer word does not satisfy a shorter fragment
    assert not occurs("arm", "the army marched", "")
    assert occurs("arm", "an arm, raised", "")
    # too short to attribute with, and the empty/absent cases
    assert not occurs("ab", SOURCES[0], "")
    assert not occurs("", SOURCES[0], "")
    assert not occurs("flagship arm", "", "")
    # `_findings_relevant` must be exactly this predicate over its scraped fragments
    assert orchestrator._findings_relevant('9. [x/y] "flagship arm" is wrong.', SOURCES[0], "")
    assert not orchestrator._findings_relevant('9. [x/y] "flagship arm" is wrong.', SOURCES[1], "")
    assert not orchestrator._findings_relevant(APOSTROPHE, SOURCES[0], "")
    print("  1. _fragment_occurs is the single predicate, and _findings_relevant uses it OK")


# --- 2. the render seam carries the map ----------------------------------------------
def test_render_returns_spans_keyed_by_line_number():
    card = {
        "score": 80,
        "gate": "needs_review",
        "errors": [
            {"dimension": "Style", "severity": "minor", "note": "a", "span": "flagship arm"},
            {"dimension": "Accuracy", "severity": "major", "note": "b"},
            {"dimension": "Fluency", "severity": "minor", "note": "c", "span": "   "},
        ],
    }
    rendered, spans = lqa.render_findings_with_spans(card)
    # the map keys on the SAME number the line carries
    assert spans == {1: "flagship arm"}, spans
    assert "1. [Style/minor] a" in rendered and "2. [Accuracy/major] b" in rendered
    # a missing span and a whitespace-only span are both simply absent, never ""
    assert 2 not in spans and 3 not in spans
    # the thin wrapper still returns the block alone for every other caller
    assert lqa.render_findings(card) == rendered
    assert lqa.render_findings_with_spans({}) == ("", {})
    assert lqa.render_findings_with_spans({"gate": "x"}) == ("", {})

    # THE ONLY WAY THE KEY CAN SKEW. `render_findings` numbers `enumerate(errors, 1)` over
    # the NON-adjudicated findings, so a map keyed on the unfiltered list would be off by
    # one for every finding after a withheld one. An adjudicated error FIRST is the case
    # that catches it: keys must be 1..N over what was actually rendered, and a withheld
    # finding must contribute no key at all.
    withheld_first = {
        "score": 80,
        "gate": "needs_review",
        "errors": [
            {
                "dimension": "Terminology",
                "severity": "neutral",
                "note": "approved term",
                "span": "NEVER-RENDERED",
                "adjudicated": {"rule": "glossary-approved-term"},
            },
            {"dimension": "Style", "severity": "minor", "note": "b", "span": "SEG1ONLY"},
            {"dimension": "Accuracy", "severity": "major", "note": "c", "span": "SEG0ONLY"},
        ],
    }
    rendered2, spans2 = lqa.render_findings_with_spans(withheld_first)
    numbers = [int(ln.split(".", 1)[0]) for ln in rendered2.splitlines() if ln[:1].isdigit()]
    assert numbers == [1, 2], numbers
    assert spans2 == {1: "SEG1ONLY", 2: "SEG0ONLY"}, spans2
    assert "NEVER-RENDERED" not in str(spans2.values())
    assert "adjudicated" in rendered2  # the honest withheld line still rides along
    print("  2. render_findings_with_spans keys the map on the rendered number OK")


# --- 3. the judge is always asked for the span ---------------------------------------
def test_span_requested_unconditionally():
    assert '"span" key' in lqa._SPAN_CLAUSE
    assert '"span" key' not in lqa._glossary_clause(
        [{"source_term": "widget", "target_term": "aparato"}], "Compra el aparato hoy."
    )
    # Pin the WORDING, not just the token. This sentence is the only thing that makes the
    # judge emit the field the orchestrator now attributes on, and `verify_postedit` pins
    # the assembled prompt byte-for-byte -- so a reword here must be a deliberate edit in
    # two places, never a passing tweak. It no longer opens "Also": it used to trail the
    # approved-terminology block and now stands alone.
    assert lqa._SPAN_CLAUSE.startswith('Give EVERY error object a "span" key')
    assert "copied verbatim and never paraphrased" in lqa._SPAN_CLAUSE
    print("  3. the span request lives outside the glossary clause, wording pinned OK")


# --- 4. attribution end to end -------------------------------------------------------
def _run(findings, spans, sources=SOURCES, *, suffix_channel=False, adaptive=False):
    """Run Translate -> LQA -> Fix over the two segments and return the Fix prompts."""
    captured = []

    def capture(_spec, prompt, **kw):
        captured.append(prompt)
        return {
            "text": (kw.get("payload") or {}).get("text", "translated"),
            "provider_used": "mock",
            "model_used": "mock-1",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    with Session(engine) as session:
        project = Project(name="span attribution", source_locale="en-US")
        session.add(project)
        session.commit()
        doc = Document(project_id=project.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        session.add(doc)
        session.commit()
        for i, text in enumerate(sources):
            session.add(
                Segment(document_id=doc.id, order_index=i, source_text=text, target_locale="es-419")
            )
        workflow = Workflow(name="span wf", source_locale="en-US")
        session.add(workflow)
        session.commit()
        for i, (name, kind) in enumerate(
            [
                ("Translate", StepType.LLM_AGENT),
                ("Check", StepType.LQA),
                ("Fix", StepType.LLM_AGENT),
            ]
        ):
            session.add(
                WorkflowStep(
                    workflow_id=workflow.id,
                    order_index=i,
                    name=name,
                    step_type=kind,
                    provider="mock",
                    chunk_policy="none",
                    prompt_text=(
                        # No {lqa_findings} on the Fix step forces the AUTO-APPEND suffix
                        # channel instead of the placeholder channel. They are separate
                        # call sites into _bound_findings_block and each must carry the
                        # attribution map.
                        f"{name}: {{text}}"
                        if suffix_channel and kind == StepType.LLM_AGENT and i == 2
                        else f"{name}: {{text}}\n{{lqa_findings}}"
                    ),
                    adaptive_feedback=bool(adaptive and kind == StepType.LLM_AGENT and i == 2),
                    # The suffix channel only fires for a CORRECTION-LOOP-shaped step, so
                    # the Fix step has to really be one: keyed, and named as the check's
                    # on_fail target. Without this the auto-append returns "" and the Fix
                    # prompt carries no findings at all -- which would look exactly like a
                    # scoping failure while proving nothing.
                    key=("fix" if suffix_channel and i == 2 else None),
                    branch=(
                        {"on_fail": {"step": "fix"}}
                        if suffix_channel and kind == StepType.LQA
                        else None
                    ),
                    tm_examples=False,
                )
            )
        session.commit()
        with (
            patch.object(lqa, "render_findings_with_spans", return_value=(findings, spans)),
            patch_pieces("app.services.orchestrator", "run_completion", side_effect=capture),
        ):
            orchestrator.run_workflow(session, doc.id, workflow.id, "es-419")
    return [p for p in captured if p.startswith("Fix:")]


def test_span_scopes_what_prose_cannot():
    # PROSE ALONE: the apostrophe fragment matches nothing, so the finding is
    # unattributable and -- per #160 -- must reach BOTH segments. This is the pre-#163
    # behaviour and the fallback path; it must stay exactly this.
    fixes = _run(block(APOSTROPHE), {})
    assert len(fixes) == 2, fixes
    assert all(APOSTROPHE in p for p in fixes), fixes

    # WITH THE SPAN: the judge told us which text the error is about, so it is scoped to
    # segment 0 and kept out of segment 1's prompt. This is the whole point of #163.
    fixes = _run(block(APOSTROPHE), {1: APOSTROPHE_SPAN})
    assert APOSTROPHE in fixes[0], fixes
    assert APOSTROPHE not in fixes[1], fixes
    assert "excluded as belonging to other segments" in fixes[1], fixes
    print("  4. a span scopes a finding whose prose is unattributable OK")


def test_span_override_is_strict_over_attributable_prose():
    """#164: when a span matches, the prose answer is DISCARDED, not unioned back in.

    Every other span case here scopes a finding whose prose matches NO segment, so a
    mutant that unions the two answers (`return hits | <prose hits>`) computes the SAME
    set -- {} | {0} == {0} -- and the whole suite stays green. That is the gate miss
    this case closes: one MQM error object = one span, so a span that hits must scope
    the finding even when its prose quotes text present in BOTH segments.

    The prose below quotes "flagship arm", which occurs in both sources; the span names
    text unique to segment 0. Strict override: the finding reaches segment 0 only.
    Union mutant: the prose hits pull it back into segment 1 and the exclusion note
    disappears.
    """
    both = ["Meet our flagship arm.", "Try our flagship arm today."]
    quoted = '1. [Accuracy/minor] Keep "flagship arm" consistent across the page.'
    # Precondition that makes this case bite: WITHOUT the span the prose would attribute
    # this finding to BOTH segments, so the only thing keeping it out of segment 1 is the
    # strict override.
    assert orchestrator._findings_relevant(quoted, both[0], "")
    assert orchestrator._findings_relevant(quoted, both[1], "")

    fixes = _run(block(quoted), {1: "Meet our"}, sources=both)
    assert len(fixes) == 2, fixes
    assert quoted in fixes[0], fixes
    assert quoted not in fixes[1], (
        "the span names only segment 0, so the strict override must keep this finding "
        "out of segment 1 even though its prose quotes text present in both -- a union "
        "of span hits and prose hits would let it through"
    )
    # and the note the code writes on segment 1 names the exclusion: the exact string
    # `_bound_findings_block` emits, count included (the same assertion form as case 9).
    assert "(1 finding excluded as belonging to other segments)" in fixes[1], fixes[1]
    print(" 10. the span override is strict: attributable prose does not union back in OK")


def test_hallucinated_span_falls_back_and_never_deletes():
    # A span that occurs in NO segment (paraphrased or invented). It must not be treated
    # as "belongs somewhere else" -- the finding falls back to prose and, failing that,
    # stays on every segment. Brittleness costs SCOPING, never CONTENT.
    fixes = _run(block(APOSTROPHE), {1: "text that appears nowhere at all"})
    assert len(fixes) == 2, fixes
    assert all(APOSTROPHE in p for p in fixes), fixes

    # ...and a hallucinated span on a finding whose PROSE is attributable still lands on
    # the prose answer rather than vanishing.
    quoted = '1. [Terminology/major] "flagship arm" must use "brazo estrella".'
    fixes = _run(block(quoted), {1: "nowhere"})
    assert quoted in fixes[0] and quoted not in fixes[1], fixes
    print("  5. a hallucinated span falls back to prose and never deletes OK")


def test_legacy_rows_and_resumed_runs_use_the_fallback():
    # An LQAResult written before this train has span == "", and a run paused before it
    # and resumed after has no map at all. Both are the empty-map case, and both must
    # behave exactly as the prose-only path did.
    quoted = '1. [Terminology/major] "flagship arm" must use "brazo estrella".'
    with_empty_map = _run(block(quoted), {})
    assert quoted in with_empty_map[0] and quoted not in with_empty_map[1], with_empty_map
    # an empty-string span is not a span
    with_blank = _run(block(quoted), {1: ""})
    assert with_blank == with_empty_map, (with_blank, with_empty_map)
    print("  6. legacy rows / resumed runs fall back byte-identically OK")


def test_over_cap_ordering_uses_the_callers_attribution():
    """The cap is where this fix is actually DELIVERED, and nothing else here reaches it.

    Every other case in this file builds a block far under LQA_FINDINGS_SEGMENT_CAP (700),
    so the ordering path is never entered -- which means span-first ordering could revert
    entirely and this suite would stay green. Two regressions live here:

      * drop the caller's map -> the cap re-sorts on PROSE, and a finding whose prose is
        unattributable but whose span names this segment is admitted and then truncated;
      * hand a SINGLE-segment caller an empty frozenset instead of None -> nothing counts
        as relevant, so the finding that IS about the only segment sorts last and is lost.
    """
    filler = [
        f"{i}. [Accuracy/major] Something generic and unattributable about wording {i}."
        for i in range(1, 10)
    ]
    mine = '10. [Terminology/major] "flagship arm" must use "brazo estrella" here.'
    over_cap = block(*filler, mine)
    assert len(over_cap) > 700, len(over_cap)

    # (a) the caller says "this one is mine" -> ordered first, survives the cap, without
    # its prose or its span being consulted here at all.
    kept = orchestrator._bound_findings_block(
        over_cap, SOURCES[0], "", relevant_findings=frozenset([mine])
    )
    assert mine in kept, kept
    assert "omitted for this segment" in kept

    # (b) the caller has no opinion (the single-unit path) -> the prose predicate still
    # orders, exactly as before this train. `mine` quotes text that IS in SOURCES[0].
    assert mine in orchestrator._bound_findings_block(over_cap, SOURCES[0], "")

    # (c) an EMPTY set is "nothing is relevant", NOT "no opinion". If a caller ever hands
    # frozenset() where it means None, this is the finding the cap throws away first.
    empty_opinion = orchestrator._bound_findings_block(
        over_cap, SOURCES[0], "", relevant_findings=frozenset()
    )
    assert mine not in empty_opinion, (
        "frozenset() must not read as 'no opinion' -- if this starts passing, some caller "
        "is handing an empty set where it means None"
    )
    print("  7. the cap orders on the caller's attribution, and None != frozenset() OK")


def test_span_scoped_finding_survives_the_cap_end_to_end():
    """THE WIRE, end to end. This is the case the whole train is for.

    The two cases above exercise `_bound_findings_block` directly, so they prove the CALLEE
    honours a map it is handed. They say nothing about whether the orchestrator still HANDS
    it one. Cut that wire and the finding this train exists to deliver reaches NOBODY --
    excluded from the other segment as belonging elsewhere, and truncated out of its own by
    prose ordering.

    EVERY cut point is covered here, and each was mutation-tested alone:

      * orchestrator.py  `findings_relevant[first]` -- the synchronous warm-up segment that
        every segmented step dispatches before the fan-out;
      * orchestrator.py  `findings_relevant[j]` in the thread-pool fan-out;
      * orchestrator.py  `findings_relevant[j]` in the adaptive-feedback serial branch;
      * orchestrator_part8.py, BOTH channels -- the `{lqa_findings}` placeholder and the
        `_findings_suffix` auto-append;
      * orchestrator_part3.py `_relevant` -- the callee ignoring the map (cases 7 and 8).

    Round 3 of the gate blocked this train because only one of those was covered while the
    commit claimed all of them. Do not narrow this case without narrowing that claim.
    """
    filler = [
        f"{i}. [Accuracy/major] Something generic and unattributable about wording {i}."
        for i in range(1, 12)
    ]
    # One span-scoped finding per segment. BOTH are needed: segment 0 is dispatched through
    # the synchronous warm-up (`first = pending_idx[0]`, which every segmented step runs)
    # and segment 1 through the fan-out, and those are SEPARATE call sites. Asserting only
    # about segment 1 leaves the warm-up unguarded, which is the more common path.
    seg0 = "12. [Style/major] The brand's voice doesn't survive here."
    seg1 = "13. [Style/major] The target's register doesn't match."
    over_cap = block(*filler, seg0, seg1)
    assert len(over_cap) > 700, len(over_cap)
    spans = {12: "flagship arm", 13: "precision that works beside you"}

    # Three shapes, because the map reaches the cap through three different routes and a
    # revert on any one of them must be caught: the {lqa_findings} placeholder channel, the
    # auto-append suffix channel, and the adaptive-feedback serial dispatch.
    for label, kw, both in (
        ("placeholder channel", {}, True),
        ("suffix channel", {"suffix_channel": True}, True),
        # The adaptive branch DOES dispatch both segments serially; the second prompt is
        # simply invisible to `_run`, whose filter is `startswith("Fix:")` and whose
        # segment-1 prompt opens with the feedback block instead. So assert what this
        # harness can actually see -- segment 0 through the serial branch, keeping its own
        # finding -- rather than a count that would fail for a reason unrelated to scoping.
        ("adaptive serial dispatch", {"adaptive": True}, False),
    ):
        fixes = _run(over_cap, spans, **kw)
        assert fixes, (label, "no Fix prompt was produced at all")
        assert seg0 in fixes[0], (
            f"{label}: the finding whose span names segment 0 was truncated out of "
            "segment 0 -- the attribution map is not reaching this dispatch route"
        )
        assert seg1 not in fixes[0], (label, fixes[0])
        # exact count, so a FILLER being excluded instead could not satisfy this
        assert "(1 finding excluded as belonging to other segments)" in fixes[0], (label, fixes[0])
        if both:
            assert len(fixes) == 2, (label, fixes)
            assert seg1 in fixes[1], (
                f"{label}: the finding whose span names segment 1 was truncated out of "
                "segment 1 -- the attribution map is not reaching the cap"
            )
            assert seg0 not in fixes[1], (label, fixes[1])
            assert "(1 finding excluded as belonging to other segments)" in fixes[1], (
                label,
                fixes[1],
            )
    print("  9. a span-scoped finding survives the cap on its own segment, all 3 routes OK")


def test_single_segment_document_keeps_prose_ordering():
    """End to end: a ONE-segment document must behave exactly as before this train.

    `finding_matches` is gated on len(segments) > 1, so a single-segment run has an EMPTY
    map. Deriving `findings_relevant` from it without the same gate yields [frozenset()],
    which disables relevance ordering and drops the finding about the only segment.
    """
    filler = [
        f"{i}. [Accuracy/major] Something generic and unattributable about wording {i}."
        for i in range(1, 10)
    ]
    mine = '10. [Terminology/major] "flagship arm" must use "brazo estrella" here.'
    fixes = _run(block(*filler, mine), {}, sources=[SOURCES[0]])
    assert len(fixes) == 1, fixes
    assert mine in fixes[0], fixes
    print("  8. a single-segment document keeps its prose ordering at the cap OK")


test_fragment_occurs_is_the_one_definition()
test_render_returns_spans_keyed_by_line_number()
test_span_requested_unconditionally()
test_span_scopes_what_prose_cannot()
test_hallucinated_span_falls_back_and_never_deletes()
test_legacy_rows_and_resumed_runs_use_the_fallback()
test_over_cap_ordering_uses_the_callers_attribution()
test_single_segment_document_keeps_prose_ordering()
test_span_scoped_finding_survives_the_cap_end_to_end()
test_span_override_is_strict_over_attributable_prose()
print("SPAN ATTRIBUTION OK")
