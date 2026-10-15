"""Quality Lenses T1 -- the deterministic checks (SPEC-quality-lenses-p2 section 4).

The checks are the half of the old judge's job a PROGRAM can decide. This suite proves each
one wraps its existing seam (never a reimplementation), that the ratified blocking policy of
section 10 is what actually gates, and that an ADVISORY check cannot block by the back door.

Coverage:
  1. The copied severities are PINNED equal to the classic path's maps (the M18 discipline
     applied to checks.py, which may not import lqa either).
  2. Each check fires on a real defect and stays silent on clean text; whitespace and
     terminal punctuation are EXCLUDED, as they are from the classic LQA sum.
  3. `number_findings` is called with BOTH locales (section 4), proven by capturing the call,
     and only the `value` kind blocks (P2-D2e).
  4. The blocking policy: empty / placeholders / tags / number-value / DNT block by default;
     char_limit AND untranslated are advisory with a per-lens opt-in (row 3, MED-1-R2);
     approved-term absence is advisory ONLY (row 5), and the morphology-tolerant seam means an
     INFLECTION is not a flag at all.
  5. The advisory severity rule, counterfactually: an advisory check emitting `major` would
     fail the gate through the score's SEVERITY clause while still reporting blocking=False.
  6. A lens with no checks still runs the always-on `empty` check, and nothing else.

Final line: "QUALITY CHECKS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qchecks_")

from sqlmodel import Session

from app import db
from app.models import QualityLens, QualityLensKind
from app.services import checks, lqa, qa, quality
from app.services import quality_lens as ql

_TERMS = [{"source_term": "device", "target_term": "dispositivo", "case_sensitive": False}]
_DNT = [{"source_term": "Acme", "target_term": "Acme", "dnt": True, "case_sensitive": False}]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _lens(**kw):
    base = {
        "slug": "probe",
        "title": "Probe",
        "kind": QualityLensKind.JUDGE,
        "dimensions": [],
        "severities": list(ql.SEED_SEVERITIES),
        "weights": dict(ql.SEED_WEIGHTS),
        "block_severity": "major",
        "threshold": 90,
        "max_findings": 5,
        "gate_policy": "block",
        "checks": list(checks.CHECK_IDS),
        "blocking_checks": [],
        "adjudication": False,
        "normalize_per_words": 0,
        "assertions": [],
    }
    base.update(kw)
    lens = QualityLens(**base)
    ql.validate_lens(lens)
    return lens


def _unit(**kw):
    base = {
        "source_text": "Book the flight now",
        "target_text": "Reserve el vuelo ahora",
        "source_locale": "en-US",
        "target_locale": "es-ES",
    }
    base.update(kw)
    return ql.LensUnit(**base)


def _by_id(findings):
    return {f["check_id"]: f for f in findings}


# ===========================================================================
# 1. The copied severities are pinned to the classic path
# ===========================================================================
def test_severities_pinned_to_the_classic_path():
    if checks.CHECK_SEVERITY[checks.PLACEHOLDERS] != lqa._QA_TO_MQM["placeholder"][1]:
        _fail("the placeholder severity drifted from the classic map")
    if checks.CHECK_SEVERITY[checks.TAGS] != lqa._QA_TO_MQM["tags"][1]:
        _fail("the tags severity drifted from the classic map")
    if checks.CHECK_SEVERITY[checks.NUMBERS] != lqa._QA_TO_MQM["numbers"][1]:
        _fail("the numbers severity drifted from the classic map")
    if checks.NUMBER_KIND_SEVERITY["value"] != lqa._QA_KIND_SEVERITY[("numbers", "value")]:
        _fail("the number VALUE override drifted from the classic map")
    if checks.CHECK_SEVERITY[checks.GLOSSARY_DNT] != quality.dnt_flag("x")["severity"]:
        _fail("the DNT severity drifted from quality.dnt_flag")
    if checks.CHECK_SEVERITY[checks.EMPTY] != lqa._QA_TO_MQM["empty"][1]:
        _fail("the empty severity drifted from the classic map")
    # The three ADVISORY checks deliberately DIFFER from their classic display severity: a
    # major would block through the score's severity clause (see test 5).
    if checks.CHECK_SEVERITY[checks.GLOSSARY_ABSENCE] != "minor":
        _fail("approved-term absence must be minor: advisory only (row 5)")
    if checks.CHECK_SEVERITY[checks.LENGTH] != "minor":
        _fail("char_limit overflow must be minor: advisory by default (row 3)")
    # NAMED DEPARTURE (MED-1-R2): the classic map calls `untranslated` MAJOR. The lens does
    # not, because the lens is a delivery GATE and this check has a real false-positive class;
    # a major would fail the gate through the severity clause while reporting blocking=False.
    if lqa._QA_TO_MQM["untranslated"][1] != "major":
        _fail("the departure control changed: the classic map no longer calls it major")
    if checks.CHECK_SEVERITY[checks.UNTRANSLATED] != "minor":
        _fail("untranslated must be minor: advisory by default (MED-1-R2)")
    print(
        "  1a. the copied check severities are pinned to the classic maps (empty included), "
        "the three advisory checks are deliberately minor, and untranslated's departure from "
        "the classic MAJOR is named OK"
    )


# ===========================================================================
# 2. Each check fires on its own defect, and only on it
# ===========================================================================
def test_empty_target_blocks():
    """T1 gate M3 (orchestrator call 2026-09-02, PENDING ratification as section 10 row 26).
    The lens IS the tier's delivery gate, so "there is nothing here" has to reach it: before
    this, an EMPTY target produced ZERO findings and scored a clean 100/PASS, because the
    judge has nothing to find and every other check abstains on an empty string."""
    lens = _lens()
    empty = checks.run(lens, _unit(target_text=""))
    flag = _by_id(empty).get(checks.EMPTY)
    if not flag or not flag["blocking"] or flag["severity"] != "major":
        _fail(("an empty target must block", empty))
    if flag["precision"] != checks.PRECISION_EXACT:
        _fail("an empty target is decided by a program: precision 1.0, no false-positive class")
    value, passed, ids = ql.score_findings(lens, empty)
    if passed or checks.EMPTY not in ids:
        _fail(("an empty target must hold the unit", value, passed, ids))
    # COUNTERFACTUAL: the check removed from the always-on set (the only way to switch it off)
    # and the SAME empty target scores a clean 100/PASS.
    real_always, checks.ALWAYS_ON = checks.ALWAYS_ON, frozenset()
    try:
        without = _lens(checks=[c for c in checks.CHECK_IDS if c != checks.EMPTY])
        cf = checks.run(without, _unit(target_text=""))
        if checks.EMPTY in _by_id(cf):
            _fail("the counterfactual lens must not run the empty check")
        if ql.score_findings(without, cf)[:2] != (100, True):
            _fail("the empty-target counterfactual did not reproduce a clean 100/PASS")
    finally:
        checks.ALWAYS_ON = real_always
    # Under `flag` it stays VISIBLE and stops gating, like every other check.
    flagged = _lens(gate_policy="flag")
    seen = checks.run(flagged, _unit(target_text=""))
    if checks.EMPTY not in _by_id(seen):
        _fail("an advisory policy still RECORDS the finding")
    if not ql.score_findings(flagged, seen)[1]:
        _fail("gate_policy=flag must not fail the gate on a check finding")
    print(
        "  2a0. an EMPTY target blocks as a major with precision 1.0; with the check off the "
        "same unit scores a clean 100/PASS; under `flag` it is recorded and does not gate "
        "(M3, row 26) OK"
    )


def test_empty_blocks_under_every_builtin():
    """L2-R2. `checklist` and `post_edit_effort` seed `checks: []` because their SCORE is
    their own contract; that must not mean an empty target ships. "Nothing to deliver" is not
    a scoring opinion a lens may switch off."""
    with Session(db.engine) as s:
        seeded = ql.ensure_builtins(s)
    for slug, lens in sorted(seeded.items()):
        found = checks.run(lens, _unit(target_text=""))
        flag = _by_id(found).get(checks.EMPTY)
        if not flag or not flag["blocking"]:
            _fail(f"the empty check must run and block under the {slug} builtin: {found!r}")
        if checks.EMPTY not in checks.blocking_check_ids(lens):
            _fail(f"{slug} must count `empty` as a blocking check id")
        if ql.score_findings(lens, found)[1]:
            _fail(f"an empty target must hold the unit under {slug}")
    if seeded["checklist"].checks or seeded["post_edit_effort"].checks:
        _fail("the fixture is only meaningful while those two builtins seed an EMPTY list")
    # And through judge_unit's declining arm: a post-edit lens with NO post-edit leg still
    # refuses to deliver an empty target, while its SCORE stays NULL (it declined to score).
    ev = ql.judge_unit(seeded["post_edit_effort"], _unit(target_text=""), None)
    if ev.state != ql.EVAL_NOT_APPLICABLE or ev.score is not None:
        _fail(("the lens still declines to SCORE", ev.state, ev.score))
    if not ev.blocked or checks.EMPTY not in ev.blocking_check_ids:
        _fail(("a declining lens must still hold an empty target", ev.blocked))
    print(
        "  2a0b. an empty target blocks under ALL FOUR builtins, including the two that seed "
        "`checks: []`, and through judge_unit's not_applicable arm (L2-R2) OK"
    )


#: The shapes `qa.check_segment`'s bare `tgt.strip() == src.strip()` reports as untranslated
#: while they are CORRECT targets. Each is a real key shape, not a contrivance.
_CORRECT_BUT_IDENTICAL = (
    ("a DNT brand delivered verbatim", "Acme", "Acme", _DNT),
    ("an acronym", "PDF", "PDF", []),
    ("a numeric key", "404", "404", []),
    ("a short interjection", "OK", "OK", []),
    ("a borrowed noun identical in the pair", "Hotel", "Hotel", []),
)
#: #705 (2026-10-11): a value with NO translatable text (only inline tags / one
#: placeholder) is delivered as its own copy on every path, so `qa.check_segment` no longer
#: reports it at all (it was the sixth row above). Two placeholders keep the engine path and
#: the advisory report, since their order can be the translation.
_TAGS_ONLY_IDENTICAL = (("{0}", "{0}"), ("⟦x1⟧ {count}", "⟦x1⟧ {count}"))


def test_untranslated_is_advisory_with_an_opt_in():
    """MED-1-R2. `untranslated` is NOT precision 1.0 -- its seam is a bare string equality,
    so it fires on a whole family of CORRECT targets. Blocking it by default would withhold a
    correct key, and a withheld key exports as the English SOURCE under `empty_as=base`: the
    row-3 laundering shape, reached from the other direction. The DNT case is the sharpest:
    `quality.dnt_violations` certifies that exact target as CORRECT under ratified row 4, so
    the lens would be blocking on the very shape another of its own checks calls right."""
    lens = _lens()
    for label, src, tgt, terms in _CORRECT_BUT_IDENTICAL:
        found = checks.run(lens, _unit(source_text=src, target_text=tgt, glossary_terms=terms))
        flag = _by_id(found).get(checks.UNTRANSLATED)
        if not flag:
            _fail(f"the seam still REPORTS {label}; the finding is recorded either way")
        if flag["blocking"]:
            _fail(f"{label} must not block by default")
        if flag["severity"] != "minor" or flag["precision"] != checks.PRECISION_AMBIGUOUS:
            _fail((f"{label}: advisory means minor + the named ambiguity class", flag))
        value, passed, ids = ql.score_findings(lens, found)
        if not passed:
            _fail((f"{label} must NOT fail the gate under the default policy", value, ids))
    # #705: the tags-only copy is not reported, even under a lens that opted in to block.
    for src, tgt in _TAGS_ONLY_IDENTICAL:
        for ln in (lens, _lens(blocking_checks=[checks.UNTRANSLATED])):
            found = checks.run(ln, _unit(source_text=src, target_text=tgt))
            if checks.UNTRANSLATED in _by_id(found):
                _fail(f"a tags-only copy is correct, never 'untranslated': {src!r}")
    found = checks.run(lens, _unit(source_text="{first} {last}", target_text="{first} {last}"))
    if checks.UNTRANSLATED not in _by_id(found):
        _fail("two placeholders are a format string: the identical value is still reported")
    # The DNT case, spelled out: the same pair is CLEAN by the DNT check's own verdict.
    if quality.dnt_violations("Acme", "Acme", _DNT):
        _fail("row 4 certifies a verbatim DNT term as correct; the fixture must reflect that")
    # A lens whose content genuinely never repeats the source can OPT IN, like `length`.
    opted = _lens(blocking_checks=[checks.UNTRANSLATED])
    source = "Book the flight now and pay later"
    found = checks.run(opted, _unit(source_text=source, target_text=source))
    if not _by_id(found)[checks.UNTRANSLATED]["blocking"]:
        _fail("the per-lens opt-in must make untranslated block")
    if ql.score_findings(opted, found)[1]:
        _fail("an opted-in untranslated finding must hold the unit")
    # COUNTERFACTUAL: put it back in DEFAULT_BLOCKING and every correct target above fails.
    real_default, checks.DEFAULT_BLOCKING = (
        checks.DEFAULT_BLOCKING,
        checks.DEFAULT_BLOCKING | {checks.UNTRANSLATED},
    )
    try:
        reproduced = []
        for label, src, tgt, terms in _CORRECT_BUT_IDENTICAL:
            cf = checks.run(lens, _unit(source_text=src, target_text=tgt, glossary_terms=terms))
            if not ql.score_findings(lens, cf)[1]:
                reproduced.append(label)
        if len(reproduced) != len(_CORRECT_BUT_IDENTICAL):
            _fail(("the blocking-by-default counterfactual did not reproduce", reproduced))
    finally:
        checks.DEFAULT_BLOCKING = real_default
    if checks.UNTRANSLATED not in checks.OPT_IN_BLOCKING:
        _fail("untranslated must sit beside length in the opt-in set")
    print(
        "  2a1b. `untranslated` is ADVISORY: five CORRECT-but-identical targets (DNT verbatim, "
        "acronym, numeric, OK, Hotel) are all recorded and none fails the gate; a tags-only "
        "copy is not reported at all (#705); a lens can opt in; with it back in "
        "DEFAULT_BLOCKING all five FAIL (MED-1-R2) OK"
    )


def test_check_findings_carry_no_judge_provenance():
    """L4. `engine_*` names the JUDGE. Stamping it onto a deterministic check would persist a
    false provenance claim: a findings query grouped by engine would credit a placeholder
    flag to a model that had no part in it."""
    lens = _lens()

    def double(spec, prompt, **kw):
        return {
            "text": '{"findings":[{"span":"Hola","severity":"minor","note":"register"}]}',
            "error": None,
            "usage": {},
            "cost_usd": 0.0,
        }

    unit = ql.LensUnit(
        source_text="Hello {0}",
        target_text="Hola",
        source_locale="en-US",
        target_locale="es-ES",
        translator_ref="local",
        translator_model="gemma-3",
    )
    real, ql.run_completion = ql.run_completion, double
    try:
        ev = ql.judge_unit(lens, unit, {"provider": "mock", "model": "m"})
    finally:
        ql.run_completion = real
    judged = [f for f in ev.findings if f["origin"] == "judge"]
    checked = [f for f in ev.findings if f["origin"] == "check"]
    if not judged or not checked:
        _fail(("the fixture must produce both a judge and a check finding", ev.findings))
    if judged[0]["engine_ref"] != "mock" or judged[0]["engine_model"] != "m":
        _fail(("a judge finding names the judge", judged[0]))
    for f in checked:
        if f["engine_ref"] is not None or f["engine_model"] is not None:
            _fail(("a check finding must carry NO judge engine (the columns are nullable)", f))
        if f["family_separation"] is not None:
            _fail("separation is a judge-vs-translator comparison; with no judge it is unknown")
        if f["translator_ref"] != "local":
            _fail("the TRANSLATOR pairing IS stamped: that engine really produced the text")
    print(
        "  2a1. a check finding carries a NULL judge engine and NULL separation while keeping "
        "the translator pairing; a judge finding names the judge (L4) OK"
    )


def test_clean_text_produces_nothing():
    out = checks.run(_lens(), _unit(glossary_terms=_TERMS + _DNT, char_limit=100))
    if out:
        _fail(("clean text must produce no findings", out))
    print("  2a. clean text produces no check findings at all OK")


def test_placeholders_and_tags():
    lens = _lens()
    ph = checks.run(lens, _unit(source_text="Hello ⟦r1⟧world", target_text="Hola mundo"))
    if list(_by_id(ph)) != [checks.PLACEHOLDERS]:
        _fail(("a dropped inline-fidelity token is a placeholders finding", _by_id(ph)))
    if not ph[0]["blocking"] or ph[0]["severity"] != "major":
        _fail(("placeholders block by default (row 2)", ph))
    tags = checks.run(lens, _unit(source_text="Hello {0}", target_text="Hola"))
    if list(_by_id(tags)) != [checks.TAGS] or not tags[0]["blocking"]:
        _fail(("a dropped {0} is a blocking tags finding", tags))
    # Disabling the check disables the finding (the enable set is real, not decorative).
    if checks.run(_lens(checks=["numbers"]), _unit(source_text="Hello {0}", target_text="Hola")):
        _fail("a lens that does not run the tags check must produce no tags finding")
    print(
        "  2b. placeholder and tag damage each land as a BLOCKING major, and only when the "
        "lens runs that check OK"
    )


def test_whitespace_and_punctuation_are_excluded():
    """As minors they would sum in and flip a card sitting exactly at the bar -- the reason
    the classic LQA structural sum excludes them too."""
    lens = _lens()
    flags = qa.check_segment("Hello world.", "Hola mundo")
    if not any(f["type"] == "punctuation" for f in flags):
        _fail("fixture: the seam must produce a punctuation flag for this pair")
    out = checks.run(lens, _unit(source_text="Hello world.", target_text="Hola mundo"))
    if out:
        _fail(("terminal punctuation must never reach a lens finding", out))
    out2 = checks.run(lens, _unit(source_text="Hello world ", target_text="Hola mundo"))
    if out2:
        _fail(("whitespace must never reach a lens finding", out2))
    print("  2c. whitespace and terminal punctuation flags are excluded from the lens OK")


def test_numbers_get_both_locales_and_only_value_blocks():
    lens = _lens()
    seen = {}
    real = qa.number_findings

    def spy(source_text, target_text, target_locale=None, source_locale=None):
        seen["locales"] = (source_locale, target_locale)
        return real(source_text, target_text, target_locale, source_locale)

    checks.qa.number_findings = spy
    try:
        out = checks.run(
            lens,
            _unit(source_text="Deliver 25 items", target_text="Entregar 25.000 articulos"),
        )
    finally:
        checks.qa.number_findings = real
    if seen.get("locales") != ("en-US", "es-ES"):
        _fail(("both locales must reach number_findings (section 4)", seen))
    value = _by_id(out).get(checks.NUMBERS)
    if not value or value["severity"] != "major" or not value["blocking"]:
        _fail(("a number VALUE change blocks (row 2)", out))
    if value["precision"] != checks.PRECISION_EXACT:
        _fail("a value change is the exact kind")
    # A count/format finding is ADVISORY and carries the named ambiguity class (P2-D2e).
    counted = checks.run(
        lens, _unit(source_text="Book 2 seats for 3 days", target_text="Reserve 2 asientos")
    )
    flag = _by_id(counted).get(checks.NUMBERS)
    if not flag or flag["blocking"] or flag["severity"] != "minor":
        _fail(("a number COUNT mismatch is advisory", counted))
    if flag["precision"] != checks.PRECISION_AMBIGUOUS:
        _fail("count/format carry the named ambiguity class (P2-D2e)")
    print(
        "  2d. number_findings is called with BOTH locales; only the VALUE kind blocks, "
        "and count/format are advisory with the ambiguity class recorded OK"
    )


def test_dnt_blocks_and_absence_is_advisory():
    lens = _lens()
    dnt = checks.run(
        lens,
        _unit(
            source_text="Acme ships today",
            target_text="Cumbre envia hoy",
            glossary_terms=_DNT,
        ),
    )
    flag = _by_id(dnt).get(checks.GLOSSARY_DNT)
    if not flag or not flag["blocking"] or flag["severity"] != "major":
        _fail(("a DNT breach blocks delivery (row 4)", dnt))
    if flag["span_text"] != "Acme":
        _fail("the offending term must be named on the finding")
    missing = checks.run(
        lens,
        _unit(
            source_text="The device is on",
            target_text="El aparato esta encendido",
            glossary_terms=_TERMS,
        ),
    )
    absent = _by_id(missing).get(checks.GLOSSARY_ABSENCE)
    if not absent or absent["blocking"] or absent["severity"] != "minor":
        _fail(("approved-term absence is ADVISORY ONLY (row 5)", missing))
    if absent["precision"] != checks.PRECISION_HEURISTIC:
        _fail("absence carries the heuristic precision class")
    # THE FALSIFIED STRICT CHECK: an INFLECTION of the required term is not a flag at all,
    # because the check rides the morphology-tolerant seam. A live prod campaign found roughly
    # half of all glossary flags were exactly this shape on known-correct text.
    inflected = checks.run(
        lens,
        _unit(
            source_text="The device is on",
            target_text="El dispositivos esta encendido",
            glossary_terms=_TERMS,
        ),
    )
    if checks.GLOSSARY_ABSENCE in _by_id(inflected):
        _fail(("an inflection of the required term must not be flagged", inflected))
    print(
        "  2e. a DNT breach blocks; approved-term absence is advisory, heuristic, and an "
        "INFLECTION of the required term is not flagged at all OK"
    )


def test_char_limit_is_advisory_with_an_opt_in():
    over = _unit(target_text="Reserve el vuelo ahora", char_limit=10)
    advisory = _by_id(checks.run(_lens(), over)).get(checks.LENGTH)
    if not advisory or advisory["blocking"]:
        _fail(("char_limit overflow is advisory by default (row 3)", advisory))
    if "limit 10" not in advisory["note"]:
        _fail(("the finding must carry both numbers", advisory))
    opted = _lens(blocking_checks=[checks.LENGTH])
    blocking = _by_id(checks.run(opted, over)).get(checks.LENGTH)
    if not blocking or not blocking["blocking"]:
        _fail("the per-lens opt-in must make char_limit block")
    if checks.run(_lens(), _unit(char_limit=None)):
        _fail("an unset limit is not a violation")
    if checks.run(_lens(), _unit(char_limit=0)):
        _fail("a 0 limit reads as UNSET through coerce_char_limit, never as 'must be empty'")
    print(
        "  2f. char_limit overflow is advisory by default and blocks only on the per-lens "
        "opt-in; an unset or 0 limit is not a violation (row 3) OK"
    )


# ===========================================================================
# 3. The gate, and the advisory back door
# ===========================================================================
def test_advisory_severity_cannot_block_by_the_back_door():
    """COUNTERFACTUAL for the severity rule: an advisory check that emitted `major` would
    fail the gate through the score's SEVERITY clause while still reporting blocking=False,
    which inverts rows 3 and 5 without touching the blocking flag."""
    lens = _lens()
    over = _unit(target_text="Reserve el vuelo ahora", char_limit=10)
    findings = checks.run(lens, over)
    _value, passed, ids = ql.score_findings(lens, findings)
    if not passed or ids:
        _fail(("an advisory-only finding must not hold the unit", findings, passed))
    promoted = [dict(f, severity="major") for f in findings]
    _v2, passed2, _ids2 = ql.score_findings(lens, promoted)
    if passed2:
        _fail("the advisory-major counterfactual did not reproduce")
    print(
        "  3a. an advisory finding does not hold the unit, while the same finding emitted "
        "as a major WOULD fail the gate through the severity clause OK"
    )


def test_blocking_ids_are_reported():
    lens = _lens()
    findings = checks.run(
        lens,
        _unit(
            source_text="Hello {0}, Acme ships 25 items",
            target_text="Hola, Cumbre envia 25.000 articulos",
            glossary_terms=_DNT,
        ),
    )
    _value, passed, ids = ql.score_findings(lens, findings)
    if passed:
        _fail("three blocking defects must hold the unit")
    if set(ids) != {checks.TAGS, checks.NUMBERS, checks.GLOSSARY_DNT}:
        _fail(("the manifest must name WHICH checks blocked", ids))
    print(
        "  3b. every blocking check id is reported, so the manifest can say which check "
        "held the unit OK"
    )


def test_no_checks_runs_only_the_always_on_set():
    """The enable set is real, with ONE exception the lens cannot switch off (L2-R2): a lens
    listing no checks still runs `empty`, and nothing else."""
    bare = _lens(checks=[], blocking_checks=[])
    over_limit = checks.run(bare, _unit(char_limit=1))
    if over_limit:
        _fail(("a lens with no checks runs no OPTIONAL check", over_limit))
    if checks.enabled_checks(bare) != checks.ALWAYS_ON:
        _fail(("a lens with no checks runs exactly the always-on set", checks.enabled_checks(bare)))
    if checks.blocking_check_ids(bare) != frozenset({checks.EMPTY}):
        _fail(("...and blocks on exactly that", checks.blocking_check_ids(bare)))
    empty_found = checks.run(bare, _unit(target_text=""))
    if [f["check_id"] for f in empty_found] != [checks.EMPTY]:
        _fail(("an empty target still lands the always-on finding", empty_found))
    with Session(db.engine) as s:
        seeded = ql.ensure_builtins(s)
    if set(seeded["esa"].checks) != set(checks.CHECK_IDS):
        _fail("the seeded judge lenses run every check")
    if seeded["checklist"].checks or seeded["post_edit_effort"].checks:
        _fail("the checklist and post-edit lenses score by their own contract")
    print(
        "  3c. the enable set is real: a lens with no checks runs ONLY the always-on `empty` "
        "and blocks on exactly that; the seeded lenses carry the intended sets OK"
    )


def main():
    db.init_db()
    test_severities_pinned_to_the_classic_path()
    test_empty_target_blocks()
    test_empty_blocks_under_every_builtin()
    test_untranslated_is_advisory_with_an_opt_in()
    test_check_findings_carry_no_judge_provenance()
    test_clean_text_produces_nothing()
    test_placeholders_and_tags()
    test_whitespace_and_punctuation_are_excluded()
    test_numbers_get_both_locales_and_only_value_blocks()
    test_dnt_blocks_and_absence_is_advisory()
    test_char_limit_is_advisory_with_an_opt_in()
    test_advisory_severity_cannot_block_by_the_back_door()
    test_blocking_ids_are_reported()
    test_no_checks_runs_only_the_always_on_set()
    print("QUALITY CHECKS OK")


if __name__ == "__main__":
    main()
