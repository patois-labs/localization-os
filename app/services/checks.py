"""Deterministic quality checks, split out of the judge (SPEC-quality-lenses-p2 section 4).

The P2 split: the LLM judge finds the things only a reader can find (mistranslation, register,
omission), and the things a program can decide with precision 1.0 are decided by a PROGRAM,
here, and never asked of a model. Every check WRAPS an existing seam rather than
reimplementing it, so a check and the write-seam gate it mirrors can never disagree about what
"the term is present" or "over the limit" means.

`run(lens, unit)` returns findings in the SAME substrate shape the judge parser produces
(`quality_lens.parse_findings`), so one writer lands them and the score math sees one list.

WHAT BLOCKS, AND WHY (the ratified rows of section 10):
  * an EMPTY target                              -- BLOCKS, and runs for EVERY lens (T1 gate
    M3 + L2-R2, orchestrator call 2026-09-02, RATIFIED by owner 2026-09-02 as section 10 row
    26). The lens is the tier's delivery gate, and without it a unit with nothing in it
    scores a clean 100 and ships. Precision 1.0, and it aligns with the keys empty-delivery
    approval flow, where an empty target is already never delivered silently.
  * an UNTRANSLATED target (target == source)    -- ADVISORY by default, per-lens opt-in
    (gate round 2). Its seam is a bare string equality and it is NOT precision 1.0: see
    _UNTRANSLATED_FALSE_POSITIVES for the five families of CORRECT target it reports.
  * placeholders / tags / a number VALUE change  -- BLOCK. Verified status quo, not new
    behavior: these are MAJOR on the classic path today and a major already fails the gate.
  * a DNT violation                              -- BLOCKS, new as a DELIVERY gate (row 4).
    The DNT check itself is already EXACT by design: a do-not-translate term is defined as
    surviving VERBATIM, so any alteration is a real breach.
  * char_limit overflow                          -- ADVISORY by default, per-lens opt-in
    (row 3). Blocking here ships the key EMPTY, and an unshipped key exports as the English
    source under `empty_as=base`, which is the laundering shape the SOFT char_limit decision
    was written to avoid. The finding is recorded either way.
  * approved-term ABSENCE                        -- ADVISORY ONLY, on the record (row 5). A
    live prod QA campaign falsified the strict check: roughly half of all glossary flags were
    legitimate INFLECTIONS of the required term on known-correct text.

SEVERITY IS NOT THE GATE, BUT IT CAN BECOME ONE. The score's block clause fails a unit when
ANY finding ranks at or above `lens.block_severity`, so an ADVISORY check that emitted a
`major` would block through the severity clause while reporting `blocking=False` -- inverting
rows 3 and 5. The two advisory checks therefore emit `minor`, and their opt-in blocking (row
3) rides the explicit `blocking` flag, never the severity.

ONE-WAY IMPORT ARROW (M18): this module imports neither `app.services.lqa` nor
`app.services.advanced.*`. The blocking severities below are COPIES of lqa's maps, pinned
equal by `tests/verify_quality_checks.py` so drift is a red test rather than a silent
divergence.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from app.services import qa, quality

# --- check ids (section 4's table) -------------------------------------------
PLACEHOLDERS = "placeholders"
TAGS = "tags"
NUMBERS = "numbers"
GLOSSARY_DNT = "glossary_dnt"
GLOSSARY_ABSENCE = "glossary_absence"
LENGTH = "length"
# T1 gate M3 (orchestrator call 2026-09-02, RATIFIED by owner 2026-09-02 as section 10 row 26),
# added to section 4's table as an addendum. The lens IS the tier's delivery gate, so the two
# states that mean "nothing was translated" have to reach it: without them an EMPTY target and
# a target IDENTICAL to its source both produce zero findings and score a clean 100/PASS (the
# judge is never asked, because there is nothing to find, and every other check abstains on an
# empty string).
#
# THE TWO ARE NOT THE SAME KIND OF CHECK, and round 2 of the gate was right to split them:
#   * EMPTY is precision 1.0 and BLOCKS. "There is nothing here" admits no false positive, and
#     it aligns with the keys empty-delivery approval flow (`keys empties` /
#     `deliver-approve`), where an empty target is already never delivered silently.
#   * UNTRANSLATED is NOT precision 1.0 and is ADVISORY by default with a per-lens opt-in.
#     Its seam is a bare `tgt.strip() == src.strip()` (qa.check_segment), which is TRUE for a
#     whole family of CORRECT targets -- see _UNTRANSLATED_FALSE_POSITIVES. Blocking it by
#     default would withhold a correct key, and a withheld key exports as the English source
#     under `empty_as=base`: the row-3 laundering shape, arrived at from the other direction.
EMPTY = "empty"
UNTRANSLATED = "untranslated"

#: The false-positive class of `untranslated`, named ON THE RECORD the way P2-D2e names the
#: number checks' ambiguity. Every one of these is a CORRECT target that the seam reports as
#: untranslated, because the seam compares strings and cannot see intent:
#:   * a DNT term delivered verbatim -- which `quality.dnt_violations` certifies as correct
#:     under ratified row 4, so the lens would be blocking on the very shape another of its
#:     own checks calls right;
#:   * an acronym or product name that does not localize (PDF, HTTP, Acme, Hotel in es);
#:   * a numeric or code value (404, v2.1, an error code, an ISO country code);
#:   * a placeholder-only value ("{0}", "%s"), which has no translatable text at all;
#:   * a short interjection or borrowed word that is identical in the pair (OK, Email, Menu).
_UNTRANSLATED_FALSE_POSITIVES = (
    "a DNT term delivered verbatim (correct under row 4)",
    "an acronym or product name that does not localize",
    "a numeric or code value",
    "a placeholder-only value",
    "a short interjection or borrowed word identical in the pair",
)
CHECK_IDS = (
    EMPTY,
    UNTRANSLATED,
    PLACEHOLDERS,
    TAGS,
    NUMBERS,
    GLOSSARY_DNT,
    GLOSSARY_ABSENCE,
    LENGTH,
)

# --- precision classes, recorded on every finding (P2-D2e, row 5) ------------
# Named on the record rather than implied by the severity, and carried on the RETURNED
# finding: the value object and the transcript payload render it, the QualityFinding row does
# not (section 2.3's column list is explicit and P2 does not add to it), so a durable findings
# query re-derives it from `check_id` through CHECK_PRECISION below rather than reading it off
# the row.
PRECISION_EXACT = "exact"  # a program decides it; a flag is a real breach
PRECISION_AMBIGUOUS = "ambiguous"  # currency/unit renderings, 12h vs 24h time forms
PRECISION_HEURISTIC = "heuristic"  # morphology-tolerant matching, known inflection misses

# Checks that block by DEFAULT (rows 2 and 4, plus M3's `empty`). `numbers` blocks on the
# `value` kind ONLY.
DEFAULT_BLOCKING = frozenset({EMPTY, PLACEHOLDERS, TAGS, NUMBERS, GLOSSARY_DNT})
# Checks a lens may OPT INTO blocking: `length` (row 3) and `untranslated` (M3 as amended by
# gate round 2). Both are advisory by default because both have a real false-positive class,
# and both are opted in the same way, through `lens.blocking_checks`. Approved-term absence is
# deliberately NOT here: row 5 makes it advisory only, on the record, so opting it in is
# refused at lens write time rather than silently honored.
OPT_IN_BLOCKING = frozenset({LENGTH, UNTRANSLATED})
# Checks that run for EVERY lens, whatever its `checks` list says (L2-R2). "There is nothing
# to deliver" is not a scoring opinion, so a lens cannot switch it off: the `checklist` and
# `post_edit_effort` builtins seed `checks: []` because their SCORE is their own contract, and
# that must not mean an empty target ships.
ALWAYS_ON = frozenset({EMPTY})
# The number KIND that blocks. `count` and `format` carry the named false-positive class
# (P2-D2e) and stay advisory.
BLOCKING_NUMBER_KIND = "value"

# COPIES of the classic path's severities (lqa._QA_TO_MQM / _QA_KIND_SEVERITY / dnt_flag),
# pinned equal by the suite. The two ADVISORY checks are deliberately `minor` -- see the
# module docstring: an advisory `major` would block through the score's severity clause.
CHECK_SEVERITY = {
    # `empty` is MINOR on the editor grid and MAJOR in the classic MQM map; the MAJOR is the
    # one that matters here, because this is a delivery gate and the map is what the gate has
    # always used (the suite pins it to lqa._QA_TO_MQM).
    EMPTY: "major",
    # ADVISORY, so `minor` -- the same rule the other two advisory checks follow: a `major`
    # would fail the gate through the score's SEVERITY clause while still reporting
    # blocking=False, which is the back door that inverts an advisory policy. A lens that opts
    # `untranslated` in gates it through the explicit `blocking` flag instead, so the opt-in
    # never needs the severity to change. This is the ONE place the copy deliberately departs
    # from lqa._QA_TO_MQM (which maps it to MAJOR), and the suite pins the departure by name.
    UNTRANSLATED: "minor",
    PLACEHOLDERS: "major",
    TAGS: "major",
    NUMBERS: "minor",
    GLOSSARY_DNT: "major",
    GLOSSARY_ABSENCE: "minor",
    LENGTH: "minor",
}
NUMBER_KIND_SEVERITY = {BLOCKING_NUMBER_KIND: "major"}

CHECK_PRECISION = {
    EMPTY: PRECISION_EXACT,
    # NOT exact: the seam is a bare string equality, and _UNTRANSLATED_FALSE_POSITIVES lists
    # five families of CORRECT target it reports. Named on the record, like P2-D2e's numbers.
    UNTRANSLATED: PRECISION_AMBIGUOUS,
    PLACEHOLDERS: PRECISION_EXACT,
    TAGS: PRECISION_EXACT,
    NUMBERS: PRECISION_EXACT,  # per KIND below: only `value` is exact
    GLOSSARY_DNT: PRECISION_EXACT,
    GLOSSARY_ABSENCE: PRECISION_HEURISTIC,
    LENGTH: PRECISION_EXACT,
}


def enabled_checks(lens) -> frozenset:
    """The check ids that RUN for this lens: whatever it lists, plus the always-on set the
    lens cannot switch off (L2-R2)."""
    listed = {str(c) for c in (getattr(lens, "checks", None) or [])}
    return frozenset(listed | ALWAYS_ON)


def blocking_check_ids(lens) -> frozenset:
    """The check ids that BLOCK for this lens: the default-blocking set plus the lens's own
    opt-ins, both narrowed to the checks that actually run (the always-on set included, so a
    lens with `checks: []` still blocks on an empty target). `gate_policy` is NOT read here --
    it decides whether a blocking finding stops delivery (quality_lens.score_findings), not
    whether the check is a blocking KIND."""
    opted = {str(c) for c in (getattr(lens, "blocking_checks", None) or [])} & OPT_IN_BLOCKING
    return frozenset((DEFAULT_BLOCKING | opted) & enabled_checks(lens))


def _finding(check_id, severity, note, *, blocking, precision, span_text="", kind_hint=None):
    """One check finding in the substrate shape (the judge parser's shape).

    Check findings carry NO span offsets: the three span-less classes (an absent DNT term, an
    absent approved term, an over-limit value) have nothing to point AT in the target, and
    inventing offsets for the placeholder/tag classes would be fabricated provenance -- the
    seams return a message, not a range. `span_text` carries the offending token where one
    exists, so a surface can still name it."""
    return {
        "kind": "span",
        "severity": severity,
        "category": None,
        "note": note,
        "span_start": None,
        "span_end": None,
        "span_text": span_text,
        "source_start": None,
        "source_end": None,
        "source_span_text": "",
        "origin": "check",
        "check_id": check_id,
        "check_kind": kind_hint,
        "blocking": bool(blocking),
        "precision": precision,
        "adjudication_outcome": None,
    }


def run(lens, unit) -> list[dict]:
    """Every enabled check for one unit, as substrate findings (section 4).

    `unit` is a `quality_lens.LensUnit`: source/target text, the locale pair, the resolved
    glossary terms and the key's char_limit. The glossary terms are RESOLVED BY THE CALLER
    and read off the unit: section 4's own signature `run(lens, unit)` takes no session, so a
    session-bound `glossary_module.match_terms_merged` resolution cannot live inside it; the
    caller (T3's `quality.judge`) does the resolution and hands the result over.

    Whitespace and terminal punctuation are EXCLUDED, for the reason recorded on the classic
    path: as minors they would sum in and flip a card sitting exactly at the bar.

    A lens with an EMPTY `checks` list still runs the always-on set (L2-R2), so there is no
    early return here: `checklist` and `post_edit_effort` seed `checks: []` because their
    SCORE is their own contract, and that must not mean an empty target ships."""
    enabled = enabled_checks(lens)
    blocking = blocking_check_ids(lens)
    src = getattr(unit, "source_text", "") or ""
    tgt = getattr(unit, "target_text", "") or ""
    out: list[dict] = []

    # ONE call, filtered per check id. `numbers` is re-derived below with BOTH locales, so the
    # flag this call produces (target locale only) is dropped here rather than double-counting
    # the same defect. `empty` short-circuits inside `check_segment`, which returns ONLY that
    # flag for a blank target, so the placeholder/tag arms below correctly find nothing.
    _FLAG_TO_CHECK = {
        "empty": EMPTY,
        "untranslated": UNTRANSLATED,
        "placeholder": PLACEHOLDERS,
        "tags": TAGS,
    }
    if enabled & {EMPTY, UNTRANSLATED, PLACEHOLDERS, TAGS}:
        for flag in qa.check_segment(src, tgt, getattr(unit, "target_locale", None)):
            check_id = _FLAG_TO_CHECK.get(flag.get("type"))
            if check_id is None or check_id not in enabled:
                continue
            out.append(
                _finding(
                    check_id,
                    CHECK_SEVERITY[check_id],
                    str(flag.get("message") or ""),
                    blocking=check_id in blocking,
                    precision=CHECK_PRECISION[check_id],
                )
            )

    if NUMBERS in enabled:
        # BOTH locales (section 4): `number_findings` takes them precisely to resolve the
        # decimal convention, so the caller that knows the pair gets the DECISIVE verdict
        # rather than the conservative one the editor grid gets.
        for flag in qa.number_findings(
            src, tgt, getattr(unit, "target_locale", None), getattr(unit, "source_locale", None)
        ):
            kind = str(flag.get("kind") or "")
            sev = NUMBER_KIND_SEVERITY.get(kind, CHECK_SEVERITY[NUMBERS])
            is_value = kind == BLOCKING_NUMBER_KIND
            out.append(
                _finding(
                    NUMBERS,
                    sev,
                    str(flag.get("message") or ""),
                    blocking=is_value and NUMBERS in blocking,
                    # Only the VALUE kind is exact; count/format carry the currency, unit and
                    # 12h/24h ambiguity classes named on the record (P2-D2e).
                    precision=PRECISION_EXACT if is_value else PRECISION_AMBIGUOUS,
                    kind_hint=kind or None,
                )
            )

    terms = getattr(unit, "glossary_terms", None) or []
    if GLOSSARY_DNT in enabled and terms:
        for term in quality.dnt_violations(src, tgt, terms):
            out.append(
                _finding(
                    GLOSSARY_DNT,
                    CHECK_SEVERITY[GLOSSARY_DNT],
                    f"Do-not-translate term {term!r} did not survive verbatim in the target",
                    blocking=GLOSSARY_DNT in blocking,
                    precision=CHECK_PRECISION[GLOSSARY_DNT],
                    span_text=term,
                )
            )

    if GLOSSARY_ABSENCE in enabled and terms:
        # The MORPHOLOGY-TOLERANT seam, not the exact one: the exact check was falsified in
        # production (inflections of the required term on known-correct text). Advisory, and
        # `minor` so it cannot block through the severity clause.
        for miss in quality.glossary_term_violations(
            src, tgt, terms, target_locale=getattr(unit, "target_locale", None)
        ):
            want = miss.get("target_term") or ""
            out.append(
                _finding(
                    GLOSSARY_ABSENCE,
                    CHECK_SEVERITY[GLOSSARY_ABSENCE],
                    f"Approved term {want!r} (for source term "
                    f"{miss.get('source_term') or ''!r}) is not present in the target",
                    blocking=False,  # advisory ONLY, on the record (row 5)
                    precision=CHECK_PRECISION[GLOSSARY_ABSENCE],
                    span_text=want,
                )
            )

    if LENGTH in enabled:
        reason = quality.char_limit_violation(tgt, getattr(unit, "char_limit", None))
        if reason:
            out.append(
                _finding(
                    LENGTH,
                    CHECK_SEVERITY[LENGTH],
                    "Value is over its character limit (" + reason + ")",
                    blocking=LENGTH in blocking,
                    precision=CHECK_PRECISION[LENGTH],
                )
            )
    return out
