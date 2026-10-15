"""Quality Lenses T1 -- the lens registry, the score, purity, and adjudication.

SPEC-quality-lenses-p2 sections 2, 3.1, 3.3, 3.4, 3.5, 5.4; the T1 exit criteria in
section 8 and the named counterfactuals in section 9. The PARSER has its own suite
(verify_quality_lens_parser.py) and the deterministic checks have theirs
(verify_quality_checks.py).

Every assertion here is COUNTERFACTUAL-CHECKED: the behavior is disabled once (a weakened
variant of the same computation) and the check is shown to go the other way. A test that
passes both before and after defends nothing.

Coverage:
  1. SEED CONSTANTS + THE ONE-WAY ARROW (M18): the copies equal lqa.MQM_DIMENSIONS /
     lqa.PENALTY / lqa.LQA_PASS_THRESHOLD; the module's own source imports neither
     app.services.lqa nor app.services.advanced.*; and a FRESH interpreter that imports
     quality_lens + checks pulls neither into sys.modules (the transitive belt).
  2. BUILTINS: four seeded, idempotent, per ORG (L5), each passing its own write-time gate;
     esa carries no taxonomy and adjudication OFF (row 17).
  3. WRITE-TIME VALIDATION (N-M1): every rule refuses its bad case, plus the counterfactual
     that an uncovered severity weight lets a CRITICAL finding pass the gate.
  4. THE SCORE (section 3.3): byte-identical to lqa._score on a fixed corpus INCLUDING
     adjudicated findings (H5); rank from WEIGHTS, never list index (H4); one
     effective_severity in BOTH the penalty sum and the block clause (H5); gate_policy.
  5. POST-EDIT EFFORT (M5): 100 - round(pct) off postedit.distances, and NOT_APPLICABLE
     with no post-edit leg (never a 0).
  6. CHECKLIST (M6): the pass fraction, the block-severity finding, and the parser contract.
  7. PROMPTS (section 3.1): the threshold is never rendered, and the double asserts on
     prompt CONTENT (the P1 committee lesson).
  8. PURITY (N-H1/N2-H1): judge_unit leaves the QualityFinding and AdvancedStagedTarget row
     COUNTS unchanged, proven by a scoped count -- and the BLANKET no-INSERT assertion is
     shown to stay green while an impure variant writes on its own connection.
  9. record_findings: exactly-one-parent (M15), denormalized ring, stamped provenance.
 10. adjudicable (section 5.4): declines on an omission, honors the kill switch, and a
     misuse-shaped finding is NOT adjudicated under the shipped no-taxonomy lens (P2-D13).

Final line: "QUALITY LENS CORE OK".
"""

import ast
import json
import os
import subprocess
import sys
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qlcore_")

from sqlmodel import Session, select

from app import db
from app.models import (
    AdvancedStagedTarget,
    Document,
    Project,
    QualityFinding,
    QualityLens,
    QualityLensExample,
    QualityLensKind,
    _utcnow,
)
from app.services import lqa, postedit
from app.services import quality_lens as ql


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _session():
    return Session(db.engine)


def _document_id():
    """A REAL parent row: SQLite enforces FKs here (PRAGMA foreign_keys=ON), so a findings
    row needs a document that exists."""
    with _session() as s:
        doc = s.exec(select(Document)).first()
        if doc is not None:
            return doc.id
        project = Project(name="Quality Lens fixture")
        s.add(project)
        s.commit()
        s.refresh(project)
        doc = Document(project_id=project.id, filename="lens.docx")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc.id


def _unit(**kw):
    base = {
        "unit_key": "app.button.book",
        "source_text": "Book the flight now",
        "target_text": "Reserve el vuelo ahora",
        "source_locale": "en-US",
        "target_locale": "es-ES",
    }
    base.update(kw)
    return ql.LensUnit(**base)


def _lens(**kw):
    """A detached (unsaved) lens object, so a validation case never needs a write."""
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
        "checks": [],
        "blocking_checks": [],
        "adjudication": False,
        "normalize_per_words": 0,
        "assertions": [],
    }
    base.update(kw)
    return QualityLens(**base)


def _f(severity, **kw):
    """A finding in the substrate shape."""
    base = {
        "kind": "span",
        "severity": severity,
        "category": None,
        "note": "",
        "span_start": 0,
        "span_end": 7,
        "span_text": "Reserve",
        "source_start": None,
        "source_end": None,
        "source_span_text": "",
        "origin": "judge",
        "check_id": None,
        "blocking": False,
        "precision": None,
        "adjudication_outcome": None,
    }
    base.update(kw)
    return base


# ===========================================================================
# 1. Seed constants + the one-way import arrow (M18)
# ===========================================================================
def test_seed_constants_pinned():
    if ql.SEED_MQM_DIMENSIONS != lqa.MQM_DIMENSIONS:
        _fail(("SEED_MQM_DIMENSIONS drifted from lqa.MQM_DIMENSIONS", ql.SEED_MQM_DIMENSIONS))
    if ql.SEED_WEIGHTS != lqa.PENALTY:
        _fail(("SEED_WEIGHTS drifted from lqa.PENALTY", ql.SEED_WEIGHTS, lqa.PENALTY))
    if ql.SEED_THRESHOLD != lqa.LQA_PASS_THRESHOLD:
        _fail(("SEED_THRESHOLD drifted from lqa.LQA_PASS_THRESHOLD", ql.SEED_THRESHOLD))
    # COUNTERFACTUAL: the pin is not vacuous -- a copy that differs by one weight is caught.
    drifted = dict(ql.SEED_WEIGHTS)
    drifted["major"] = 4
    if drifted == lqa.PENALTY:
        _fail("the seed-constant pin cannot detect drift")
    print("  1a. seed constants are COPIES pinned equal to lqa's MQM table (M18) OK")


def test_one_way_arrow_source():
    src = open(os.path.join(_ROOT, "app", "services", "quality_lens.py"), encoding="utf-8").read()  # noqa: SIM115
    src_checks = open(os.path.join(_ROOT, "app", "services", "checks.py"), encoding="utf-8").read()  # noqa: SIM115
    for name, text in (("quality_lens", src), ("checks", src_checks)):
        for node in ast.walk(ast.parse(text)):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                mods = [mod] + [f"{mod}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                if mod in ("app.services.lqa", "lqa") or mod.startswith("app.services.lqa."):
                    _fail(f"{name} imports lqa: the one-way arrow (M18) is broken")
                if mod.startswith("app.services.advanced"):
                    _fail(f"{name} imports app.services.advanced: check-11 direction reversed")
    print("  1b. neither module's SOURCE imports app.services.lqa or app.services.advanced OK")


def test_one_way_arrow_runtime():
    """The TRANSITIVE belt: a source-only check would miss lqa arriving through a seam."""
    code = textwrap.dedent("""
        import sys
        import app.services.quality_lens, app.services.checks  # noqa: F401
        leaked = [m for m in sys.modules
                  if m == "app.services.lqa" or m.startswith("app.services.advanced")]
        assert not leaked, "quality_lens pulled %r into sys.modules" % leaked
        print("clean")
    """)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        _fail(("one-way arrow broken transitively", proc.stdout, proc.stderr[-1500:]))
    print(
        "  1c. a fresh interpreter importing quality_lens+checks pulls in NEITHER lqa nor "
        "advanced (transitive belt) OK"
    )


# ===========================================================================
# 2. Builtins, seeded per org (L5)
# ===========================================================================
def test_builtins_seeded_per_org():
    with _session() as s:
        first = ql.ensure_builtins(s)
        if sorted(first) != sorted(ql.BUILTIN_SLUGS):
            _fail(("builtin slugs", sorted(first)))
        again = ql.ensure_builtins(s)
        if {k: v.id for k, v in again.items()} != {k: v.id for k, v in first.items()}:
            _fail("ensure_builtins is not idempotent")
        other = ql.ensure_builtins(s, org_id=1)
        if other["esa"].id == first["esa"].id:
            _fail("a second org must get its OWN builtin rows (L5), not a shared one")
        rows = s.exec(select(QualityLens).where(QualityLens.slug == "esa")).all()
        if len(rows) != 2:
            _fail(("expected one esa row per org", len(rows)))
        for lens in first.values():
            ql.validate_lens(lens)  # the seeds pass their own write-time gate
        if first["esa"].dimensions != [] or first["esa"].adjudication:
            _fail("esa must carry NO taxonomy and adjudication OFF (row 17)")
        if first["mqm"].dimensions != ql.SEED_MQM_DIMENSIONS:
            _fail("mqm must carry the copied MQM taxonomy")
        if not first["checklist"].assertions:
            _fail("the seeded checklist lens must carry assertions (M6 refuses an empty one)")
        if ql.get_lens(s, "esa") is None or ql.get_lens(s, "nope") is not None:
            _fail("get_lens resolution")
        first["esa"].archived_at = _utcnow()
        s.add(first["esa"])
        s.commit()
        if ql.get_lens(s, "esa") is not None:
            _fail("an ARCHIVED lens must never resolve (L4): a soft delete must stop gating")
        first["esa"].archived_at = None
        s.add(first["esa"])
        s.commit()
    print(
        "  2a. four builtins seeded PER ORG, idempotent, self-validating; archived lenses "
        "do not resolve OK"
    )


# ===========================================================================
# 3. Write-time validation (N-M1)
# ===========================================================================
def _refuses(label, **kw):
    try:
        ql.validate_lens(_lens(**kw))
    except ql.InvalidLensConfig:
        return
    _fail(f"validate_lens accepted a lens it must refuse: {label}")


def test_write_time_validation():
    ql.validate_lens(_lens())  # the control: a good lens is accepted
    _refuses("weights missing a severity", weights={"minor": 1, "major": 5, "critical": 10})
    _refuses(
        "weights naming a severity outside the vocabulary",
        weights={**ql.SEED_WEIGHTS, "blocker": 20},
    )
    _refuses("block_severity outside the vocabulary", block_severity="blocker")
    # N-M1 by name: a block_severity IN the vocabulary but absent from the weight table. It
    # would rank 1 through weights.get(sev, 1), sit below major's 5, and turn the block clause
    # into "any finding at all blocks" -- a gate that does not enforce the policy it reports.
    _refuses(
        "a block_severity with no weight entry",
        severities=[*ql.SEED_SEVERITIES, "blocker"],
        weights=dict(ql.SEED_WEIGHTS),
        block_severity="blocker",
    )
    _refuses(
        "adjudication on without a 'neutral' weight",
        adjudication=True,
        severities=["minor", "major", "critical"],
        weights={"minor": 1, "major": 5, "critical": 10},
    )
    _refuses("normalize_per_words non-zero (M7, reserved)", normalize_per_words=25)
    _refuses("a checklist lens with no assertions (M6)", kind=QualityLensKind.CHECKLIST)
    _refuses("an unknown lens kind", kind="vibes")
    _refuses("an unknown gate_policy", gate_policy="maybe")
    _refuses("an unknown check id", checks=["placeholders", "vibes"])
    _refuses(
        "opting approved-term ABSENCE into blocking (row 5: advisory only)",
        checks=["glossary_absence"],
        blocking_checks=["glossary_absence"],
    )
    _refuses("blocking_checks naming a check the lens does not run", blocking_checks=["length"])
    _refuses("a non-integer weight", weights={**ql.SEED_WEIGHTS, "minor": "one"})
    _refuses("a threshold outside 0..100", threshold=140)
    _refuses("max_findings outside 1..20", max_findings=0)
    _refuses("max_findings over the ceiling", max_findings=21)
    _refuses(
        "running checks without minor/major in the vocabulary",
        severities=["neutral", "critical"],
        weights={"neutral": 0, "critical": 10},
        checks=["placeholders"],
    )
    # And the one that is accepted: `length` IS the ratified per-lens opt-in (row 3).
    ql.validate_lens(_lens(checks=["length"], blocking_checks=["length"]))
    print(
        "  3a. every write-time rule refuses its bad case; the row-3 length opt-in is accepted OK"
    )


def test_uncovered_weight_counterfactual():
    """N-M1 counterfactual. `rank()` reads weights.get(sev, 1), so a severity in the
    vocabulary with NO weight ranks 1, sits BELOW major's 5, and a CRITICAL finding sails
    through a gate that reports it blocks. The write-time rule is what makes that
    unrepresentable, so the proof is: the lens is refused, AND the refusal is load-bearing."""
    holed = _lens(
        severities=["minor", "major", "critical"],
        weights={"minor": 1, "major": 5},  # `critical` uncovered
        block_severity="major",
    )
    try:
        ql.validate_lens(holed)
    except ql.InvalidLensConfig:
        pass
    else:
        _fail("a lens whose weights do not cover its vocabulary must be refused")
    # COUNTERFACTUAL: skip the write-time gate and watch the hole open.
    value, passed, _ids = ql.score_findings(holed, [_f("critical")])
    if not (value == 99 and passed):
        _fail(("the uncovered-weight hole did not reproduce", value, passed))
    print(
        "  3b. counterfactual: WITHOUT the weights-cover-the-vocabulary rule a CRITICAL "
        "finding scores 99 and PASSES the gate (N-M1) OK"
    )


def test_mixed_case_normalization():
    """#464: a lens authored with mixed-case severities, weight keys, or block_severity must
    be stored in lowercase so that `rank()` and `penalty()` lookups succeed at gate time."""
    with _session() as s:
        lens = ql.create_lens(
            s,
            slug="mixed-case",
            title="Mixed Case",
            kind=QualityLensKind.JUDGE,
            dimensions=[],
            severities=["Neutral", "Minor", "Major", "Critical"],
            weights={"Neutral": 0, "Minor": 1, "Major": 5, "Critical": 10},
            block_severity="Major",
            threshold=90,
            max_findings=5,
            gate_policy="block",
            checks=[],
            blocking_checks=[],
            adjudication=False,
            normalize_per_words=0,
            assertions=[],
        )
        if lens.severities != ["neutral", "minor", "major", "critical"]:
            _fail(("#464 create_lens: severities not lowercased", lens.severities))
        if set(lens.weights.keys()) != {"neutral", "minor", "major", "critical"}:
            _fail(("#464 create_lens: weight keys not lowercased", lens.weights))
        if lens.block_severity != "major":
            _fail(("#464 create_lens: block_severity not lowercased", lens.block_severity))
        if ql.rank(lens, "critical") != 10:
            _fail(
                (
                    "#464 create_lens: rank for critical should be 10, got",
                    ql.rank(lens, "critical"),
                )
            )
    print("  3c. create_lens stores lowercase; rank returns the correct weight OK")

    with _session() as s:
        lens = ql.create_lens(
            s,
            slug="mixed-case-upd",
            title="Mixed Case Upd",
            kind=QualityLensKind.JUDGE,
            dimensions=[],
            severities=["neutral", "minor", "major", "critical"],
            weights={"neutral": 0, "minor": 1, "major": 5, "critical": 10},
            block_severity="major",
            threshold=90,
            max_findings=5,
            gate_policy="block",
            checks=[],
            blocking_checks=[],
            adjudication=False,
            normalize_per_words=0,
            assertions=[],
        )
        updated = ql.update_lens(
            s,
            lens.id,
            severities=["Neutral", "Minor", "Major", "Critical"],
            weights={"Neutral": 0, "Minor": 1, "Major": 5, "Critical": 10},
            block_severity="Major",
        )
        if updated.severities != ["neutral", "minor", "major", "critical"]:
            _fail(("#464 update_lens: severities not lowercased", updated.severities))
        if set(updated.weights.keys()) != {"neutral", "minor", "major", "critical"}:
            _fail(("#464 update_lens: weight keys not lowercased", updated.weights))
        if updated.block_severity != "major":
            _fail(("#464 update_lens: block_severity not lowercased", updated.block_severity))
        if ql.rank(updated, "critical") != 10:
            _fail(
                (
                    "#464 update_lens: rank for critical should be 10, got",
                    ql.rank(updated, "critical"),
                )
            )
    print("  3d. update_lens stores lowercase; rank returns the correct weight OK")

    _refuses(
        "weight keys that collide after lowercasing ('Minor' and 'minor')",
        severities=["minor", "major", "critical"],
        weights={"Minor": 1, "minor": 1, "major": 5, "critical": 10},
        block_severity="major",
    )
    print("  3e. weight keys 'Minor' and 'minor' are refused (lowercase collision) OK")

    # 3f (gate): a lens stored with mixed-case weight keys BEFORE the write-path fix is never
    # re-saved by an upgrade, so the READ side must canonicalize too: rank() and penalty()
    # look a lowercased severity up in the stored keys.
    from types import SimpleNamespace

    legacy = SimpleNamespace(weights={"Neutral": 0, "Minor": 1, "Major": 5, "Critical": 10})
    if ql.rank(legacy, "critical") != 10 or ql.rank(legacy, "Major") != 5:
        _fail(("#464 rank must read a legacy mixed-case weight table", legacy.weights))
    got = ql.penalty(legacy, [_f("critical"), _f("major"), _f("neutral")])
    if got != 15:
        _fail(("#464 penalty must read a legacy mixed-case weight table; got", got))
    print("  3f. rank/penalty read a legacy mixed-case weight table correctly OK")


# ===========================================================================
# 4. The score (section 3.3)
# ===========================================================================
_PARITY_CORPUS = [
    [],
    [_f("minor")],
    [_f("major")],
    [_f("critical")],
    [_f("minor")] * 11,
    [_f("neutral")],
    [_f("minor"), _f("major"), _f("critical")],
    # THE SHAPE THAT MATTERS (H5): adjudicated findings. The classic path REWRITES the
    # severity to neutral on the finding; P2 keeps the raw severity and downgrades through
    # effective_severity, so this is the only shape where the two can diverge.
    [_f("major", adjudication_outcome="withheld")],
    [_f("major", adjudication_outcome="withheld"), _f("minor")],
    [_f("critical", adjudication_outcome="withheld"), _f("major")],
    [_f("critical", adjudication_outcome="upheld")],
]


def _as_lqa_errors(findings):
    """The same corpus in the classic shape: lqa_safety.adjudicate rewrites a withheld
    finding's severity to 'neutral' and stamps a marker.

    NAMED RECORD (L8): this translation ENCODES the withheld -> neutral mapping, so the parity
    test proves the two implementations agree GIVEN that mapping rather than deriving it. The
    mapping itself is `effective_severity`'s own contract (H5) and is pinned separately by
    test_effective_severity_in_both_clauses, which shows the score moving when the block clause
    reads the raw severity instead."""
    out = []
    for f in findings:
        e = {"dimension": "Accuracy", "severity": f["severity"], "note": ""}
        if f.get("adjudication_outcome") == "withheld":
            e["severity"] = "neutral"
            e["adjudicated"] = {"rule": "glossary-approved-term"}
        out.append(e)
    return out


def test_mqm_parity_with_lqa_score():
    with _session() as s:
        mqm = ql.ensure_builtins(s)["mqm"]
        for corpus in _PARITY_CORPUS:
            mine = ql.score_findings(mqm, corpus)[:2]
            theirs = lqa._score(_as_lqa_errors(corpus))
            if mine != theirs:
                _fail(("mqm parity broke", corpus, mine, theirs))
    print(
        f"  4a. mqm reproduces lqa._score EXACTLY on {len(_PARITY_CORPUS)} corpora, "
        "adjudicated findings included (H5) OK"
    )


def test_rank_comes_from_weights_not_index():
    """H4. A lens whose `severities` list is ordered with neutral LAST would, under index
    ranking, put neutral ABOVE major and block on an adjudicated finding."""
    lens = _lens(severities=["critical", "major", "minor", "neutral"])
    if not (ql.rank(lens, "neutral") == 0 < ql.rank(lens, "major") == 5):
        _fail("rank must come from the weights")
    withheld = [_f("major", adjudication_outcome="withheld")]
    _v, passed, _ids = ql.score_findings(lens, withheld)
    if not passed:
        _fail("a withheld finding must not block under weight ranking")
    # COUNTERFACTUAL: index ranking on the same list.
    order = lens.severities

    def index_rank(sev):
        return order.index(sev) if sev in order else 0

    if not (index_rank("neutral") >= index_rank("major")):
        _fail("the index-ranking counterfactual did not reproduce")
    print(
        "  4b. rank derives from WEIGHTS, never list index; under index ranking the same "
        "reordered vocabulary would rank neutral ABOVE major (H4) OK"
    )


def test_effective_severity_in_both_clauses():
    """H5. Using the raw severity in the block clause and the adjudicated value in the
    penalty sum is how an adjudicated finding scores as harmless while still failing the
    gate."""
    lens = _lens()
    findings = [_f("major", adjudication_outcome="withheld")]
    value, passed, _ids = ql.score_findings(lens, findings)
    if not (value == 100 and passed):
        _fail(("a withheld major must score 100 and pass", value, passed))
    # COUNTERFACTUAL: the block clause reading the RAW severity.
    raw_blocks = any(
        ql.rank(lens, f["severity"]) >= ql.rank(lens, lens.block_severity) for f in findings
    )
    if not raw_blocks:
        _fail("the raw-severity counterfactual did not reproduce")
    print(
        "  4c. one effective_severity in BOTH clauses: a withheld major scores 100 and "
        "passes, while a raw-severity block clause would still fail it (H5) OK"
    )


def test_gate_policy_and_blocking_checks():
    lens = _lens(checks=["placeholders"])
    blocking = _f("major", origin="check", check_id="placeholders", blocking=True)
    value, passed, ids = ql.score_findings(lens, [blocking])
    if passed or ids != ["placeholders"]:
        _fail(("a blocking check finding must hold the unit", value, passed, ids))
    flagged = _lens(checks=["placeholders"], gate_policy="flag")
    value2, passed2, ids2 = ql.score_findings(flagged, [blocking])
    if not passed2 or ids2 or value2 != 95:
        _fail(("gate_policy=flag must record the finding and not fail the gate", value2, passed2))
    # A JUDGE major still fails the gate under `flag`: the policy governs the deterministic
    # checks' half of the gate (N-M9), not the judge's.
    if ql.score_findings(flagged, [_f("major")])[1]:
        _fail("gate_policy=flag must not relax the judge's own block clause")
    print(
        "  4d. a blocking check holds the unit; gate_policy=flag records it, keeps the "
        "penalty and does not gate, while the judge's block clause still applies OK"
    )


# ===========================================================================
# 5. Post-edit effort (M5)
# ===========================================================================
def test_flag_policy_does_not_fail_the_gate():
    """M1. `flag` has to leave the check findings out of ALL THREE pass clauses. Excluding
    them from the blocking flag alone leaves the severity clause failing the unit; excluding
    them from both still leaves three blocking check findings summing 15 penalty points and
    failing a bar of 90, which is a `flag` policy that blocks."""
    findings = [
        _f("major", origin="check", check_id="placeholders", blocking=True),
        _f("major", origin="check", check_id="tags", blocking=True),
        _f("major", origin="check", check_id="glossary_dnt", blocking=True),
    ]
    blocking_lens = _lens(checks=["placeholders", "tags", "glossary_dnt"])
    value, passed, ids = ql.score_findings(blocking_lens, findings)
    if passed or value != 85 or len(ids) != 3:
        _fail(("under `block` three blocking checks hold the unit", value, passed, ids))
    flag_lens = _lens(checks=["placeholders", "tags", "glossary_dnt"], gate_policy="flag")
    value2, passed2, ids2 = ql.score_findings(flag_lens, findings)
    if not passed2:
        _fail(("gate_policy=flag must NOT fail the gate", value2, passed2, ids2))
    if ids2:
        _fail(("nothing blocks under flag", ids2))
    if value2 != 85:
        _fail(
            "the REPORTED score still counts every finding under flag, so the operator sees "
            f"what the checks found; got {value2}"
        )
    # The judge's own half of the gate is untouched by the policy (N-M9).
    if ql.score_findings(flag_lens, [_f("major")])[1]:
        _fail("gate_policy=flag must not relax the judge's own block clause")
    # COUNTERFACTUAL: keeping the numeric bar over ALL findings (the pre-fix computation)
    # fails the unit at 85 against a bar of 90 even with every other clause excluded.
    if flag_lens.threshold <= value2:
        _fail("the flag counterfactual did not reproduce: the score must sit below the bar")
    print(
        "  4e. gate_policy=flag leaves the check findings out of all three clauses (blocking "
        "flag, severity, numeric bar) while the reported score still counts them; the same "
        "findings under `block` hold the unit OK"
    )


def test_slug_uniqueness_is_enforced_by_the_writer():
    """M2. The model docstring claims writer-enforced uniqueness per (org_id, customer_id);
    a claim no code enforces is a fiction. SQLite cannot express it as a UNIQUE index over
    NULLable ring columns (two NULLs never collide), so `save_lens` is the enforcement."""
    with _session() as s:
        ql.ensure_builtins(s)
        dupe = QualityLens(
            slug="esa",
            title="My ESA",
            kind=QualityLensKind.JUDGE,
            dimensions=[],
            severities=list(ql.SEED_SEVERITIES),
            weights=dict(ql.SEED_WEIGHTS),
            block_severity="major",
            threshold=90,
            max_findings=5,
            gate_policy="block",
            checks=[],
            blocking_checks=[],
            assertions=[],
            org_id=None,
            customer_id=None,
        )
        try:
            ql.save_lens(s, dupe)
        except ql.InvalidLensConfig:
            s.rollback()
        else:
            _fail("save_lens accepted a duplicate slug in the SAME (org_id, customer_id) ring")
        # A DIFFERENT ring is not a duplicate: that is how a builtin forks to a customer copy.
        forked = QualityLens(
            slug="esa",
            title="Acme ESA",
            kind=QualityLensKind.JUDGE,
            dimensions=[],
            severities=list(ql.SEED_SEVERITIES),
            weights=dict(ql.SEED_WEIGHTS),
            block_severity="major",
            threshold=95,
            max_findings=5,
            gate_policy="block",
            checks=[],
            blocking_checks=[],
            assertions=[],
            org_id=None,
            customer_id=42,
        )
        ql.save_lens(s, forked)
        # ...and it WINS the resolution for its own customer, while the org-level row still
        # answers for everyone else. Both answers are deterministic (ordered by id).
        if ql.get_lens(s, "esa", customer_id=42).id != forked.id:
            _fail("a customer fork must win the resolution for that customer")
        if ql.get_lens(s, "esa").customer_id is not None:
            _fail("the org-level row still answers when no customer is named")
        if ql.get_lens(s, "esa", customer_id=99).id != ql.get_lens(s, "esa").id:
            _fail("an unrelated customer falls back to the org-level row")
        # COUNTERFACTUAL: the ring comparison must be NULL-aware. A `WHERE org_id = NULL`
        # matches nothing, so a SQL-side check would have let the duplicate through on exactly
        # the org-level shape this install uses.
        if ql._lens_in_ring(s, "esa", org_id=None, customer_id=None) is None:
            _fail("the NULL-ring lookup found nothing: the uniqueness check would be vacuous")
        s.delete(forked)
        s.commit()
    print(
        "  2b. slug uniqueness is enforced in the single write path, NULL-ring aware; a "
        "different customer ring is a legitimate fork and wins its own resolution "
        "deterministically OK"
    )


def test_data_export_exclusions_resolved_in_t3():
    """L6 ratchet, RESOLVED IN T3 (hand-off (d), SPEC-quality-lenses-p2 §5.6). T1 asserted the
    five tables carried a literal 'T3 revisit' marker so T3 could not silently drop the
    exclusion without a decision. T3 has now MADE the decision: all five stay EXCLUDED (a
    QualityFinding is parented to an excluded advancedrun, so exporting it would dangle; lens
    config and gold are not customer content; the portable quality record stays lqa_result), and
    the 'T3 revisit' marker is REMOVED because the revisit happened. This test now pins the
    resolved state: still classified/excluded, and NO stale 'T3 revisit' marker left behind."""
    from app.services import data_export as dx

    quality_tables = [t for t in dx.EXPLICIT_EXCLUSIONS if t.startswith("quality")]
    if len(quality_tables) != 5:
        _fail(("all five quality tables must stay export-classified (excluded)", quality_tables))
    stale = [t for t in quality_tables if "T3 revisit" in dx.EXPLICIT_EXCLUSIONS[t]]
    if stale:
        _fail(("T3 resolved the markers; no reason may still say 'T3 revisit'", stale))
    print(
        "  2c. all five quality tables stay export-excluded and the 'T3 revisit' marker is "
        "RESOLVED (T3 hand-off (d)) OK"
    )


def test_post_edit_effort():
    with _session() as s:
        lens = ql.ensure_builtins(s)["post_edit_effort"]
    baseline, final = "Reserve el vuelo ahora", "Reserva el vuelo ya"
    _c, _w, pct = postedit.distances(baseline, final)
    want = max(0, 100 - round(pct))
    if ql.post_edit_score(baseline, final) != want:
        _fail("post-edit score must be 100 - round(pct) off postedit.distances")
    if ql.post_edit_score("same text", "same text") != 100:
        _fail("an untouched target is 100")
    ev = ql.judge_unit(lens, _unit(baseline_text=baseline, final_text=final), None)
    if ev.score != want or ev.state != ql.EVAL_OK:
        _fail(("judge_unit post-edit score", ev.score, want))
    # NO post-edit leg -> NOT_APPLICABLE, never a 0 (the Lab's own rule).
    ev2 = ql.judge_unit(lens, _unit(), None)
    if ev2.state != ql.EVAL_NOT_APPLICABLE or ev2.score is not None:
        _fail(("no post-edit leg must be not_applicable with a NULL score", ev2.state, ev2.score))
    # COUNTERFACTUAL: reporting 0 instead would rank an unedited unit as the worst possible
    # work, which is exactly what the state column exists to prevent.
    if ev2.score == 0:
        _fail("not_applicable must not be a 0")
    print(
        "  5a. post-edit effort is 100 - round(pct) on the shared distance primitive, and "
        "no post-edit leg is NOT_APPLICABLE rather than a 0 (M5) OK"
    )


def test_few_shot_examples_are_spans_never_scores():
    """P2-D6 / parent section 6.4: curated SPAN examples are in, scored examples ANCHOR the
    judge on a number and are refused at write time."""
    with _session() as s:
        lens = ql.ensure_builtins(s)["esa"]
        rows = []
        for i in range(6):  # more than the cap, to prove the cap
            row = QualityLensExample(
                lens_id=lens.id,
                source_locale="en-US",
                target_locale="es-ES",
                source_text=f"Cancel booking {i}",
                target_text="Cancelar la reserva",
                span_start=0,
                span_end=8,
                severity="minor",
                note="register",
            )
            s.add(row)
            rows.append(row)
        s.commit()
        picked = ql.examples_for(s, lens, _unit())
        if len(picked) != ql.MAX_EXAMPLES:
            _fail(("at most 4 examples per lens/customer/pair (P2-D6)", len(picked)))
        prompt = ql.build_judge_prompt(lens, _unit(), picked)
        if "Examples:" not in prompt or 'span "Cancelar"' not in prompt:
            _fail(("the few-shot block must render the span", prompt))
        for word in ("score", "rating", "quality"):
            if word in prompt.lower():
                _fail(f"a rendered example must not carry {word!r}")
        # A different locale pair selects nothing, so a judge is never shown examples from
        # another language.
        if ql.examples_for(s, lens, _unit(target_locale="fr-FR")):
            _fail("examples must be selected per locale pair")
        for row in rows:
            s.delete(row)
        s.commit()
    for payload in ({"score": 82}, {"rating": 4}, {"quality": "good"}):
        try:
            ql.validate_example_payload({**payload, "span_start": 0, "span_end": 3})
        except ql.InvalidLensConfig:
            continue
        _fail(f"a scored example must be refused at write time: {payload}")
    ql.validate_example_payload({"span_start": 0, "span_end": 3, "severity": "minor"})
    print(
        "  5b. few-shot examples render as SPANS, are capped at 4 per locale pair, and a "
        "payload carrying a score/rating/quality is refused at write time (P2-D6) OK"
    )


# ===========================================================================
# 6. Checklist (M6)
# ===========================================================================
_CHECKLIST_OK = (
    '{"results":[{"assertion_index":0,"passed":true,"note":"fine"},'
    '{"assertion_index":1,"passed":false,"span":"Reserve","note":"lost a tag"},'
    '{"assertion_index":2,"passed":true,"note":"reads well"}]}'
)


def test_checklist_contract():
    with _session() as s:
        lens = ql.ensure_builtins(s)["checklist"]
    unit = _unit()
    findings, passed_count, total = ql.parse_checklist(
        _CHECKLIST_OK, lens, unit.source_text, unit.target_text
    )
    if (passed_count, total) != (2, 3):
        _fail((passed_count, total))
    if len(findings) != 1 or findings[0]["severity"] != lens.block_severity:
        _fail(("a false assertion becomes ONE finding at block_severity", findings))
    if findings[0]["span_text"] != "Reserve":
        _fail("the supplied span must be located on the target")
    for label, bad in (
        ("a missing assertion", '{"results":[{"assertion_index":0,"passed":true}]}'),
        (
            "a duplicate index",
            '{"results":[{"assertion_index":0,"passed":true},'
            '{"assertion_index":0,"passed":true},{"assertion_index":2,"passed":true}]}',
        ),
        (
            "a non-boolean passed",
            '{"results":[{"assertion_index":0,"passed":"true"},'
            '{"assertion_index":1,"passed":true},{"assertion_index":2,"passed":true}]}',
        ),
        (
            "an out-of-range index",
            '{"results":[{"assertion_index":9,"passed":true},'
            '{"assertion_index":1,"passed":true},{"assertion_index":2,"passed":true}]}',
        ),
    ):
        try:
            ql.parse_checklist(bad, lens, unit.source_text, unit.target_text)
        except ql.LensVerdictUnreadable:
            continue
        _fail(f"the checklist parser accepted {label}")
    # COUNTERFACTUAL: a truthiness read of `passed` would accept the string "true" AND would
    # silently read an empty string as a FAILED assertion.
    if not bool("true") or bool(""):
        _fail("the truthiness counterfactual did not reproduce")
    print(
        "  6a. checklist: one result per assertion, indices complete and unique, `passed` "
        "STRICTLY boolean; a false assertion is one finding at block_severity (M6) OK"
    )


# ===========================================================================
# 7. Prompts (section 3.1) -- the double asserts on CONTENT
# ===========================================================================
def test_prompt_never_carries_the_bar():
    with _session() as s:
        lenses = ql.ensure_builtins(s)
    unit = _unit()
    prompt = ql.build_judge_prompt(lenses["esa"], unit)
    lowered = prompt.lower()
    for word in ("score", "pass", "threshold"):
        if word in lowered:
            _fail(f"the judge prompt must not carry the word {word!r} (anchoring)")
    if str(lenses["esa"].threshold) in prompt:
        _fail("the judge prompt must never render the threshold")
    if "Do not categorize the errors." not in prompt:
        _fail("a no-taxonomy lens must tell the judge not to categorize")
    mqm_prompt = ql.build_judge_prompt(lenses["mqm"], unit)
    if "Categories: Accuracy," not in mqm_prompt:
        _fail("a lens with a taxonomy must render its categories")
    if unit.source_text not in prompt or unit.target_text not in prompt:
        _fail("the prompt must carry the pair")
    print(
        "  7a. the judge prompt carries neither the threshold nor the words score/pass/"
        "threshold, and renders the taxonomy only when the lens has one OK"
    )


def test_double_asserts_on_content():
    """The P1 committee lesson: a double that returns a canned verdict regardless of the
    prompt proves nothing. This one REFUSES an empty target, a prompt missing its few-shot
    block, and a prompt carrying the threshold."""
    with _session() as s:
        lens = ql.ensure_builtins(s)["esa"]
        lens_id = lens.id
    seen = {}

    def double(spec, prompt, **kw):
        if "Translation: \n" in prompt or prompt.rstrip().endswith("Translation:"):
            raise AssertionError("the judge was handed an empty target")
        if str(lens.threshold) in prompt:
            raise AssertionError("the judge prompt carried the threshold")
        if kw.get("task") != "quality_judge":
            raise AssertionError("the ledger correlation task was not passed through")
        seen["prompt"] = prompt
        seen["tap_meta"] = kw.get("tap_meta")
        return {
            "text": '{"findings":[{"span":"Reserve","severity":"minor","note":"register"}]}',
            "error": None,
            "usage": {"input_tokens": 11, "output_tokens": 7},
            "cost_usd": 0.0002,
        }

    real, ql.run_completion = ql.run_completion, double
    try:
        ev = ql.judge_unit(
            lens, _unit(), {"provider": "mock", "model": "m"}, tap_meta={"advanced_run_id": 3}
        )
    finally:
        ql.run_completion = real
    if ev.score != 99 or not ev.passed or ev.lens_id != lens_id:
        _fail(("judge_unit result", ev.score, ev.passed))
    if ev.cost_usd != 0.0002 or ev.tokens_in != 11 or ev.tokens_out != 7:
        _fail(("judge_unit must return the call's real spend", ev.cost_usd, ev.tokens_in))
    if seen["tap_meta"] != {"advanced_run_id": 3}:
        _fail("tap_meta must be passed through to the provider call (N-M6)")
    payload = ev.as_payload()
    if payload["findings"][0]["span"] != "Reserve" or "score" not in payload:
        _fail(("the payload must be self-describing (L6)", payload))
    print(
        "  7b. judge_unit passes task='quality_judge' + tap_meta through, returns the real "
        "cost/tokens, and the double refuses an empty target or a bar-carrying prompt OK"
    )


def test_unreadable_carries_its_spend():
    """N-M2 enabler: a judge that burned tokens and returned garbage must not book $0."""
    with _session() as s:
        lens = ql.ensure_builtins(s)["esa"]

    def double(spec, prompt, **kw):
        return {
            "text": "I could not decide.",
            "error": None,
            "usage": {"input_tokens": 40, "output_tokens": 9},
            "cost_usd": 0.004,
        }

    real, ql.run_completion = ql.run_completion, double
    try:
        ql.judge_unit(lens, _unit(), {"provider": "mock"})
    except ql.LensVerdictUnreadable as exc:
        if (exc.cost_usd, exc.tokens_in, exc.tokens_out) != (0.004, 40, 9):
            _fail(("the unreadable verdict lost its spend", exc.cost_usd, exc.tokens_in))
    else:
        _fail("prose with no JSON must be UNREADABLE, never a clean pass")
    finally:
        ql.run_completion = real
    print(
        "  7c. an unreadable verdict fails CLOSED and carries its real cost and tokens, so "
        "the tool boundary can still book the call (N-M2) OK"
    )


# ===========================================================================
# 8. Purity (N-H1 / N2-H1)
# ===========================================================================
def _counts(session):
    return (
        len(session.exec(select(QualityFinding)).all()),
        len(session.exec(select(AdvancedStagedTarget)).all()),
    )


def test_judge_unit_is_pure_by_row_count():
    with _session() as s:
        lens = ql.ensure_builtins(s)["esa"]
        before = _counts(s)

        def double(spec, prompt, **kw):
            return {
                "text": '{"findings":[{"span":"Reserve","severity":"major","note":"sense"}]}',
                "error": None,
                "usage": {},
                "cost_usd": 0.0,
            }

        real, ql.run_completion = ql.run_completion, double
        try:
            ev = ql.judge_unit(lens, _unit(), {"provider": "mock"}, session=s)
        finally:
            ql.run_completion = real
        after = _counts(s)
        if before != after:
            _fail(("judge_unit wrote a quality-domain row", before, after))
        if len(ev.findings) != 1 or ev.passed:
            _fail(("the evaluation itself must be real", ev.findings, ev.passed))

        # COUNTERFACTUAL, and the reason the proof is a SCOPED ROW COUNT: an impure variant
        # that writes on its OWN connection (exactly how the spend ledger and prompt tap
        # commit) is INVISIBLE to a blanket "this session inserted nothing" assertion.
        doc_id = _document_id()

        def impure(evaluation):
            with Session(db.engine) as own:
                ql.record_findings(own, evaluation, org_id=None, document_id=doc_id)

        s.expunge_all()
        blanket_clean_before = not (s.new or s.dirty)
        impure(ev)
        blanket_clean_after = not (s.new or s.dirty)
        leaked = _counts(s)
        if not (blanket_clean_before and blanket_clean_after):
            _fail("the blanket-assertion counterfactual did not reproduce")
        if leaked[0] == after[0]:
            _fail("the impure variant did not actually insert (the counterfactual is vacuous)")
    print(
        "  8a. judge_unit leaves the QualityFinding + AdvancedStagedTarget row counts "
        "UNCHANGED; the blanket no-INSERT assertion stays green while an own-connection "
        "writer inserts, which is why the proof is a scoped count (N2-H1) OK"
    )


# ===========================================================================
# 9. record_findings: the single writer
# ===========================================================================
def test_record_findings_exactly_one_parent():
    with _session() as s:
        lens = ql.ensure_builtins(s)["esa"]
        ev = ql.LensEvaluation(
            evaluation_id="abc123",
            lens_id=lens.id,
            lens_slug=lens.slug,
            unit_key="app.button.book",
            findings=[
                _f("major", engine_ref="mock", engine_model="m", translator_ref="local"),
                _f(
                    "minor",
                    kind="omission",
                    span_start=None,
                    span_end=None,
                    span_text="",
                    source_start=0,
                    source_end=4,
                    source_span_text="Book",
                ),
            ],
        )
        doc_id = _document_id()
        for label, kw in (
            ("no parent", {}),
            ("two parents", {"document_id": doc_id, "segment_id": 1}),
        ):
            try:
                ql.record_findings(s, ev, **kw)
            except ValueError:
                continue
            _fail(f"record_findings accepted {label}: the M15 rule is not enforced")
        rows = ql.record_findings(s, ev, org_id=None, customer_id=7, document_id=doc_id)
        if len(rows) != 2:
            _fail("both findings must land")
        row = rows[0]
        if row.customer_id != 7 or row.lens_slug != "esa" or row.evaluation_id != "abc123":
            _fail(("the ring and the lens identity must be denormalized onto the row", row))
        if row.engine_ref != "mock" or row.translator_ref != "local":
            _fail("the judge/translator pairing must land on every finding (section 7)")
        omission = rows[1]
        if omission.span_start is not None or omission.source_span_text != "Book":
            _fail("an omission keeps SOURCE offsets and NULL target offsets (H3)")
    print(
        "  9a. record_findings enforces exactly-one-parent (M15), denormalizes the ring and "
        "the lens slug, and lands the pairing on every finding OK"
    )


# ===========================================================================
# 10. Adjudication (section 5.4, P2-D13)
# ===========================================================================
_TERM = {"source_term": "device", "target_term": "dispositivo", "case_sensitive": False}


def _misuse_finding(**kw):
    """A misuse-shaped finding: its span IS the approved term, which is exactly the shape
    lqa_safety rule 6 exists to keep OUT of adjudication."""
    base = {
        "kind": "span",
        "span_text": "dispositivo",
        "severity": "major",
        "category": "Terminology/Approved-term-misuse",
        "adjudication_outcome": None,
    }
    base.update(kw)
    return base


def test_adjudicable_declines_on_omission_and_honors_the_switch():
    with _session() as s:
        mqm = ql.ensure_builtins(s)["mqm"]
    ordinary = {"kind": "span", "span_text": "dispositivo", "category": "Terminology/Wrong term"}
    if not ql.adjudicable(mqm, ordinary, _TERM):
        _fail("a Terminology finding whose span IS the approved term must be adjudicable")
    omission = dict(ordinary, kind="omission", span_text="")
    if ql.adjudicable(mqm, omission, _TERM):
        _fail("adjudicable must DECLINE on an omission (it has no target span)")
    from app.services import lqa_safety

    real, lqa_safety.adjudication_enabled = lqa_safety.adjudication_enabled, lambda: False
    try:
        if ql.adjudicable(mqm, ordinary, _TERM):
            _fail("the install-wide kill switch must be honored")
    finally:
        lqa_safety.adjudication_enabled = real
    print(
        " 10a. adjudicable declines on an omission (never crashes) and honors "
        "lqa_safety.adjudication_enabled() OK"
    )


def test_misuse_not_adjudicated_under_esa():
    """P2-D13's required counterfactual. Rule 6 (an approved-term MISUSE finding is NEVER
    adjudicated) keys on the DIMENSION string; under a no-taxonomy lens the category is NULL,
    so the rule cannot fire. The shipped answer is adjudication OFF for esa."""
    with _session() as s:
        lenses = ql.ensure_builtins(s)
        esa, mqm = lenses["esa"], lenses["mqm"]
    esa_misuse = _misuse_finding(category=None)
    if ql.adjudicable(esa, esa_misuse, _TERM):
        _fail("a misuse-shaped finding must NOT be adjudicated under the shipped esa lens")
    findings = [_misuse_finding(category=None)]
    ql.apply_adjudication(esa, findings, [_TERM])
    if findings[0]["adjudication_outcome"] is not None:
        _fail("apply_adjudication must leave an esa finding untouched")
    # COUNTERFACTUAL: turn adjudication ON for the no-taxonomy lens (the pre-P2-D13 world)
    # and the same misuse-shaped finding IS withheld, silencing the one class rule 6 protects.
    esa.adjudication = True
    reproduced = ql.adjudicable(esa, _misuse_finding(category=None), _TERM)
    esa.adjudication = False
    if not reproduced:
        _fail("the P2-D13 counterfactual did not reproduce")
    # With a taxonomy, rule 6 fires on its own: the misuse category is refused even though
    # mqm has adjudication ON.
    if ql.adjudicable(mqm, _misuse_finding(), _TERM):
        _fail("rule 6 must refuse a misuse finding under a lens WITH a taxonomy")
    print(
        " 10b. a misuse-shaped finding is NOT adjudicated under esa (row 17), while the "
        "same lens with adjudication forced on WOULD withhold it; rule 6 still fires "
        "under mqm's taxonomy OK"
    )


def test_rule_six_survives_the_parser_end_to_end():
    """THE GUARD THAT MATTERS, driven through `parse_findings` rather than hand-built.

    Hand-feeding `category="Terminology/Approved-term-misuse"` to `adjudicable` proves nothing
    if the PARSER can never produce that value. It could not: category canonicalization
    required exact equality against the eight MQM head names, so the only category a misuse
    finding could carry after parsing was a bare `Terminology`, which `_dim_matches` reads
    through its CONTAINS arm -- so on the SHIPPED `mqm` builtin (adjudication ON) a major
    approved-term MISUSE was adjudicable, withheld, and scored 100/PASS. Rule 6 inverted.

    So the fixture starts as JUDGE OUTPUT and goes through the real parse."""
    with _session() as s:
        mqm = ql.ensure_builtins(s)["mqm"]
    target = "Actualice el dispositivo ahora"
    verdict = json.dumps(
        {
            "findings": [
                {
                    "span": "dispositivo",
                    "severity": "major",
                    "category": "Terminology/Approved-term-misuse",
                    "note": "the approved term is wrong in this sense",
                }
            ]
        }
    )
    findings = ql.parse_findings(verdict, mqm, "Update the device now", target)
    if findings[0]["category"] != "Terminology/Approved-term-misuse":
        _fail(("the sub-category must survive the parse or rule 6 cannot fire", findings[0]))
    if ql.adjudicable(mqm, findings[0], _TERM):
        _fail("rule 6: an approved-term MISUSE finding must NEVER be adjudicated")
    ql.apply_adjudication(mqm, findings, [_TERM])
    if findings[0]["adjudication_outcome"] is not None:
        _fail("a misuse finding must not be withheld")
    if findings[0]["severity"] != "major":
        _fail("a misuse finding keeps its major severity")
    value, passed, _ids = ql.score_findings(mqm, findings)
    if passed or value != 95:
        _fail(("a withheld misuse finding would have scored 100/PASS", value, passed))
    # COUNTERFACTUAL: the exact-equality canonicalization this replaced. Under it the only
    # category that survived was a bare head, and rule 6 could not see the misuse marker.
    bare = dict(findings[0], category="Terminology")
    if not ql.adjudicable(mqm, bare, _TERM):
        _fail("the exact-equality counterfactual did not reproduce")
    ql.apply_adjudication(mqm, [bare], [_TERM])
    if ql.score_findings(mqm, [bare])[:2] != (100, True):
        _fail("the inverted-rule-6 counterfactual must score 100/PASS")
    print(
        " 10d. rule 6 end to end THROUGH the parser: the sub-category survives, the misuse "
        "finding is never adjudicated, keeps major and scores 95/FAIL; under the exact-"
        "equality canon it was withheld and scored 100/PASS OK"
    )


def test_sub_dimension_categories_are_accepted():
    """HIGH-1: the shipped judge emits SUB-dimensions (`Accuracy/Omission`, `Design/Markup`,
    `accuracy/mistranslation` -- the classic map's own values). Exact equality against the
    eight head names rejected every one of them, so a live judge behaving exactly like the
    shipped one made every unit under `mqm` unscoreable. No double showed it."""
    with _session() as s:
        lenses = ql.ensure_builtins(s)
        mqm, esa = lenses["mqm"], lenses["esa"]
    # THE PIN: on a known head with no trailing whitespace, canon_category reproduces
    # lqa.canon_dimension exactly.
    corpus = [
        "Accuracy",
        "accuracy",
        "  Accuracy  ",  # a bare head: both sides strip the head, so both agree
        "Accuracy/Omission",
        "accuracy/mistranslation",
        "Design/Markup",
        "Terminology/Approved-term-misuse",
        "locale convention/date format",
        "Style/Register (too casual)",
        "Fluency/",
    ]
    for raw in corpus:
        mine = ql.canon_category(mqm.dimensions, raw)
        theirs = lqa.canon_dimension(raw)
        if mine != theirs:
            _fail(("canon_category drifted from lqa.canon_dimension", raw, mine, theirs))
    # THE SECOND DELIBERATE DIVERGENCE (L1-R2), named rather than claimed as equality. The
    # lens STRIPS the whole value before partitioning; `canon_dimension` strips only the head.
    #
    # Why the lens strips: the stored category is what every downstream reader sees, and rule 6
    # already matches through `lqa_safety._dim_matches`, which collapses whitespace
    # (`" ".join(str(dimension).split())`) before comparing -- so a trailing-space sub-category
    # is ALREADY equivalent for the rule that matters, and keeping the whitespace would only
    # produce two stored categories that differ invisibly and group apart in a report.
    #
    # Why an empty or whitespace-only category is None here and `Other/` there: the classic
    # fold runs at PERSIST time and must bucket whatever it is handed, while for the lens "the
    # judge named no category" is an ABSENT category, not a category called "Other".
    for raw, want in (
        ("Accuracy/Omission  ", "Accuracy/Omission"),
        ("  Accuracy/Omission  ", "Accuracy/Omission"),
        ("Design/Markup\n", "Design/Markup"),
        ("", None),
        ("   ", None),
    ):
        mine = ql.canon_category(mqm.dimensions, raw)
        if mine != want:
            _fail(("the strip divergence must be exactly this", raw, mine, want))
        theirs = lqa.canon_dimension(raw)
        if mine == theirs:
            _fail(
                f"the divergence control changed: canon_dimension now agrees on {raw!r} "
                f"({theirs!r}); if the classic side started stripping, drop this case into the "
                "equality corpus above rather than leaving a divergence that no longer exists"
            )
    # ...and through the real parser, on a lens whose taxonomy is the copied MQM list.
    for raw, want in (
        ("Accuracy/Omission", "Accuracy/Omission"),
        # The HEAD is canonicalized to the taxonomy's capitalization; the SUB is preserved
        # VERBATIM, exactly as lqa.canon_dimension does it (it is model-authored text, and
        # rewriting it would invent a sub-category vocabulary nobody defined).
        ("design/markup", "Design/markup"),
        ("ACCURACY", "Accuracy"),
    ):
        verdict = json.dumps(
            {"findings": [{"span": "Reserve", "severity": "minor", "category": raw, "note": "n"}]}
        )
        out = ql.parse_findings(verdict, mqm, _unit().source_text, _unit().target_text)
        if out[0]["category"] != want:
            _fail(("sub-dimension category must be accepted and head-canonicalized", raw, out))
    # THE NAMED DIVERGENCE from canon_dimension: an unknown HEAD rejects here (a gate cannot
    # honestly score against a taxonomy it does not know) where the classic persist-time fold
    # buckets it under `Other/`.
    unknown = json.dumps(
        {
            "findings": [
                {"span": "Reserve", "severity": "minor", "category": "Vibes/Bad", "note": "n"}
            ]
        }
    )
    try:
        ql.parse_findings(unknown, mqm, _unit().source_text, _unit().target_text)
    except ql.LensVerdictUnreadable:
        pass
    else:
        _fail("an unknown category HEAD must reject the verdict")
    if not lqa.canon_dimension("Vibes/Bad").startswith("Other/"):
        _fail("the divergence control changed: canon_dimension no longer buckets unknown heads")
    # ESA keeps strip-not-reject: a no-taxonomy lens declares the taxonomy expendable.
    esa_out = ql.parse_findings(unknown, esa, _unit().source_text, _unit().target_text)
    if esa_out[0]["category"] is not None:
        _fail("a no-taxonomy lens strips the category")
    # COUNTERFACTUAL: exact equality against the head names rejects every sub-dimension.
    if any(d.lower() == "accuracy/omission" for d in mqm.dimensions):
        _fail("the exact-equality counterfactual did not reproduce")
    print(
        " 10e. sub-dimension categories are accepted with the SUB preserved and the head "
        "canonicalized: pinned equal to lqa.canon_dimension on 10 values, with the strip and "
        "empty-value divergences NAMED on 5 more; an unknown head rejects (the third named "
        "divergence); esa still strips OK"
    )


def test_withheld_finding_keeps_its_raw_severity():
    lens = _lens(adjudication=True)
    findings = [_f("major", span_text="dispositivo")]
    ql.apply_adjudication(lens, findings, [_TERM])
    if findings[0]["adjudication_outcome"] != "withheld":
        _fail("an adjudicable finding must be marked withheld")
    if findings[0]["severity"] != "major":
        _fail(
            "a withheld finding keeps its RAW severity on the record; only the EFFECTIVE "
            "severity is neutral, so the audit trail still says what the judge claimed"
        )
    if ql.effective_severity(findings[0]) != "neutral":
        _fail("effective_severity must downgrade a withheld finding")
    print(
        " 10c. a withheld finding keeps its raw severity for the audit trail and is "
        "downgraded only through effective_severity OK"
    )


def main():
    db.init_db()
    test_seed_constants_pinned()
    test_one_way_arrow_source()
    test_one_way_arrow_runtime()
    test_builtins_seeded_per_org()
    test_slug_uniqueness_is_enforced_by_the_writer()
    test_data_export_exclusions_resolved_in_t3()
    test_write_time_validation()
    test_uncovered_weight_counterfactual()
    test_mixed_case_normalization()
    test_mqm_parity_with_lqa_score()
    test_rank_comes_from_weights_not_index()
    test_effective_severity_in_both_clauses()
    test_gate_policy_and_blocking_checks()
    test_flag_policy_does_not_fail_the_gate()
    test_post_edit_effort()
    test_few_shot_examples_are_spans_never_scores()
    test_checklist_contract()
    test_prompt_never_carries_the_bar()
    test_double_asserts_on_content()
    test_unreadable_carries_its_spend()
    test_judge_unit_is_pure_by_row_count()
    test_record_findings_exactly_one_parent()
    test_adjudicable_declines_on_omission_and_honors_the_switch()
    test_misuse_not_adjudicated_under_esa()
    test_rule_six_survives_the_parser_end_to_end()
    test_sub_dimension_categories_are_accepted()
    test_withheld_finding_keeps_its_raw_severity()
    print("QUALITY LENS CORE OK")


if __name__ == "__main__":
    main()
