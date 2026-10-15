"""#160: scope findings at the real fix-prompt seam, before the existing cap.

Also #345 (owner decision 2026-09-29): a numeral/punctuation-only segment whose
target equals its source is CORRECT as-is, so an "identical to source" finding on
it is ADVISORY (neutral, penalty 0, never fails the gate) -- the deterministic
downgrade in lqa_downstream_findings_bloc, exercised here through the real
score_text gate with a canned judge.
"""

import os
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_findings_scope_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, StepType, TransMode, Workflow, WorkflowStep
from app.services import lqa, orchestrator
from app.services.lqa_downstream_findings_bloc import (
    ADVISORY_NUMERAL_SUFFIX,
    _san_finding,
    downgrade_numeral_identical,
    identical_source_finding,
    numeral_only_source,
)
from app.services.providers import ProviderSpec

init_db()

SOURCES = [
    "Meet our flagship arm.",
    "Acme Robotics: precision that works beside you",
]
QUOTED = '2. [Terminology/major] "flagship arm" must use "brazo estrella".'
GLOBAL = "7. [Style/minor] Keep the tone consistent."
META = "(1 finding adjudicated)"


def block(*lines):
    return "\n".join(['<lqa_findings score="80" gate="needs_review">', *lines, "</lqa_findings>"])


def exclusions(value, texts, index):
    matches = {
        line: {
            j
            for j, (source, target) in enumerate(texts)
            if orchestrator._findings_relevant(line, source, target)
        }
        for line in value.splitlines()
        if orchestrator._FINDING_LINE_RE.match(line)
    }
    return frozenset(line for line, owners in matches.items() if owners and index not in owners)


def scoped(value, source=SOURCES[1], target="", **kw):
    texts = [(source, target), (SOURCES[0], "widget number 3")]
    return orchestrator._bound_findings_block(
        value, source, target, excluded_findings=exclusions(value, texts, 0), **kw
    )


def test_filter_and_budget():
    mixed = block(QUOTED, GLOBAL, META)
    assert scoped(mixed, SOURCES[0]) == mixed
    other = scoped(mixed)
    assert QUOTED not in other and GLOBAL in other and META in other, other
    assert "7. [Style/minor]" in other and "1. [Style/minor]" not in other
    assert "excluded as belonging to other segments" in other
    assert other.startswith(mixed.splitlines()[0]) and other.endswith("</lqa_findings>")
    assert scoped(block(QUOTED)) == block("(1 finding excluded as belonging to other segments)")
    metadata_only = scoped(block(QUOTED, META, "(+3 more findings truncated)"))
    assert META in metadata_only and "(+3 more findings truncated)" in metadata_only
    assert QUOTED not in metadata_only
    assert scoped(block(QUOTED, GLOBAL), "", "") == block(QUOTED, GLOBAL)
    assert scoped(block(QUOTED), target="Nuestro brazo estrella.") == block(QUOTED)
    assert scoped("") == ""
    no_findings = block("(no specific findings)")
    assert scoped(no_findings) == no_findings
    # No attributable span, including quotes too short for the shared predicate.
    for note in (GLOBAL, "8. [Style/minor] Use 'a'.", '9. [Style/minor] "   " is blank.'):
        for source in SOURCES:
            assert scoped(block(note), source) == block(note)
    # Reuse the reviewed word boundaries, including curly/backtick quote parsing.
    for quote in ('"widget number 3"', "`widget number 3`", "\u201cwidget number 3\u201d"):
        finding = block(f"9. [Accuracy/major] Fix {quote}.")
        assert "9. [Accuracy/major]" not in scoped(finding, "widget number 39")
        assert scoped(finding, "widget number 3") == finding
    # Filtering does not replace budgeting: retain both honest notes and metadata.
    large = block(
        QUOTED,
        *(f"{i}. [Style/minor] " + "Keep tone consistent. " * 4 for i in range(10, 30)),
        META,
    )
    bounded = scoped(large)
    assert len(bounded) <= orchestrator.LQA_FINDINGS_SEGMENT_CAP
    assert QUOTED not in bounded and "10. [Style/minor]" in bounded
    assert "omitted for this segment" in bounded and "excluded" in bounded and META in bounded
    assert bounded.endswith("</lqa_findings>")
    # Relevant-first budgeting: global findings may be evicted, but both counts survive.
    relevant = '40. [Accuracy/major] Fix "precision".'
    bounded = scoped(
        block(QUOTED, GLOBAL + " Check the whole document carefully." * 3, relevant), cap=220
    )
    assert relevant in bounded and GLOBAL not in bounded, bounded
    assert "(1 finding excluded as belonging to other segments)" in bounded, bounded
    assert "(+1 more finding omitted for this segment)" in bounded, bounded
    # A single unit keeps the pre-existing cap behavior, even for an unrelated span.
    assert orchestrator._bound_findings_block(block(QUOTED), SOURCES[1]) == block(QUOTED)
    relevant_large = block(
        *(f'{i}. [Accuracy/major] Fix "flagship arm". ' + "x" * 75 for i in range(10, 30))
    )
    assert scoped(relevant_large, SOURCES[0]) == orchestrator._bound_findings_block(
        relevant_large, SOURCES[0]
    )


def capture(_spec, prompt, **kw):
    return {
        "text": (kw.get("payload") or {}).get("text", "translated"),
        "provider_used": "mock",
        "model_used": "mock-1",
        "attempts": 1,
        "error": None,
        "cost_usd": 0.0,
        "usage": {},
    }


def test_resolved_prompts():
    step = WorkflowStep(
        workflow_id=0,
        step_type=StepType.LLM_AGENT,
        chunk_policy="none",
        run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
    )
    for template in ("Fix {text}\n{lqa_findings}", "Fix {text}"):
        for value in (block(QUOTED), block(QUOTED, GLOBAL), ""):
            ctx = {
                "source_locale": "en-US",
                "target_locale": "es-419",
                "lqa_findings": value,
            }
            suffix = orchestrator._findings_suffix(step, [step], template, value)
            for i, source in enumerate(SOURCES):
                with patch_pieces(
                    "app.services.orchestrator", "run_completion", side_effect=capture
                ) as spy:
                    orchestrator._run_step_on_segment(
                        ProviderSpec(provider="mock", model="mock-1"),
                        template,
                        ctx,
                        step,
                        source,
                        source,
                        "es-419",
                        [],
                        findings_suffix=suffix,
                        excluded_findings=exclusions(value, [(s, s) for s in SOURCES], i),
                    )
                prompt = spy.call_args.args[1]
                assert (QUOTED in prompt) == (bool(value) and i == 0), prompt
                if value == block(QUOTED, GLOBAL):
                    assert GLOBAL in prompt, prompt
                else:
                    assert GLOBAL not in prompt, prompt
                if not value:
                    assert "<lqa_findings" not in prompt
                    assert orchestrator._LQA_FINDINGS_PREAMBLE not in prompt
            assert ctx["lqa_findings"] == value  # shared context is never mutated


def test_document_dispatch(findings=None, resume=False, document_sources=SOURCES):
    if findings is None:
        findings = block(QUOTED)
    # Exercise document-wide attribution through the real caller.
    for mode, sources in (
        (TransMode.SEGMENTED, document_sources),
        (TransMode.SEGMENTED, document_sources[1:]),
        (TransMode.FULL_TEXT, document_sources),
    ):
        with Session(engine) as session:
            project = Project(name="scope", source_locale="en-US", target_locales="es-419")
            session.add(project)
            session.commit()
            doc = Document(
                project_id=project.id,
                filename="scope.txt",
                fmt="txt",
                mode=mode,
                skeleton={"units": [{"text": s} for s in sources]},
            )
            workflow = Workflow(name="scope")
            session.add(doc)
            session.add(workflow)
            session.commit()
            for i, source in enumerate(sources):
                session.add(
                    Segment(
                        document_id=doc.id,
                        order_index=i,
                        source_text=source,
                        target_locale="es-419",
                    )
                )
            sequence = [("Translate", StepType.LLM_AGENT), ("Check", StepType.LQA)]
            if resume:
                sequence.append(("Review", StepType.HUMAN_REVIEW))
            sequence.append(("Fix", StepType.LLM_AGENT))
            for i, (name, kind) in enumerate(sequence):
                session.add(
                    WorkflowStep(
                        workflow_id=workflow.id,
                        order_index=i,
                        name=name,
                        step_type=kind,
                        provider="mock",
                        chunk_policy="none",
                        prompt_text=f"{name}: {{text}}\n{{lqa_findings}}",
                        tm_examples=False,
                    )
                )
            session.commit()
            with (
                # #163: the orchestrator reads the block AND the span map from one call, so this
                # is the seam to stub. An empty map is the no-span case, i.e. the
                # prose-scrape fallback -- which must stay byte-identical to pre-#163.
                patch.object(lqa, "render_findings_with_spans", return_value=(findings, {})),
                patch_pieces(
                    "app.services.orchestrator", "run_completion", side_effect=capture
                ) as spy,
            ):
                result = orchestrator.run_workflow(session, doc.id, workflow.id, "es-419")
                if resume:
                    assert result["status"] == "paused", result
                    # This quote belonged only to the pre-review target, never the source.
                    assert not any(c.args[1].startswith("Fix:") for c in spy.call_args_list)
                    orchestrator.resume_workflow(
                        session,
                        result["run_id"],
                        "es-419",
                        orchestrator.SEG_JOINER.join("Edited translation." for _ in sources),
                    )
            fixes = [c.args[1] for c in spy.call_args_list if c.args[1].startswith("Fix:")]
            if resume:
                # #344: the {prior} value is now fenced in <content>...</content>.
                assert all(
                    prompt.startswith("Fix: <content>Edited translation.")
                    and "</content>\n<lqa_findings" in prompt
                    for prompt in fixes
                ), fixes
            assert len(fixes) == (1 if mode == TransMode.FULL_TEXT else len(sources)), fixes
            if findings == block(QUOTED) and mode == TransMode.SEGMENTED and len(sources) > 1:
                assert QUOTED in fixes[0] and QUOTED not in fixes[1], fixes
            else:
                for prompt in fixes:
                    assert findings in prompt, fixes


test_filter_and_budget()
test_resolved_prompts()
test_document_dispatch()
for note in (
    "1. [Style/minor] The target's register doesn't match.",
    '1. [Terminology/major] The approved term "brazo estrella" is not used.',
    _san_finding('1. [Accuracy/major] The tag "<b>here</b>" is dropped.'),
):
    test_document_dispatch(block(note), document_sources=[*SOURCES[:1], "Read <b>here</b> now."])
# The translation mock emits the source via payload, so give the pre-review target
# its own quote and then remove it in the operator's edit.
_original_capture = capture


def translated_capture(spec, prompt, **kw):
    result = _original_capture(spec, prompt, **kw)
    if prompt.startswith("Translate:"):
        result["text"] = "Old target phrase."
    return result


capture = translated_capture
test_document_dispatch(block('1. [Accuracy/major] Fix "Old target phrase".'), resume=True)


# ===========================================================================
# #345: numeral-only segments identical to their source are ADVISORY
# ===========================================================================
def test_numeral_predicate():
    for s, want in (
        ("3.", True),
        ("[4]", True),
        ("1)", True),
        ("2025", True),
        ("2 025", True),  # interior whitespace is fine
        ("\u20ac5.00", True),  # currency symbol (S*) glued to a figure
        ("\u00a72.1", True),  # section sign (S*)
        ("\u0663", True),  # Arabic-Indic digit (Nd)
        ("Hello", False),
        ("3.5 items", False),
        ("", False),
        ("   ", False),  # blank is the separate `empty` defect, not this rule
    ):
        assert numeral_only_source(s) is want, (s, want)
    print("  345a. numeral_only_source predicate (N*/P*/Z*/S*, no letters) OK")


def test_identical_source_matcher():
    # Note or dimension, case-insensitive, any capitalisation.
    assert identical_source_finding({"note": "Target identical to source"}, "3.")
    assert identical_source_finding({"dimension": "Accuracy/Untranslated"}, "3.")
    assert identical_source_finding({"note": "Segment was NOT translated"}, "3.")
    assert identical_source_finding({"note": "the text is same as the source"}, "3.")
    # A finding whose span IS the whole target, regardless of wording.
    assert identical_source_finding({"span": "3.", "note": "suspicious"}, "3.")
    # A partial span or an unrelated finding is NOT the identical-source class.
    assert not identical_source_finding({"span": "3", "note": "suspicious"}, "3.")
    assert not identical_source_finding({"note": "mistranslated figure"}, "3.")
    assert not identical_source_finding("not a dict", "3.")
    print("  345b. identical_source_finding matcher (note/dimension phrases, whole-target span) OK")


def test_numeral_downgrade():
    errs = [
        {
            "dimension": "Accuracy/Untranslated",
            "severity": "major",
            "note": "Target identical to source",
        },
        {"dimension": "Style", "severity": "major", "note": "tone off"},
    ]
    assert downgrade_numeral_identical(errs, "3.", "3.") == 1
    assert errs[0]["severity"] == "neutral"
    assert errs[0]["note"].endswith(ADVISORY_NUMERAL_SUFFIX)
    # An unrelated finding on the same segment is untouched.
    assert errs[1] == {"dimension": "Style", "severity": "major", "note": "tone off"}
    # Idempotent: a re-run does not double the suffix.
    downgrade_numeral_identical(errs, "3.", "3.")
    assert errs[0]["note"].count(ADVISORY_NUMERAL_SUFFIX) == 1
    # An EMPTY note gets the marker without its leading space; a re-score must not
    # append it a second time (the leading-space guard would).
    empty = [{"dimension": "Accuracy/Untranslated", "severity": "major", "note": ""}]
    assert downgrade_numeral_identical(empty, "2025", "2025") == 1
    assert empty[0]["note"] == ADVISORY_NUMERAL_SUFFIX.strip()
    downgrade_numeral_identical(empty, "2025", "2025")
    assert empty[0]["note"].count(ADVISORY_NUMERAL_SUFFIX.strip()) == 1
    # Number CHANGED (3. -> 4.): still an error, byte-identical card.
    changed = [{"dimension": "Accuracy/Number", "severity": "major", "note": "value changed"}]
    assert downgrade_numeral_identical(changed, "3.", "4.") == 0
    assert changed[0]["severity"] == "major"
    # A REAL WORD segment identical to its source: untouched.
    word = [
        {
            "dimension": "Accuracy/Untranslated",
            "severity": "major",
            "note": "Target identical to source",
        }
    ]
    assert downgrade_numeral_identical(word, "Hello", "Hello") == 0
    assert word[0]["severity"] == "major"
    print("  345c. downgrade_numeral_identical (count, untouched, idempotent) OK")


class _Judge345:
    """Force lqa's judge live and return a canned findings payload (the
    verify_lqa_adjudication pattern) -- no provider key, no network."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        self._orig = (lqa.provider_is_live, lqa.run_completion)
        lqa.provider_is_live = lambda name: True
        lqa.run_completion = lambda *a, **kw: {
            "text": self.payload,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "usage": {},
            "cost_usd": 0.0,
        }
        return self

    def __exit__(self, *exc):
        lqa.provider_is_live, lqa.run_completion = self._orig
        return False


_UNTRANSLATED_MAJOR = (
    '{"errors":[{"dimension":"Accuracy/Untranslated","severity":"major",'
    '"note":"untranslated, identical to source","span":"3."}]}'
)


def test_numeral_gate():
    args = ("en-US", "es-ES", "", "mock")
    # The #345 fix: each numeral segment now PASSES. Both the judge's major and the
    # structural "Target identical to source" major are downgraded to advisory
    # (neutral) -- the gate reads the whole error list, so both must move.
    for source in ("3.", "[4]", "2025"):
        with _Judge345(_UNTRANSLATED_MAJOR):
            card = lqa.score_text(source, source, *args)
        assert card["passed"] is True and card["score"] == 100, (source, card)
        assert card["gate"] == "auto_approved", (source, card)
        assert len(card["errors"]) == 2, (source, card)
        assert all(e["severity"] == "neutral" for e in card["errors"]), (source, card)
        assert all(e["note"].endswith(ADVISORY_NUMERAL_SUFFIX) for e in card["errors"])
        # Defect 4: where the finding is rendered, it reads as an advisory neutral,
        # never as an error.
        rendered = lqa.render_findings(card)
        assert "/neutral]" in rendered and "advisory: number-only segment" in rendered, rendered
    print("  345d. 3. / [4] / 2025 identical segments PASS the gate, advisory in the render OK")
    # A REAL WORD segment identical to its source still FAILS (untouched by the rule).
    with _Judge345(_UNTRANSLATED_MAJOR):
        card = lqa.score_text("Hello", "Hello", *args)
    assert card["passed"] is False and card["score"] == 90, card
    assert all(e["severity"] == "major" for e in card["errors"]), card
    assert ADVISORY_NUMERAL_SUFFIX not in str(card["errors"])
    print("  345e. Hello -> Hello with an untranslated major still FAILS the gate OK")
    # A numeral segment whose target CHANGED the number is still an error.
    with _Judge345('{"errors":[]}'):
        card = lqa.score_text("3.", "4.", *args)
    assert card["passed"] is False, card
    assert any(e["severity"] in ("major", "critical") for e in card["errors"]), card
    print("  345f. 3. -> 4. is still an error (gate fails) OK")
    # A numeral segment plus ONE unrelated major elsewhere in the card: the other
    # major still fails the gate; only the identical-source findings moved.
    mixed = (
        '{"errors":['
        '{"dimension":"Accuracy/Untranslated","severity":"major","note":"untranslated","span":"3."},'
        '{"dimension":"Style","severity":"major","note":"formatting inconsistent"}]}'
    )
    with _Judge345(mixed):
        card = lqa.score_text("3.", "3.", *args)
    assert card["passed"] is False and card["score"] == 95, card
    sevs = sorted(e["severity"] for e in card["errors"])
    assert sevs == ["major", "neutral", "neutral"], sevs
    majors = [e for e in card["errors"] if e["severity"] == "major"]
    assert len(majors) == 1 and majors[0]["dimension"] == "Style", card
    print("  345g. numeral segment + one unrelated major elsewhere still FAILS the gate OK")


# --- gate round 1 (final review gate, 2026-09-30) ----------------------------
_J = orchestrator.SEG_JOINER
_ARGS345 = ("en-US", "es-ES", "", "mock")


def _blob(*parts):
    return _J.join(parts)


def _finding345(span=None, dim="Accuracy/Untranslated", sev="major", note="untranslated"):
    e = {"dimension": dim, "severity": sev, "note": note}
    if span is not None:
        e["span"] = span
    return e


def _payload345(*findings):
    import json

    return json.dumps({"errors": list(findings)})


def test_numeral_blob_downgrade():
    """HIGH-1: the workflow LQA step scores a SEG_JOINER-joined blob."""
    src = _blob("Install the app", "3.", "Open settings")
    tgt = _blob("Instale la app", "3.", "Abra la configuracion")
    # (a) a finding on the numeral segment is downgraded; the gate passes.
    errs = [_finding345("3.")]
    assert downgrade_numeral_identical(errs, src, tgt) == 1
    assert errs[0]["severity"] == "neutral"
    with _Judge345(_payload345(_finding345("3."))):
        card = lqa.score_text(src, tgt, *_ARGS345)
    assert card["passed"] is True and card["score"] == 100, card
    assert [e["severity"] for e in card["errors"]] == ["neutral"], card
    # (b) a finding on a WORD segment in the same blob stays major.
    with _Judge345(_payload345(_finding345("Instale la app"))):
        card = lqa.score_text(src, tgt, *_ARGS345)
    assert card["passed"] is False, card
    assert card["errors"][0]["severity"] == "major", card
    # (c) a span-less finding on a multi-segment blob cannot be attributed: untouched.
    with _Judge345(_payload345(_finding345())):
        card = lqa.score_text(src, tgt, *_ARGS345)
    assert card["passed"] is False and card["errors"][0]["severity"] == "major", card
    # (d) an identical WORD segment beside a numeral one is NOT rescued by the numeral.
    same = _blob("Hello", "3.")
    with _Judge345(_payload345(_finding345("Hello"))):
        card = lqa.score_text(same, same, *_ARGS345)
    assert card["passed"] is False, card
    assert any(e["severity"] == "major" and e.get("span") == "Hello" for e in card["errors"])
    # (e) numeral segment CHANGED inside a blob stays an error even with a matching span.
    with _Judge345(_payload345(_finding345("4.", dim="Accuracy/Number", note="value changed"))):
        card = lqa.score_text(_blob("Hi", "3."), _blob("Hola", "4."), *_ARGS345)
    assert card["passed"] is False, card
    # (f) an all-numeral blob still downgrades the span-less structural flag.
    allnum = _blob("3.", "[4]", "2025")
    with _Judge345(_payload345(_finding345("3."))):
        card = lqa.score_text(allnum, allnum, *_ARGS345)
    assert card["passed"] is True, card
    # (g) differing segment counts fall back to the whole-string rule (word blob: no-op).
    errs = [_finding345("3.")]
    assert downgrade_numeral_identical(errs, src, _blob("Instale", "3.")) == 0
    print("  345h. multi-segment SEG_JOINER blob: per-segment downgrade, words/span-less stay OK")


def test_numeral_protected_dimensions():
    """MEDIUM-2: locale-format and number-value defects are never advisory."""
    cases = (
        (
            "09/30/2026",
            "de-DE",
            _finding345(
                "09/30/2026", dim="Locale convention", note="Date format not localized for de-DE"
            ),
        ),
        (
            "$1,000.50",
            "fr-FR",
            _finding345(
                "$1,000.50", dim="Locale convention/Number format", note="untranslated currency"
            ),
        ),
        (
            "1,5",
            "en-US",
            _finding345(
                None,
                dim="locale_convention",
                note="Decimal separator not localized; number format left untranslated",
            ),
        ),
        ("3.", "es-ES", _finding345("3.", dim="Accuracy/Number", note="untranslated number")),
        ("3.", "es-ES", _finding345("3.", dim="LOCALE CONVENTION", note="not translated")),
    )
    for text, loc, finding in cases:
        errs = [dict(finding)]
        assert downgrade_numeral_identical(errs, text, text) == 0, (text, finding)
        assert errs[0]["severity"] == "major", (text, errs)
        with _Judge345(_payload345(finding)):
            card = lqa.score_text(text, text, "en-US", loc, "", "mock")
        assert card["passed"] is False, (text, card)
        assert any(
            e["severity"] == "major" and e["dimension"] == finding["dimension"]
            for e in card["errors"]
        ), card
    print("  345i. Locale convention / Accuracy/Number findings never downgraded OK")


def test_numeral_changed_and_whitespace():
    """LOW-3: `target equals source` guard pinned; whitespace normalisation pinned."""
    changed = {"dimension": "Accuracy/Number", "severity": "major", "span": "4."}
    errs = [dict(changed)]
    assert downgrade_numeral_identical(errs, "3.", "4.") == 0 and errs[0]["severity"] == "major"
    # Even a NON-protected dimension with the whole-target span stays major on a change.
    untr = {"dimension": "Accuracy/Untranslated", "severity": "major", "span": "4."}
    errs = [dict(untr)]
    assert downgrade_numeral_identical(errs, "3.", "4.") == 0 and errs[0]["severity"] == "major"
    with _Judge345(_payload345(changed)):
        card = lqa.score_text("3.", "4.", *_ARGS345)
    assert card["passed"] is False, card
    assert any(e["severity"] == "major" and e.get("span") == "4." for e in card["errors"]), card
    with _Judge345(_payload345(untr)):
        card = lqa.score_text("3.", "4.", *_ARGS345)
    assert card["passed"] is False, card
    # Whitespace-normalised equality still counts as identical.
    for s, t in (("3. ", "3."), ("3.", " 3. "), ("2  025", "2 025")):
        errs = [_finding345(t.strip())]
        assert downgrade_numeral_identical(errs, s, t) == 1, (s, t)
        assert errs[0]["severity"] == "neutral"
    print("  345j. 3. -> 4. stays major and fails the gate; whitespace normalisation pinned OK")


def test_numeral_original_severity():
    """LOW-4: the downgrade records what the judge said."""
    errs = [_finding345("3.", sev="major"), _finding345("3.", sev="critical", note="")]
    assert downgrade_numeral_identical(errs, "3.", "3.") == 2
    assert errs[0]["advisory_numeral"] == {"original_severity": "major"}
    assert errs[1]["advisory_numeral"] == {"original_severity": "critical"}
    downgrade_numeral_identical(errs, "3.", "3.")  # idempotent: original is not overwritten
    assert errs[0]["advisory_numeral"] == {"original_severity": "major"}
    with _Judge345(_payload345(_finding345("3."))):
        card = lqa.score_text("3.", "3.", *_ARGS345)
    assert all(e["advisory_numeral"]["original_severity"] == "major" for e in card["errors"]), card
    print("  345k. downgraded findings record original_severity OK")


def test_numeral_workflow_lqa_step():
    """HIGH-1 through the REAL orchestrator LQA step (run-248 shape): translate a
    segmented document whose middle segment is `3.`, judge stubbed to call it
    untranslated; the LQA step must PASS."""
    sources = ["Install the app", "3.", "Open settings"]

    def translate(_spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text", "")
        out = text if numeral_only_source(text) else "ES " + text
        return {
            "text": out,
            "provider_used": "mock",
            "model_used": "mock-1",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    def run(judge_payload):
        with Session(engine) as session:
            project = Project(name="numwf", source_locale="en-US", target_locales="es-ES")
            session.add(project)
            session.commit()
            doc = Document(
                project_id=project.id,
                filename="n.txt",
                fmt="txt",
                mode=TransMode.SEGMENTED,
                skeleton={"units": [{"text": s} for s in sources]},
            )
            workflow = Workflow(name="numwf")
            session.add(doc)
            session.add(workflow)
            session.commit()
            for i, source in enumerate(sources):
                session.add(
                    Segment(
                        document_id=doc.id,
                        order_index=i,
                        source_text=source,
                        target_locale="es-ES",
                    )
                )
            for i, (name, kind) in enumerate(
                [("Translate", StepType.LLM_AGENT), ("Check", StepType.LQA)]
            ):
                session.add(
                    WorkflowStep(
                        workflow_id=workflow.id,
                        order_index=i,
                        name=name,
                        step_type=kind,
                        provider="mock",
                        chunk_policy="none",
                        prompt_text="Translate: {text}",
                        tm_examples=False,
                    )
                )
            session.commit()
            with (
                _Judge345(judge_payload),
                patch_pieces("app.services.orchestrator", "run_completion", side_effect=translate),
            ):
                result = orchestrator.run_workflow(session, doc.id, workflow.id, "es-ES")
            steps = [s for s in result["steps"] if s.get("step_type") == "lqa"]
            assert steps and "lqa" in steps[0], result
            return steps[0]["lqa"]

    ok = run(_payload345(_finding345("3.")))
    assert ok["score"] == 100 and ok["gate"] == "auto_approved", ok
    bad = run(_payload345(_finding345("ES Install the app")))
    assert bad["score"] < 100 and bad["gate"] != "auto_approved", bad
    print("  345l. real workflow LQA step: run-248 shape (numeral in a blob) passes OK")


def test_numeral_wording_required():
    """Gate round 2 MEDIUM-1 / LOW-2 / LOW-4: the span only picks the segment; the
    untranslated / identical wording is required to downgrade."""
    de = ("en-US", "de-DE", "", "mock")

    def gate(src, tgt, finding, args=_ARGS345):
        with _Judge345(_payload345(finding)):
            return lqa.score_text(src, tgt, *args)

    # Fluency date finding spanning an unchanged date: stays major, fails.
    card = gate(
        "09/30/2026",
        "09/30/2026",
        _finding345("09/30/2026", dim="Fluency", note="Date format not localized for de-DE"),
        de,
    )
    assert card["passed"] is False and any(e["severity"] == "major" for e in card["errors"]), card
    card = gate(
        _blob("Due date", "09/30/2026"),
        _blob("Fecha", "09/30/2026"),
        _finding345("09/30/2026", dim="Fluency", note="Date format not localized"),
        de,
    )
    assert card["passed"] is False, card
    # Swap case: number multiset equal, Accuracy major spanning `3.` stays major.
    card = gate(
        _blob("3.", "Step 4.", "Page 3"),
        _blob("3.", "Paso 3.", "Pagina 4"),
        _finding345("3.", dim="Accuracy", note="Step number 4 rendered as 3"),
    )
    assert card["passed"] is False and any(e["severity"] == "major" for e in card["errors"]), card
    # Style major spanning `3.` in a two-segment blob stays major.
    errs = [_finding345("3.", dim="Style", note="spell out numbers below ten")]
    assert (
        downgrade_numeral_identical(errs, _blob("3.", "Step three."), _blob("3.", "Paso 3.")) == 0
    )
    assert errs[0]["severity"] == "major"
    # Whole-target span with a non-untranslated dimension on a single segment: stays.
    errs = [_finding345("3.", dim="Fluency", note="odd")]
    assert downgrade_numeral_identical(errs, "3.", "3.") == 0 and errs[0]["severity"] == "major"
    # Positive: untranslated wording on the numeral segment still downgrades (run 248).
    card = gate(
        _blob("Install the app", "3.", "Open settings"),
        _blob("Instale la app", "3.", "Abra la configuracion"),
        _finding345("3."),
    )
    assert card["passed"] is True and card["score"] == 100, card
    errs = [_finding345("3.", dim="Accuracy/Untranslated", note="neutral wording")]
    assert downgrade_numeral_identical(errs, "3.", "3.") == 1
    # Protected dimension spelling variants are never downgraded.
    for dim in ("Accuracy / Number", "Number format", "Locale Convention", "accuracy_number"):
        errs = [_finding345("3.", dim=dim, note="untranslated")]
        assert downgrade_numeral_identical(errs, "3.", "3.") == 0, dim
        assert errs[0]["severity"] == "major", dim
    # LOW-2: bad_spans exclusion. `3.`->`3.` is fine, `4.`->`3.` changed; a finding
    # spanning `3.` is ambiguous (also the target of the changed segment): untouched.
    errs = [_finding345("3.")]
    assert downgrade_numeral_identical(errs, _blob("3.", "4."), _blob("3.", "3.")) == 0
    assert errs[0]["severity"] == "major"
    # LOW-4: a judge cannot forge the audit record.
    forged = _finding345("3.")
    forged["advisory_numeral"] = {"original_severity": "minor"}
    with _Judge345(_payload345(forged)):
        card = lqa.score_text("3.", "3.", *_ARGS345)
    assert all(e["advisory_numeral"]["original_severity"] == "major" for e in card["errors"]), card
    forged2 = _finding345("Hello", dim="Style", note="x")
    forged2["advisory_numeral"] = {"original_severity": "minor"}
    with _Judge345(_payload345(forged2)):
        card = lqa.score_text("Hello", "Hola", *_ARGS345)
    assert all("advisory_numeral" not in e for e in card["errors"]), card
    print("  345m. wording required to downgrade; swap/date/style stay major; forgery stripped OK")


test_numeral_predicate()
test_identical_source_matcher()
test_numeral_downgrade()
test_numeral_gate()
test_numeral_blob_downgrade()
test_numeral_protected_dimensions()
test_numeral_changed_and_whitespace()
test_numeral_original_severity()
test_numeral_workflow_lqa_step()
test_numeral_wording_required()
print("FINDINGS SEGMENT SCOPE OK")
