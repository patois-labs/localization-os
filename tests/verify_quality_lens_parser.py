"""Quality Lenses T1 -- the fail-closed findings parser (SPEC-quality-lenses-p2 section 3.2).

THE PARSER IS WHERE FAIL-OPEN BUGS LIVE. Every shape below either lands as a real verdict or
raises `LensVerdictUnreadable`; nothing in between, and no partial accept. The red team the
spec names in section 8 (T1 Risks) is run here in full: truncated arrays, prose-wrapped
verdicts, template echoes, off-vocabulary severities, spans that do not occur, spans occurring
TWICE, reversed and negative offsets, unicode offsets, a 500-finding list, `{"findings": {}}`,
a bare top-level array, an all-string array, and an omission carrying only a target span.

COUNTERFACTUAL DISCIPLINE. Each guard is paired with `_naive`, a deliberately weakened parser
that keeps the tolerant `llm_json` load and the `reject` belt but drops the post-parse guards.
Every rejection test asserts BOTH that the real parser raises AND that the weakened one
returns a verdict that scores a CLEAN PASS. A test that passes both before and after defends
nothing.

Named section 9 counterfactuals proven here:
  * fast-path fail-open (H1): with the post-parse backstops removed but the `reject` belt
    intact, a clean stray dict auto-approves;
  * a bare top-level array of strings scores a false clean pass without the H2 arm;
  * cap truncation silently improves the score (P2-D10);
  * a dropped unlocatable span raises the score;
  * a judge-authored `adjudication_outcome` silences its own major finding (the provenance
    strip).

Final line: "QUALITY LENS PARSER OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qlparse_")

from sqlmodel import Session

from app import db
from app.services import llm_json
from app.services import quality_lens as ql

SOURCE = "Book the flight now and pay later"
TARGET = "Reserve el vuelo ahora y pague el vuelo despues"
# "el vuelo" occurs TWICE in TARGET -- the L1 collision fixture.


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _lenses():
    with Session(db.engine) as s:
        rows = ql.ensure_builtins(s)
        # Detach: these fixtures only read configuration.
        s.expunge_all()
        return rows


def _naive(text, lens):
    """THE WEAKENED PARSER: the tolerant load and the `reject` belt, with every post-parse
    guard removed. This is what "trusting the belt" actually ships."""
    try:
        data = llm_json.loads(text, reject=ql._reject_non_findings)
    except Exception:
        return []  # the soft-degrade that turns an unreadable verdict into a clean pass
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict):
        return []
    raw = data.get("findings")
    raw = raw if isinstance(raw, list) else []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        span = e.get("span")
        if isinstance(span, str) and span not in lens_target(e):
            continue  # the "drop what we cannot locate" shortcut
        out.append(
            {
                "kind": e.get("kind") or "span",
                "severity": str(e.get("severity") or "minor").strip().lower(),
                "origin": "judge",
                "blocking": False,
                "adjudication_outcome": e.get("adjudication_outcome"),
            }
        )
    return out[: int(getattr(lens, "max_findings", 5))]  # the silent truncation


def lens_target(_e):
    return TARGET


def _naive_passes(text, lens):
    """True when the weakened parser AUTO-APPROVES this text: the gate passes on a verdict
    the real parser refuses to score. The number differs per shape (a dropped finding scores
    100, an off-vocabulary severity scores 99 through `weights.get(sev, 1)`); what makes it a
    fail-open is that the gate PASSES either way."""
    findings = _naive(text, lens)
    _value, passed, _ids = ql.score_findings(lens, findings)
    return passed


def _rejects(label, text, lens, *, expect_naive_pass=True):
    """TWO CLASSES OF GUARD, told apart honestly by `expect_naive_pass`.

    `True` (the default): dropping this guard AUTO-APPROVES flagged content, and the test
    proves it by scoring the weakened parser's output. That is a fail-open.

    `False`: dropping the guard does not open the gate, it MISLOCATES or MISLABELS the
    finding -- a span at the wrong offsets, an omission carrying target offsets, a category
    outside the taxonomy. The cost lands downstream instead: T2's meta-eval overlap is
    computed per kind against target and source offsets (N-M3), so a mislabelled finding
    scores coincidental hits, and a surface renders a span that is not the flagged text.
    Claiming a fail-open counterfactual for these would be a false claim, so the flag says
    which kind of defect each guard actually prevents."""
    try:
        ql.parse_findings(text, lens, SOURCE, TARGET)
    except ql.LensVerdictUnreadable:
        pass
    else:
        _fail(f"the parser ACCEPTED {label}: it must reject the whole verdict")
    if expect_naive_pass and not _naive_passes(text, lens):
        _fail(f"counterfactual vacuous for {label}: the weakened parser did not auto-approve")


def _accepts(label, text, lens):
    try:
        return ql.parse_findings(text, lens, SOURCE, TARGET)
    except ql.LensVerdictUnreadable as exc:
        _fail(f"the parser rejected a LEGITIMATE verdict ({label}): {exc}")


# ===========================================================================
# 1. The envelope (H1, H2)
# ===========================================================================
def test_zero_finding_shapes_are_legitimate():
    esa = _lenses()["esa"]
    for label, text in (
        ("an explicit empty findings list", '{"findings":[]}'),
        ("a bare empty array", "[]"),
        ("a prose-wrapped empty array", "No issues found:\n\n[]\n\nHope that helps!"),
    ):
        out = _accepts(label, text, esa)
        if out != []:
            _fail((label, out))
    print("  1a. the three zero-finding shapes stay a legitimate clean pass OK")


def test_top_level_array_is_normalized():
    esa = _lenses()["esa"]
    out = _accepts(
        "a bare top-level findings array",
        '[{"span":"Reserve","severity":"major","note":"sense"}]',
        esa,
    )
    if len(out) != 1 or out[0]["severity"] != "major":
        _fail(("a top-level array is a legitimate emission (H2)", out))
    # ...and the same content wrapped is identical.
    wrapped = _accepts(
        "the wrapped twin",
        '{"findings":[{"span":"Reserve","severity":"major","note":"sense"}]}',
        esa,
    )
    if wrapped != out:
        _fail("wrapped and unwrapped emissions must parse identically")
    print(
        "  1b. a top-level ARRAY is normalized to a verdict and parses identically to its "
        "wrapped twin (H2) OK"
    )


def test_all_non_dict_array_rejects():
    esa = _lenses()["esa"]
    _rejects("a bare array of strings", '["Accuracy: wrong verb","Style: too casual"]', esa)
    _rejects(
        "the WRAPPED all-string list", '{"findings":["Accuracy: wrong verb","Style: casual"]}', esa
    )
    print(
        "  1c. an all-non-dict array rejects in BOTH the bare and wrapped shapes; the "
        "weakened parser filters both to zero findings and scores a false clean pass (H2) OK"
    )


def test_non_verdict_json_values_reject():
    esa = _lenses()["esa"]
    for label, text in (
        ("a bare string", '"looks fine to me"'),
        ("a bare number", "42"),
        ("a bare boolean", "true"),
        ("a bare null", "null"),
    ):
        _rejects(label, text, esa)
    print(
        "  1d. a non-array, non-object JSON value rejects EXPLICITLY rather than by an "
        "incidental later exception OK"
    )


def test_fast_path_stray_dict_rejects():
    """H1, the named counterfactual. `llm_json`'s reject belt screens TOLERANT-path candidates
    only and explicitly does NOT screen a clean single value returned by the fast path, so a
    reader who trusts the belt ships a fail-open."""
    esa = _lenses()["esa"]
    for label, text in (
        ("a clean stray dict", '{"note":"looks fine"}'),
        ("an empty object", "{}"),
        ("a non-list findings value", '{"findings":"none"}'),
        ("an object-valued findings key", '{"findings":{}}'),
        ("a null findings value", '{"findings":null}'),
    ):
        # The belt itself does not even see these: the fast path returns them directly.
        if ql._reject_non_findings(json.loads(text)) and text == '{"note":"looks fine"}':
            pass  # the belt WOULD reject it on the tolerant path; the fast path never asks
        _rejects(label, text, esa)
    print(
        "  1e. the CLEAN FAST PATH is closed by the unconditional post-parse backstops: a "
        'stray dict, {}, {"findings":{}} and a non-list findings all reject, while the '
        "belt-only parser auto-approves every one (H1) OK"
    )


def test_prose_and_fence_wrappers_are_tolerated():
    esa = _lenses()["esa"]
    body = '{"findings":[{"span":"Reserve","severity":"minor","note":"register"}]}'
    for label, text in (
        ("a prose preamble", f"Here is the JSON output:\n\n{body}\n\nHope that helps!"),
        ("a json fence", f"```json\n{body}\n```"),
        ("trailing prose", f"{body}\n\nThat is my final answer."),
    ):
        out = _accepts(label, text, esa)
        if len(out) != 1:
            _fail((label, out))
    print("  1f. tolerance is for the WRAPPER, not the content: prose and fences still land OK")


def test_truncated_verdict_rejects():
    esa = _lenses()["esa"]
    truncated = '{"findings":[{"span":"Reserve","severity":"critical","note":"the whole clause'
    _rejects("a truncated verdict", truncated, esa)
    print(
        "  1g. a truncated (finish_reason=length) verdict fails CLOSED; the weakened "
        "parser's except-return-[] soft degrade would score it 100 OK"
    )


def test_template_echo():
    esa = _lenses()["esa"]
    echo = (
        '{"findings":[{"kind":"span","span":"...","severity":"...","category":null,"note":"..."}]}'
    )
    _rejects("a bare template echo", echo, esa, expect_naive_pass=False)
    # ...and an echo in the PREAMBLE does not mask the real verdict that follows it.
    real = '{"findings":[{"span":"Reserve","severity":"major","note":"sense"}]}'
    out = _accepts("an echo followed by a real verdict", f"Template: {echo}\n\n{real}", esa)
    if len(out) != 1 or out[0]["severity"] != "major":
        _fail(("the belt must skip the echo so the real verdict wins", out))
    print(
        "  1h. a template echo rejects on its own (its placeholder severity is off-vocab) "
        "and, in a preamble, is skipped so the REAL verdict after it lands OK"
    )


# ===========================================================================
# 2. Per-finding shape: kind, spans, severity, category
# ===========================================================================
def test_unknown_kind_rejects():
    esa = _lenses()["esa"]
    _rejects(
        "an unknown finding kind",
        '{"findings":[{"kind":"vibes","span":"Reserve","severity":"minor","note":"x"}]}',
        esa,
        expect_naive_pass=False,
    )
    print("  2a. an unknown finding kind rejects OK")


def test_span_must_occur():
    esa = _lenses()["esa"]
    _rejects(
        "a span that does not occur in the target",
        '{"findings":[{"span":"Cancelar la reserva","severity":"major","note":"invented"}]}',
        esa,
    )
    print(
        "  2b. a span that does not occur REJECTS the verdict; the weakened parser drops "
        "the finding instead, which RAISES the score to a clean 100 OK"
    )


def test_repeated_span_needs_qualification():
    """L1: a string span occurring more than once requires explicit offsets or an
    `occurrence` ordinal. Scoring the wrong occurrence mislocates the error silently."""
    esa = _lenses()["esa"]
    if TARGET.count("el vuelo") != 2:
        _fail("fixture: 'el vuelo' must occur twice")
    _rejects(
        "an ambiguous repeated span",
        '{"findings":[{"span":"el vuelo","severity":"major","note":"ambiguous"}]}',
        esa,
        expect_naive_pass=False,
    )
    second = TARGET.index("el vuelo", TARGET.index("el vuelo") + 1)
    out = _accepts(
        "a repeated span qualified by an ordinal",
        '{"findings":[{"span":"el vuelo","occurrence":2,"severity":"major","note":"x"}]}',
        esa,
    )
    if out[0]["span_start"] != second:
        _fail(("the ordinal must select the SECOND occurrence", out[0]["span_start"], second))
    out2 = _accepts(
        "a repeated span qualified by offsets",
        f'{{"findings":[{{"span":"el vuelo","start":{second},'
        f'"end":{second + len("el vuelo")},"severity":"major","note":"x"}}]}}',
        esa,
    )
    if out2[0]["span_start"] != second:
        _fail("explicit offsets must qualify a repeated span")
    for label, text in (
        (
            "an occurrence ordinal past the last occurrence",
            '{"findings":[{"span":"el vuelo","occurrence":3,"severity":"major","note":"x"}]}',
        ),
        (
            "a zero occurrence ordinal",
            '{"findings":[{"span":"el vuelo","occurrence":0,"severity":"major","note":"x"}]}',
        ),
    ):
        _rejects(label, text, esa, expect_naive_pass=False)
    print(
        "  2c. a repeated span rejects unqualified, resolves to the NAMED occurrence with "
        "an ordinal or offsets, and rejects an out-of-range ordinal (L1) OK"
    )


def test_occurrence_is_non_overlapping_and_range_checked():
    """L1 + L2, both on the ordinal path.

    L1: `str.count` counts NON-overlapping occurrences, so the ordinal must resolve the same
    way. Advancing the cursor by ONE character resolved occurrence 2 of "aa" in "aaaa" to
    index 1 -- an overlap `count` never counted, so the ordinal and the count were measuring
    different things and the finding landed on a span the judge did not mean.

    L2: an out-of-range ordinal was range-checked ONLY when the span occurred more than once;
    on a span occurring once it was silently coerced to 1, so a judge that named occurrence 4
    had occurrence 1 scored instead."""
    esa = _lenses()["esa"]
    target = "aaaa"
    if target.count("aa") != 2:
        _fail("fixture: str.count is non-overlapping, so 'aa' in 'aaaa' counts 2")
    out = ql.parse_findings(
        '{"findings":[{"span":"aa","occurrence":2,"severity":"minor","note":"n"}]}',
        esa,
        SOURCE,
        target,
    )
    if out[0]["span_start"] != 2:
        _fail(
            "occurrence 2 of 'aa' in 'aaaa' must be the NON-OVERLAPPING one at index 2; "
            f"got {out[0]['span_start']} (the one-character-advance bug)"
        )
    # ...and the ordinal past the last non-overlapping occurrence rejects.
    for ordinal in (3, 0, -1):
        try:
            ql.parse_findings(
                f'{{"findings":[{{"span":"aa","occurrence":{ordinal},'
                f'"severity":"minor","note":"n"}}]}}',
                esa,
                SOURCE,
                target,
            )
        except ql.LensVerdictUnreadable:
            continue
        _fail(f"occurrence {ordinal} must reject against a count of 2")
    # L2: a span occurring ONCE still range-checks its ordinal.
    once = '{"findings":[{"span":"Reserve","occurrence":4,"severity":"minor","note":"n"}]}'
    try:
        ql.parse_findings(once, esa, SOURCE, TARGET)
    except ql.LensVerdictUnreadable:
        pass
    else:
        _fail("an out-of-range ordinal on a single-occurrence span must reject, not coerce to 1")
    ok = ql.parse_findings(
        '{"findings":[{"span":"Reserve","occurrence":1,"severity":"minor","note":"n"}]}',
        esa,
        SOURCE,
        TARGET,
    )
    if ok[0]["span_start"] != TARGET.index("Reserve"):
        _fail("occurrence 1 on a single-occurrence span is legitimate")
    print(
        "  2c2. the occurrence ordinal is NON-OVERLAPPING (matching str.count) and is "
        "range-checked on every finding that supplies one, not only on ambiguous spans "
        "(L1, L2) OK"
    )


def test_offset_bounds():
    esa = _lenses()["esa"]
    for label, start, end in (
        ("negative offsets", -3, 4),
        ("reversed offsets", 12, 4),
        ("equal offsets (an empty span)", 5, 5),
        ("an end past the target", 0, len(TARGET) + 20),
    ):
        _rejects(
            label,
            f'{{"findings":[{{"start":{start},"end":{end},"severity":"major","note":"x"}}]}}',
            esa,
            expect_naive_pass=False,
        )
    _rejects(
        "a boolean smuggled in as an offset",
        '{"findings":[{"start":true,"end":4,"severity":"major","note":"x"}]}',
        esa,
        expect_naive_pass=False,
    )
    _rejects(
        "offsets that disagree with the quoted span",
        '{"findings":[{"span":"Reserve","start":8,"end":16,"severity":"major","note":"x"}]}',
        esa,
        expect_naive_pass=False,
    )
    out = _accepts(
        "well-formed offsets",
        '{"findings":[{"start":0,"end":7,"severity":"minor","note":"x"}]}',
        esa,
    )
    if out[0]["span_text"] != "Reserve":
        _fail(("offsets must resolve to the covered substring", out))
    print(
        "  2d. negative, reversed, empty and out-of-range offsets all reject, a boolean is "
        "not an offset, and offsets that disagree with the quoted span reject OK"
    )


def test_unicode_offsets_are_characters():
    esa = _lenses()["esa"]
    target = "Reserva el vuelo ✈ ahora"
    start = target.index("✈")
    text = (
        f'{{"findings":[{{"start":{start},"end":{start + 1},"severity":"minor","note":"glyph"}}]}}'
    )
    out = ql.parse_findings(text, esa, SOURCE, target)
    if out[0]["span_text"] != "✈":
        _fail(("offsets are PYTHON CHARACTER indices, not bytes", out))
    print("  2e. offsets are character indices, not bytes: a non-ASCII span resolves exactly OK")


def test_omission_is_source_anchored():
    """H3, and both arms of the bidirectional validation."""
    esa = _lenses()["esa"]
    out = _accepts(
        "an omission anchored to the source",
        '{"findings":[{"kind":"omission","source_span":"pay later","severity":"major",'
        '"note":"dropped"}]}',
        esa,
    )
    f = out[0]
    if f["span_start"] is not None or f["span_text"] != "":
        _fail(("an omission keeps NULL target offsets", f))
    if f["source_span_text"] != "pay later" or f["source_start"] != SOURCE.index("pay later"):
        _fail(("an omission carries SOURCE offsets", f))
    _rejects(
        "an omission carrying ONLY a target span",
        '{"findings":[{"kind":"omission","span":"Reserve","severity":"major","note":"x"}]}',
        esa,
        expect_naive_pass=False,
    )
    _rejects(
        "a span finding carrying ONLY a source span",
        '{"findings":[{"kind":"span","source_span":"pay later","severity":"major","note":"x"}]}',
        esa,
        expect_naive_pass=False,
    )
    _rejects(
        "an omission whose source span does not occur",
        '{"findings":[{"kind":"omission","source_span":"refund the fare","severity":"major",'
        '"note":"x"}]}',
        esa,
        expect_naive_pass=False,
    )
    print(
        "  2f. an omission is SOURCE-anchored with NULL target offsets; a target-only "
        "omission and a source-only span finding each reject (H3) OK"
    )


def test_omission_span_fallback():
    """H2 (item 15): the SHIPPED judge (gemma) reports an omission by putting the omitted SOURCE
    substring in the `span` field, not `source_span`. The parser accepts that as the SOURCE anchor
    -- but ONLY when the substring is in the SOURCE and NOT in the TARGET, so a target-anchored
    string can never be laundered into a source anchor (H1/H2 fail-closed intact). The primary fix
    is the prompt contract (it now names source_span); this is the fallback for a judge that
    ignores it, which is the live behavior the mock doubles hid."""
    esa = _lenses()["esa"]
    # ACCEPT: "pay later" is in the SOURCE and not in the TARGET -> a legitimate source anchor.
    out = _accepts(
        "a live-shape omission with the source substring under `span`",
        '{"findings":[{"kind":"omission","span":"pay later","severity":"critical","note":"x"}]}',
        esa,
    )
    f = out[0]
    if f["source_span_text"] != "pay later" or f["source_start"] != SOURCE.index("pay later"):
        _fail(("the `span` must resolve as the SOURCE anchor", f))
    if f["span_text"] != "" or f["span_start"] is not None:
        _fail(("an omission keeps NULL TARGET offsets even via the fallback", f))
    # REJECT (the "not in the target" conjunct, on a custom pair where the string is in BOTH):
    # "{0}" occurs in the source AND the target, so it is ambiguous and must NOT launder.
    try:
        ql.parse_findings(
            '{"findings":[{"kind":"omission","span":"{0}","severity":"major","note":"x"}]}',
            esa,
            "Save {0} before closing",  # source has {0}
            "Guarda {0} antes",  # target ALSO has {0}
        )
    except ql.LensVerdictUnreadable:
        pass
    else:
        _fail("an omission whose `span` occurs in the TARGET too must reject (no laundering)")

    # and the PRIMARY fix: the rendered judge contract now advertises source_span.
    class _U:
        source_locale, target_locale = "en", "es"
        source_text, target_text = SOURCE, TARGET

    prompt = ql.build_judge_prompt(esa, _U())
    if "source_span" not in prompt:
        _fail("the judge JSON contract must advertise source_span (the primary H2 fix)")
    print(
        "  2f-bis. an omission's SOURCE substring under `span` resolves as the source anchor "
        "iff it is in the source and NOT the target; the contract now names source_span (H2) OK"
    )


def test_omission_span_ignored_when_source_span_ok():
    """R2-5 (T3 gate round 2), pinned DELIBERATE relaxation: an omission is anchored in the SOURCE
    only. When a valid `source_span` resolves, the parser does NOT target-locate `span` at all -- a
    stray or garbled `span` alongside a good `source_span` is IGNORED, never a whole-verdict reject.
    Required by the dual-field contract (§2.3 advertises both fields): a judge that honestly fills
    `source_span` AND also fills `span` must not have its verdict rejected for the extra field. Not
    a laundering hole: the ACCEPTED anchor is always the source one and `span_text` stays ''."""
    esa = _lenses()["esa"]
    # (a) valid source_span + a garbage `span` that is in NEITHER source nor target -> ACCEPT.
    out = _accepts(
        "an omission with a good source_span and a garbage stray span",
        '{"findings":[{"kind":"omission","source_span":"pay later","span":"ZZZ-not-anywhere",'
        '"severity":"major","note":"x"}]}',
        esa,
    )
    f = out[0]
    if f["source_span_text"] != "pay later" or f["span_text"] != "" or f["span_start"] is not None:
        _fail(("the source anchor is used and the target stays NULL", f))
    # (b) valid source_span + a `span` that IS in the target ("vuelo") -> still ACCEPT (span
    #     ignored). Under the SPAN branch that string would target-locate; on an omission it is
    #     never consulted, which is the whole point of the source-only anchor rule.
    out2 = _accepts(
        "an omission with a good source_span and a span present in the target",
        '{"findings":[{"kind":"omission","source_span":"pay later","span":"vuelo",'
        '"severity":"major","note":"x"}]}',
        esa,
    )
    if out2[0]["source_span_text"] != "pay later" or out2[0]["span_text"] != "":
        _fail(("a target-present stray span on an omission is ignored, not laundered", out2[0]))
    print(
        "  2f-ter. an omission with a valid source_span IGNORES a stray/garbled `span` (even one "
        "present in the target) rather than rejecting the verdict; source anchor used (R2-5) OK"
    )


def test_severity_vocabulary_and_write_back():
    esa = _lenses()["esa"]
    _rejects(
        "an off-vocabulary severity",
        '{"findings":[{"span":"Reserve","severity":"moderate","note":"x"}]}',
        esa,
    )
    _rejects(
        "a missing severity",
        '{"findings":[{"span":"Reserve","note":"x"}]}',
        esa,
    )
    out = _accepts(
        "an uncanonical severity",
        '{"findings":[{"span":"Reserve","severity":" CRITICAL ","note":"x"}]}',
        esa,
    )
    if out[0]["severity"] != "critical":
        _fail(("the canonical severity must be written BACK onto the finding", out))
    # COUNTERFACTUAL: without the write-back, every reader that does NOT route through
    # `effective_severity` sees the raw string -- the persisted QualityFinding.severity, the
    # transcript payload, and T2's gold comparison. A direct weight lookup then reads it as a
    # penalty-1 minor that is neither major nor critical.
    if esa.weights.get(" CRITICAL ", 1) != 1 or " CRITICAL " in esa.severities:
        _fail("the uncanonical-severity counterfactual did not reproduce")
    if ql.rank(esa, out[0]["severity"]) != 10:
        _fail("the written-back severity must rank as critical")
    print(
        "  2g. an off-vocabulary or missing severity rejects; an uncanonical one is "
        "normalized and WRITTEN BACK onto the finding, so no downstream reader (the "
        "persisted row, the payload, a gold comparison) sees ' CRITICAL ' and weighs it 1 OK"
    )


def test_category_rule_per_lens():
    lenses = _lenses()
    mqm, esa = lenses["mqm"], lenses["esa"]
    out = _accepts(
        "a category inside the taxonomy",
        '{"findings":[{"span":"Reserve","severity":"minor","category":"accuracy","note":"x"}]}',
        mqm,
    )
    if out[0]["category"] != "Accuracy":
        _fail(("the canonical dimension spelling must be written back", out))
    _rejects(
        "a category outside the taxonomy",
        '{"findings":[{"span":"Reserve","severity":"minor","category":"Vibes","note":"x"}]}',
        mqm,
        expect_naive_pass=False,
    )
    stripped = _accepts(
        "a chatty category under a no-taxonomy lens",
        '{"findings":[{"span":"Reserve","severity":"minor","category":"Vibes","note":"x"}]}',
        esa,
    )
    if stripped[0]["category"] is not None:
        _fail("a no-taxonomy lens STRIPS a category rather than failing the run")
    print(
        "  2h. with a taxonomy an off-list category rejects (and a valid one is canonicalized); "
        "with no taxonomy it is stripped, never rejected OK"
    )


def test_per_segment_cap():
    esa = _lenses()["esa"]
    many = json.dumps(
        {
            "findings": [
                {"start": i, "end": i + 1, "severity": "minor", "note": "n"}
                for i in range(len(TARGET))
            ][:500]
        }
    )
    _rejects("a runaway finding list", many, esa, expect_naive_pass=False)
    # COUNTERFACTUAL: truncating to the cap silently IMPROVES the score.
    truncated = _naive(many, esa)
    if len(truncated) != esa.max_findings:
        _fail("the truncation counterfactual did not reproduce")
    full = ql.score_findings(esa, [{"severity": "minor", "origin": "judge"}] * len(TARGET))[0]
    capped = ql.score_findings(esa, truncated)[0]
    if not capped > full:
        _fail(("silent truncation must be shown to raise the score", capped, full))
    exact = _accepts(
        "exactly max_findings findings",
        json.dumps(
            {
                "findings": [
                    {"start": i, "end": i + 1, "severity": "minor", "note": "n"}
                    for i in range(esa.max_findings)
                ]
            }
        ),
        esa,
    )
    if len(exact) != esa.max_findings:
        _fail("the cap is inclusive")
    print(
        "  2i. more than max_findings rejects the WHOLE verdict; truncating instead would "
        f"raise the score from {full} to {capped} (P2-D10) OK"
    )


def test_provenance_strip():
    """A judge must NEVER author its own provenance. The parser rebuilds every finding out of
    the contract's own fields, so the forged keys cannot survive at all."""
    esa = _lenses()["esa"]
    forged = json.dumps(
        {
            "findings": [
                {
                    "span": "Reserve",
                    "severity": "major",
                    "note": "x",
                    "origin": "human",
                    "engine_ref": "trusted-judge",
                    "adjudication_outcome": "withheld",
                    "post_edit_fate": "fixed",
                    "adjudicated": {"rule": "glossary-approved-term"},
                    "blocking": False,
                    "precision": "exact",
                }
            ]
        }
    )
    out = ql.parse_findings(forged, esa, SOURCE, TARGET)
    f = out[0]
    if f["origin"] != "judge" or f["adjudication_outcome"] is not None:
        _fail(("judge-authored provenance survived the parse", f))
    for key in ("adjudicated", "post_edit_fate"):
        if key in f and f.get(key):
            _fail(f"{key} survived the parse")
    if f["engine_ref"] is not None if "engine_ref" in f else False:
        _fail("engine_ref survived the parse")
    value, passed, _ids = ql.score_findings(esa, out)
    if passed or value != 95:
        _fail(("a forged withheld marker must not silence the finding", value, passed))
    # COUNTERFACTUAL: the same verdict through the weakened parser, which copies the judge's
    # own keys, scores 100 and PASSES -- the judge silenced its own major finding.
    if not _naive_passes(forged, esa):
        _fail("the provenance counterfactual did not reproduce")
    print(
        "  2j. judge-authored origin/adjudication_outcome/post_edit_fate are unrepresentable "
        "after the parse; copying them instead lets a judge silence its own major finding OK"
    )


def test_mixed_list_keeps_its_dict_findings():
    # NAMED RECORD (L7): this IS a partial accept of the list -- the non-dict members are
    # dropped rather than failing the verdict. It is lqa's own status quo for the same shape
    # and the safe direction (a dropped non-dict carries no severity, so nothing is silently
    # downgraded); the all-non-dict case, which WOULD be a false clean pass, fails closed.
    esa = _lenses()["esa"]
    out = _accepts(
        "a mixed list",
        '{"findings":["some prose",{"span":"Reserve","severity":"minor","note":"x"}]}',
        esa,
    )
    if len(out) != 1:
        _fail(("a list with at least one dict finding lands it", out))
    print(
        "  2k. a MIXED list (interleaved junk plus a real finding) lands the finding; the "
        "partial accept is a named record, not an oversight (L7) OK"
    )


def main():
    db.init_db()
    test_zero_finding_shapes_are_legitimate()
    test_top_level_array_is_normalized()
    test_all_non_dict_array_rejects()
    test_non_verdict_json_values_reject()
    test_fast_path_stray_dict_rejects()
    test_prose_and_fence_wrappers_are_tolerated()
    test_truncated_verdict_rejects()
    test_template_echo()
    test_unknown_kind_rejects()
    test_span_must_occur()
    test_repeated_span_needs_qualification()
    test_occurrence_is_non_overlapping_and_range_checked()
    test_offset_bounds()
    test_unicode_offsets_are_characters()
    test_omission_is_source_anchored()
    test_omission_span_fallback()
    test_omission_span_ignored_when_source_span_ok()
    test_severity_vocabulary_and_write_back()
    test_category_rule_per_lens()
    test_per_segment_cap()
    test_provenance_strip()
    test_mixed_list_keeps_its_dict_findings()
    print("QUALITY LENS PARSER OK")


if __name__ == "__main__":
    main()
