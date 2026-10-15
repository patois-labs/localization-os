"""Quality Lenses: findings in, deterministic score out (SPEC-quality-lenses-p2 section 3).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports SEED_MQM_DIMENSIONS, SEED_WEIGHTS, SEED_THRESHOLD,
SEED_SEVERITIES, SEED_BLOCK_SEVERITY, DEFAULT_MAX_FINDINGS, MAX_FINDINGS_CEILING, MAX_EXAMPLES,
GATE_BLOCK, GATE_FLAG, GATE_POLICIES, EVAL_OK, EVAL_NOT_APPLICABLE, BUILTIN_MQM, BUILTIN_ESA,
BUILTIN_CHECKLIST, BUILTIN_POST_EDIT_EFFORT, BUILTIN_SLUGS, JUDGE_FORBIDDEN_KEYS, _ASSERTION_SEED,
LensVerdictUnreadable, InvalidLensConfig, LensUnit, _read, LensEvaluation, builtin_specs,
_canon_severities, validate_lens, _EXAMPLE_ANCHOR_KEYS, validate_example_payload, _lens_in_ring,
get_lens, DEFAULT_LENS_SETTING, _EDITABLE_LENS_FIELDS, lens_view, get_lens_by_id, _next_fork_slug,
archive_lens, _apply_lens_fields, examples_for, JUDGE_SYSTEM, _JUDGE_JSON_CONTRACT,
_CHECKLIST_JSON_CONTRACT, _example_block, build_judge_prompt, build_checklist_prompt, _PLACEHOLDER,
_is_placeholder_finding, _reject_non_findings, _reject_non_results, _normalize_envelope, _is_int,
_locate, rank, effective_severity, post_edit_score, _spec, engine_family, _separation, _stamp,
adjudicable, canon_category, penalty, provider_spec, apply_adjudication from
app.services.quality_lens_seed_constants_copies_of, app.services.quality_lens_part2.

P2 replaces the LLM judge's job. The judge emits FINDINGS ONLY (a span plus a severity, a
category only when the lens has a taxonomy); the SCORE is deterministic math over those
findings under a named, configurable lens. The AutoMQM result behind the design is that
judges locate errors well and score badly, and that LLM scores clump onto {0, 50, 90, 95},
which puts a default bar of 90 on a mode.

WHAT LIVES HERE (T1, the substrate):
  * the lens registry and its WRITE-TIME validation (section 2.1, N-M1);
  * the judge and checklist prompt builders (section 3.1, 3.4). The THRESHOLD IS NEVER
    RENDERED INTO A PROMPT (parent spec section 6.4, anchoring);
  * the FAIL-CLOSED parser (section 3.2), where every shape check is an UNCONDITIONAL
    post-parse guard on the returned object and every failure rejects the WHOLE verdict;
  * the deterministic score (section 3.3), byte-identical to `lqa._score` for the seeded
    `mqm` lens, INCLUDING adjudicated findings;
  * the PURE `judge_unit` (N-H1) and the separate write-side `record_findings`;
  * `adjudicable`, the findings-shaped sibling of the classic MQM-string adjudicator.

NOT IN T1, AND NAMED SO IT IS NOT MISTAKEN FOR DONE (T3 scope):
  * DELETION. `QualityFinding` carries hard FKs to `advancedrun`, `segment`, `document`,
    `advancedstagedtarget` and `qualitylens`, and SQLite enforces them (PRAGMA foreign_keys=ON),
    but NOTHING deletes a finding today. So the moment T3 writes findings, deleting a run, a
    document or a lens FK-fails on the rows this module wrote, exactly like the
    delete_workflow FK train. T3 owns the deletion path (cascade or sever, per parent) and the
    retention/GDPR sweep that goes with it.
  * The lens has NO surface in T1: no route, no CLI verb, no MCP tool, no kernel-facade entry
    (a `_KERNEL_SURFACE` entry with no shell importing it fails the facade's drift check).

TWO IMPORT RULES, both load-bearing:
  * this module must NOT import `app.services.advanced.*` (the module registry's check-11
    direction: the advanced package imports the services, never the other way);
  * this module must NOT import `app.services.lqa` (M18, the ONE-WAY ARROW). The seed
    constants below are COPIES; `tests/verify_quality_lens_core.py` pins them equal to
    `lqa.MQM_DIMENSIONS` / `lqa.PENALTY` / `lqa.LQA_PASS_THRESHOLD`, so drift is a red test
    rather than a silent divergence. The TEST imports lqa; the module never does. `lqa` may
    later import THIS module (the P2b backport); the arrow never reverses.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field

from sqlmodel import Session, select

from app.models import (
    QualityFinding,
    QualityFindingKind,
    QualityFindingOrigin,
    QualityLens,
    QualityLensExample,
    QualityLensKind,
)
from app.services import checks, llm_json, lqa_safety, postedit
from app.services.providers import (
    DeadEngineError,
    ProviderSpec,
    run_completion,
    usage_token_counts,
)
from app.services.quality_lens_part2 import (
    apply_adjudication,
    canon_category,
    penalty,
    provider_spec,
)
from app.services.quality_lens_seed_constants_copies_of import (
    _ASSERTION_SEED,
    _CHECKLIST_JSON_CONTRACT,
    _EDITABLE_LENS_FIELDS,
    _EXAMPLE_ANCHOR_KEYS,
    _JUDGE_JSON_CONTRACT,
    _PLACEHOLDER,
    BUILTIN_CHECKLIST,
    BUILTIN_ESA,
    BUILTIN_MQM,
    BUILTIN_POST_EDIT_EFFORT,
    BUILTIN_SLUGS,
    DEFAULT_LENS_SETTING,
    DEFAULT_MAX_FINDINGS,
    EVAL_NOT_APPLICABLE,
    EVAL_OK,
    GATE_BLOCK,
    GATE_FLAG,
    GATE_POLICIES,
    JUDGE_FORBIDDEN_KEYS,
    JUDGE_SYSTEM,
    MAX_EXAMPLES,
    MAX_FINDINGS_CEILING,
    SEED_BLOCK_SEVERITY,
    SEED_MQM_DIMENSIONS,
    SEED_SEVERITIES,
    SEED_THRESHOLD,
    SEED_WEIGHTS,
    InvalidLensConfig,
    LensEvaluation,
    LensUnit,
    LensVerdictUnreadable,
    _apply_lens_fields,
    _canon_severities,
    _example_block,
    _is_int,
    _is_placeholder_finding,
    _lens_in_ring,
    _locate,
    _next_fork_slug,
    _normalize_envelope,
    _read,
    _reject_non_findings,
    _reject_non_results,
    _separation,
    _spec,
    _stamp,
    adjudicable,
    archive_lens,
    build_checklist_prompt,
    build_judge_prompt,
    builtin_specs,
    effective_severity,
    engine_family,
    examples_for,
    get_lens,
    get_lens_by_id,
    lens_owned_by_ring,
    lens_view,
    post_edit_score,
    rank,
    validate_example_payload,
    validate_lens,
)

__all__ = [
    "BUILTIN_CHECKLIST",
    "BUILTIN_ESA",
    "BUILTIN_MQM",
    "BUILTIN_POST_EDIT_EFFORT",
    "BUILTIN_SLUGS",
    "DEFAULT_LENS_SETTING",
    "DEFAULT_MAX_FINDINGS",
    "EVAL_NOT_APPLICABLE",
    "EVAL_OK",
    "GATE_BLOCK",
    "GATE_FLAG",
    "GATE_POLICIES",
    "JUDGE_FORBIDDEN_KEYS",
    "JUDGE_SYSTEM",
    "MAX_EXAMPLES",
    "MAX_FINDINGS_CEILING",
    "SEED_BLOCK_SEVERITY",
    "SEED_MQM_DIMENSIONS",
    "SEED_SEVERITIES",
    "SEED_THRESHOLD",
    "SEED_WEIGHTS",
    "_ASSERTION_SEED",
    "_CHECKLIST_JSON_CONTRACT",
    "_EDITABLE_LENS_FIELDS",
    "_EXAMPLE_ANCHOR_KEYS",
    "_JUDGE_JSON_CONTRACT",
    "_PLACEHOLDER",
    "InvalidLensConfig",
    "LensEvaluation",
    "LensUnit",
    "LensVerdictUnreadable",
    "ProviderSpec",
    "QualityLensExample",
    "_apply_lens_fields",
    "_canon_severities",
    "_example_block",
    "_is_int",
    "_is_placeholder_finding",
    "_lens_in_ring",
    "_locate",
    "_next_fork_slug",
    "_normalize_envelope",
    "_read",
    "_reject_non_findings",
    "_reject_non_results",
    "_separation",
    "_spec",
    "_stamp",
    "adjudicable",
    "apply_adjudication",
    "archive_lens",
    "build_checklist_prompt",
    "build_judge_prompt",
    "builtin_specs",
    "canon_category",
    "dataclass",
    "dc_field",
    "effective_severity",
    "engine_family",
    "examples_for",
    "get_lens",
    "get_lens_by_id",
    "lens_owned_by_ring",
    "lens_view",
    "lqa_safety",
    "penalty",
    "post_edit_score",
    "postedit",
    "provider_spec",
    "rank",
    "validate_example_payload",
    "validate_lens",
]


def ensure_builtins(session: Session, *, org_id=None, customer_id=None, created_by="system"):
    """Seed the four builtin lenses for ONE org (L5), idempotently.

    PER ORG AT FIRST USE, not once globally: a builtin is read-only and an edit FORKS to a
    customer-owned copy, so seeding globally would mean one org's fork re-points every other
    org's lens. Returns {slug: QualityLens} for the ring."""
    out: dict = {}
    made = False
    for spec in builtin_specs():
        row = _lens_in_ring(session, spec["slug"], org_id=org_id, customer_id=customer_id)
        if row is None:
            row = QualityLens(
                org_id=org_id,
                customer_id=customer_id,
                builtin=True,
                created_by=created_by,
                max_findings=DEFAULT_MAX_FINDINGS,
                gate_policy=GATE_BLOCK,
                blocking_checks=[],
                normalize_per_words=0,
                **spec,
            )
            # THE seed goes through the SAME write path a fork does, uniqueness included.
            save_lens(session, row, commit=False)
            made = True
        out[spec["slug"]] = row
    if made:
        session.commit()
        for row in out.values():
            session.refresh(row)
    return out


def save_lens(session: Session, lens, *, commit=True):
    """THE single write path for a lens: validate the configuration, enforce slug uniqueness
    inside the ring, then add. T3's CRUD spine calls THIS, never `session.add` directly.

    Uniqueness is enforced here rather than by a UNIQUE index because SQLite cannot express
    "unique per (org_id, customer_id)" over NULLable ring columns without a functional
    coalesce() index (two NULLs never collide), and a claim in a docstring that no code
    enforces is a fiction. Same posture as the exactly-one-parent rule in `record_findings`."""
    validate_lens(lens)
    lens.severities = [str(s).strip().lower() for s in (lens.severities or [])]
    lens.weights = {str(k).strip().lower(): v for k, v in (lens.weights or {}).items()}
    lens.block_severity = str(lens.block_severity or "").strip().lower()
    clash = _lens_in_ring(
        session,
        lens.slug,
        org_id=lens.org_id,
        customer_id=lens.customer_id,
        exclude_id=lens.id,
    )
    if clash is not None:
        raise InvalidLensConfig(
            f"slug {lens.slug!r} is already used by lens id {clash.id} in this org/customer "
            "ring; a lens slug is unique per (org_id, customer_id)"
        )
    session.add(lens)
    if commit:
        session.commit()
        session.refresh(lens)
    return lens


def list_lenses(session: Session, *, org_id=None, customer_id=None, include_archived=False):
    """Every lens visible to this ring (§5.6), org-level and customer-owned, seeding the builtins
    for the ring on first use (L5). Ordered builtins-first then by slug, so the four builtins head
    the list on every surface. Archived lenses are excluded unless `include_archived`."""
    ensure_builtins(session, org_id=org_id, customer_id=customer_id)
    rows = session.exec(select(QualityLens).order_by(QualityLens.id)).all()
    out = []
    for row in rows:
        if not include_archived and row.archived_at is not None:
            continue
        if row.org_id not in (None, org_id):
            continue
        if row.customer_id not in (None, customer_id):
            continue
        out.append(row)
    out.sort(key=lambda r: (not r.builtin, str(r.slug)))
    return out


def create_lens(
    session: Session, *, org_id=None, customer_id=None, created_by="", slug=None, **fields
):
    """Author a NEW customer-owned lens (§5.6, P2-D17: the caller is admin-gated at the surface).
    Validated + slug-uniqueness enforced through `save_lens`. A blank/taken slug is auto-suffixed
    inside the ring. Returns the saved row; raises `InvalidLensConfig` on a bad configuration."""
    base = str(slug or (fields.get("title") or "lens")).strip().lower().replace(" ", "-") or "lens"
    row = QualityLens(
        org_id=org_id,
        customer_id=customer_id,
        builtin=False,
        created_by=str(created_by or ""),
        slug=_next_fork_slug(session, base, org_id=org_id, customer_id=customer_id),
    )
    _apply_lens_fields(row, fields)
    return save_lens(session, row)


def update_lens(
    session: Session, lens_id, *, org_id=None, customer_id=None, created_by="", **fields
):
    """Edit a lens (§5.6). A BUILTIN is read-only, so an edit FORKS to a customer-owned copy (the
    EvalCandidate immutability idiom): the fork carries the merged config and a fresh in-ring slug,
    the builtin is untouched. A customer-owned lens is edited in place. Validated through
    `save_lens`. Raises `InvalidLensConfig` on a bad config; returns None if the lens is out of
    ring."""
    lens = get_lens_by_id(session, lens_id, org_id=org_id, customer_id=customer_id)
    if lens is None:
        return None
    if lens.builtin:
        fork = QualityLens(
            org_id=org_id,
            customer_id=customer_id,
            builtin=False,
            created_by=str(created_by or ""),
            slug=_next_fork_slug(session, lens.slug, org_id=org_id, customer_id=customer_id),
        )
        _apply_lens_fields(fork, {**lens_view(lens), **fields})
        return save_lens(session, fork)
    if not lens_owned_by_ring(lens, org_id=org_id, customer_id=customer_id):
        # GitHub #464 follow-up: a custom lens shared down from a wider ring (NULL org or NULL
        # customer) is readable from here but is NOT this ring's to edit in place.
        return None
    _apply_lens_fields(lens, fields)
    return save_lens(session, lens)


def default_lens_id(session: Session, *, org_id=None, customer_id=None, slug=None) -> int | None:
    """The install default lens id for a ring (P2-D14 / D4): seed the builtins (L5), then resolve
    the configured default slug (`advanced_default_lens`, default 'esa'). `create_mission` stamps
    THIS id explicitly at create time, so a NEW mission is gated by the default lens while a NULL
    lens_id stays reserved for a pre-T3 mission (which runs ungated). Returns None only if the
    resolve fails (a misconfigured default slug), never silently gating against nothing."""
    from app.services import settings

    want = str(slug or settings.get(DEFAULT_LENS_SETTING, BUILTIN_ESA) or BUILTIN_ESA).strip()
    ensure_builtins(session, org_id=org_id, customer_id=customer_id)
    lens = get_lens(session, want, org_id=org_id, customer_id=customer_id)
    if lens is None and want != BUILTIN_ESA:
        lens = get_lens(session, BUILTIN_ESA, org_id=org_id, customer_id=customer_id)
    return lens.id if lens is not None else None


def build_prompt(lens, unit, examples=()) -> str:
    """The prompt for this lens KIND. `post_edit_effort` has no prompt: it makes no judge
    call at all (section 3.4)."""
    if lens.kind == QualityLensKind.CHECKLIST:
        return build_checklist_prompt(lens, unit, examples)
    if lens.kind == QualityLensKind.POST_EDIT_EFFORT:
        raise InvalidLensConfig("the post_edit_effort lens makes no judge call")
    return build_judge_prompt(lens, unit, examples)


def _omission_span_fallback(raw, source, target):
    """H2 fallback for an OMISSION whose SOURCE substring arrived in `span` instead of
    `source_span` (the live gemma shape: `{"kind":"omission","span":"{count}",...}`). Returns the
    (start, end, span_text) SOURCE anchor, or None when the `span` cannot honestly serve as one.

    FAIL-CLOSED BY CONSTRUCTION. The `span` is accepted as a SOURCE anchor ONLY when it occurs in
    the SOURCE and does NOT occur in the TARGET: a target-anchored string can therefore never be
    laundered into a source anchor (H1/H2), and an ambiguous string present in both still rejects.
    Resolution runs THROUGH `_locate` against the source, so the occurrence-ordinal rule, the
    offsets/quoted-span disagreement rule and the whole-verdict reject discipline are unchanged --
    a `span` that occurs many times in the source with no ordinal still raises, exactly as a
    source_span would."""
    span = raw.get("span")
    span = span if isinstance(span, str) else None
    if not span or not span.strip():
        return None
    needle = span if span in source else span.strip()
    if not needle or needle not in source or needle in target:
        return None
    return _locate(
        source,
        {"source_span": span, "occurrence": raw.get("occurrence")},
        prefix="source_",
        label="source (omission fallback)",
    )


def _canon_category(lens, raw):
    """Section 3.2 step 7. With a taxonomy, a category whose HEAD is outside it REJECTS (and
    the canonical head capitalization is written back with the sub-category preserved). With
    NO taxonomy the lens has declared the taxonomy expendable, so a chatty judge's category is
    STRIPPED rather than failing the run."""
    return canon_category(lens.dimensions, raw.get("category"))


def parse_findings(text, lens, source, target) -> list[dict]:
    """The judge's verdict as substrate findings, or `LensVerdictUnreadable` (section 3.2).

    ORDER OF OPERATIONS, and EVERY failure rejects the WHOLE verdict (raises, never a partial
    accept): normalize the envelope, backstop the shape, then per finding: kind, span
    location, severity vocabulary, category; then the per-segment cap; and the provenance
    strip, which here is a whitelist rebuild -- the parser constructs every finding out of the
    contract's own fields, so a judge-authored `origin`/`adjudication_outcome`/`engine_ref`
    cannot survive at all. A judge must never author its own provenance."""
    try:
        data = llm_json.loads(text, reject=_reject_non_findings)
    except Exception as exc:  # json.JSONDecodeError and friends
        raise LensVerdictUnreadable("judge output carried no readable JSON") from exc
    data = _normalize_envelope(data, "findings", "findings")
    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        # The CLEAN FAST PATH backstop: llm_json returns a lone stray dict BEFORE the reject
        # belt runs, so the belt never screened it. A dict without a LIST-valued `findings` is
        # not a verdict; falling through would iterate to zero findings and report a clean
        # pass for content the judge flagged.
        raise LensVerdictUnreadable("verdict carried no findings list")
    if raw_findings and not any(isinstance(e, dict) for e in raw_findings):
        # WRAPPED/UNWRAPPED SYMMETRY with the array arm above: the identical content as a
        # bare top-level array already fails closed, so the wrapped shape must too.
        raise LensVerdictUnreadable("findings list carried no objects")
    # NAMED RECORD (L7), not an oversight: a MIXED list keeps its dict members and DROPS the
    # non-dict junk interleaved with them, which is a partial accept of the list. It is lqa's
    # own status quo for the same shape, and the safe direction here (the dropped members
    # carry no severity, so nothing is silently downgraded); the all-non-dict case, which
    # WOULD be a false clean pass, fails closed two lines above.
    items = [e for e in raw_findings if isinstance(e, dict)]
    sevs = _canon_severities(lens)
    out: list[dict] = []
    for raw in items:
        kind = str(raw.get("kind") or QualityFindingKind.SPAN).strip().lower()
        if kind not in QualityFindingKind.ALL:
            raise LensVerdictUnreadable(f"unknown finding kind {kind!r}")
        # Anchor resolution differs by KIND, and the difference is load-bearing. A SPAN is
        # anchored in the TARGET; an OMISSION is anchored in the SOURCE (it has nothing to point
        # at in the target by definition). Resolving an omission's substring against the TARGET is
        # exactly the item-15 gap that made every real omission `LensVerdictUnreadable`, so the
        # omission arm NEVER touches the target locate.
        if kind == QualityFindingKind.SPAN:
            target_anchor = _locate(target, raw, prefix="", label="target")
            # A source_span on a SPAN finding is still validated (a broken one rejects the whole
            # verdict) and then discarded: source offsets are the omission contract (section 2.3).
            _locate(source, raw, prefix="source_", label="source")
            if target_anchor is None:
                # A span finding that supplies only a source span rejects: it is either an
                # omission mislabelled, or an anchor into the wrong string. Do NOT relax this.
                raise LensVerdictUnreadable("span finding carries no target span")
            source_anchor = None
        else:
            source_anchor = _locate(source, raw, prefix="source_", label="source")
            if source_anchor is None:
                # H2 fallback: a judge that reported the omitted SOURCE substring under `span`
                # rather than `source_span`. Accepted only when it is a source-and-not-target
                # anchor (fail-closed); see `_omission_span_fallback`.
                source_anchor = _omission_span_fallback(raw, source, target)
            if source_anchor is None:
                raise LensVerdictUnreadable("omission finding carries no source span")
            # R2-5 (gate round 2), DELIBERATE and pinned: an omission is anchored in the SOURCE
            # only. When a valid `source_span` resolves above, the fallback is skipped and `span`
            # is NOT target-located -- a stray or garbled `span` on an omission finding is IGNORED,
            # not a whole-verdict reject. This is required by the dual-field contract §2.3 now
            # advertises BOTH `span` and `source_span`: a judge that fills both would otherwise
            # re-open the item-15 gap (its own honest `source_span` rejected because it also filled
            # `span`). Not a laundering hole: the ACCEPTED anchor is always the SOURCE one, and
            # `span_text` on an accepted omission is always '' (nothing about the target claimed).
            # Pinned by verify_quality_lens_parser::test_omission_span_ignored_when_source_span_ok.
            target_anchor = None
        severity = str(raw.get("severity") or "").strip().lower()
        if severity not in sevs:
            # NORMALIZE-AND-WRITE-BACK, then fail closed on what is left: an off-vocabulary
            # severity ("moderate", or a "..." placeholder that slipped a partial echo) would
            # otherwise score as a benign penalty-1 minor through weights.get(sev, 1) and,
            # being neither major nor critical, could AUTO-APPROVE real findings.
            raise LensVerdictUnreadable(f"severity {severity!r} is not in the lens vocabulary")
        category = _canon_category(lens, raw)
        note = raw.get("note")
        out.append(
            {
                "kind": kind,
                "severity": severity,  # canonical, written back
                "category": category,
                "note": str(note) if isinstance(note, (str, int, float)) else "",
                "span_start": target_anchor[0] if target_anchor else None,
                "span_end": target_anchor[1] if target_anchor else None,
                "span_text": target_anchor[2] if target_anchor else "",
                "source_start": source_anchor[0] if source_anchor else None,
                "source_end": source_anchor[1] if source_anchor else None,
                "source_span_text": source_anchor[2] if source_anchor else "",
                "origin": QualityFindingOrigin.JUDGE,
                "check_id": None,
                "blocking": False,
                "precision": None,
                "adjudication_outcome": None,
            }
        )
    cap = int(getattr(lens, "max_findings", DEFAULT_MAX_FINDINGS) or DEFAULT_MAX_FINDINGS)
    if len(out) > cap:
        # P2-D10: exceeding the cap rejects the WHOLE verdict. Truncating a runaway list
        # silently IMPROVES the score, and the house doctrine is reject, never partial.
        raise LensVerdictUnreadable(f"verdict carried {len(out)} findings, over the cap of {cap}")
    return out


def parse_checklist(text, lens, source, target) -> tuple[list[dict], int, int]:
    """The checklist verdict: (findings, passed_count, total) (section 3.4, M6).

    Contract: exactly one result per assertion, indices complete and unique, `passed` STRICTLY
    boolean. Anything else rejects the whole verdict. A false assertion becomes ONE finding at
    `lens.block_severity`, with its span when one was supplied."""
    assertions = [str(a) for a in (lens.assertions or [])]
    if not assertions:
        raise InvalidLensConfig("a checklist lens needs at least one assertion")
    try:
        data = llm_json.loads(text, reject=_reject_non_results)
    except Exception as exc:
        raise LensVerdictUnreadable("checklist output carried no readable JSON") from exc
    data = _normalize_envelope(data, "results", "checklist")
    results = data.get("results")
    if not isinstance(results, list):
        raise LensVerdictUnreadable("checklist verdict carried no results list")
    rows = [r for r in results if isinstance(r, dict)]
    if len(rows) != len(results) or len(rows) != len(assertions):
        raise LensVerdictUnreadable(
            f"checklist verdict carried {len(results)} results for {len(assertions)} assertions"
        )
    seen: dict = {}
    for row in rows:
        idx = row.get("assertion_index")
        if not _is_int(idx) or not 0 <= idx < len(assertions):
            raise LensVerdictUnreadable(f"assertion_index {idx!r} is out of range")
        if idx in seen:
            raise LensVerdictUnreadable(f"assertion_index {idx} appeared twice")
        if not isinstance(row.get("passed"), bool):
            # STRICTLY boolean: a "true"/1/"yes" would make a truthiness coercion decide a
            # delivery gate, and an empty string would silently read as a failed assertion.
            raise LensVerdictUnreadable(f"assertion {idx} did not carry a boolean 'passed'")
        seen[idx] = row
    block = str(lens.block_severity or SEED_BLOCK_SEVERITY).strip().lower()
    findings: list[dict] = []
    passed_count = 0
    for idx in range(len(assertions)):
        row = seen[idx]
        if row.get("passed"):
            passed_count += 1
            continue
        anchor = _locate(target, row, prefix="", label="target")
        note = row.get("note")
        findings.append(
            {
                "kind": QualityFindingKind.SPAN,
                "severity": block,
                "category": None,
                "note": str(note) if isinstance(note, (str, int, float)) else assertions[idx],
                "span_start": anchor[0] if anchor else None,
                "span_end": anchor[1] if anchor else None,
                "span_text": anchor[2] if anchor else "",
                "source_start": None,
                "source_end": None,
                "source_span_text": "",
                "origin": QualityFindingOrigin.JUDGE,
                "check_id": None,
                "blocking": False,
                "precision": None,
                "adjudication_outcome": None,
                "assertion_index": idx,
            }
        )
    return findings, passed_count, len(assertions)


def score_findings(lens, findings) -> tuple[int, bool, list]:
    """(score, passed, blocking_check_ids) for a judge-kind lens (section 3.3).

    For `lens.slug == "mqm"` with the seeded weights and threshold the first two clauses are
    byte-identical to `lqa._score`; the parity test pins that on a fixed corpus INCLUDING
    adjudicated findings, because that is the only shape where the two can diverge.

    `gate_policy='flag'` (N-M9) governs the DETERMINISTIC CHECKS' half of the gate: their
    findings are still recorded and still REPORTED in the score, but they do not fail the
    gate, through ANY of the three clauses -- the blocking flag, the severity clause, or the
    numeric bar. All three must exclude them together, or `flag` is inert: excluding them from
    the blocking flag alone leaves the severity clause failing the unit (every blocking check
    emits a major), and excluding them from both still leaves three check findings summing 15
    penalty points and dropping a clean unit to 85 against a bar of 90. The REPORTED score
    stays honest (it counts every finding, so the operator sees what the checks found); only
    the PASS DECISION is computed over the gating list."""
    findings = list(findings or [])
    value = max(0, 100 - penalty(lens, findings))
    gating = findings
    gate_value = value
    if str(getattr(lens, "gate_policy", GATE_BLOCK)) != GATE_BLOCK:
        gating = [f for f in findings if f.get("origin") != QualityFindingOrigin.CHECK]
        gate_value = max(0, 100 - penalty(lens, gating))
    bar = rank(lens, lens.block_severity)
    # Under `flag` this list is EMPTY BY DESIGN (nothing blocked, because nothing was allowed
    # to). A surface that wants to render "this WOULD have blocked under `block`" reads
    # `finding.blocking` off `evaluation.findings`, which is stamped whatever the policy says.
    blocking_ids = [
        f.get("check_id")
        for f in gating
        if f.get("origin") == QualityFindingOrigin.CHECK and f.get("blocking")
    ]
    passed = (
        gate_value >= int(lens.threshold)
        and not any(rank(lens, effective_severity(f)) >= bar for f in gating)
        and not blocking_ids
    )
    return value, passed, [b for b in blocking_ids if b]


def judge_unit(lens, unit, engine, *, session=None, examples=None, tap_meta=None) -> LensEvaluation:
    """ONE callable owns: prompt build, provider call, parse, checks, and score (H10). T3's
    tool and T2's meta-eval BOTH call it; nothing reimplements either half.

    IT WRITES NO QUALITY-DOMAIN OR CONTENT ROW (N-H1, N2-H1), stated precisely because the
    blanket phrasing "performs no database writes" would be false and would license a vacuous
    test. It does not write `QualityFinding`, `QualityLens*`, `AdvancedStagedTarget` or any
    production content row. It DOES cause writes, and they are correct: the provider call
    books spend through `spend.record_usage`, which opens its OWN session and commits a
    `SpendLedger` row on a separate connection (and returns early with no write when the cost
    is 0, which is why an all-local run books nothing there), and prompt-tap capture likewise
    opens its own session. Those rows are EXPECTED and REQUIRED: they are what makes the "$0
    on the local rig" claim checkable at all, since $0 is a ledger fact and not an assertion.
    That is also why the `task="quality_judge"` + `tap_meta` correlation below is not in
    tension with purity: it must NOT be resolved by stripping the correlation.

    Persisting is `record_findings`, a separate callable T3's tool invokes. The separation is
    load-bearing: if this callable wrote findings, ONE meta-eval run would insert production
    findings for every gold item on every repeat, which is exactly the violation the Lab's
    isolation rule forbids."""
    evaluation_id = uuid.uuid4().hex
    spec = _spec(engine)
    warnings: list[str] = []
    judge_family = engine_family(spec.provider, spec.model) if spec else ""
    translator_family = engine_family(unit.translator_ref, unit.translator_model)
    separation = _separation(judge_family, translator_family)
    if separation is False:
        # P2-D12: WARN and record, never refuse. A refusal would make $0 all-local testing
        # impossible, since that rig runs conductor, delegates and judge on one model.
        warnings.append(
            "The judge and the translator share the model family "
            f"'{judge_family}'; a judge that grades its own family is a weaker signal."
        )
    elif separation is None and (unit.translator_ref or unit.translator_model):
        warnings.append(
            "Judge or translator model family could not be resolved, so family separation "
            "is unknown."
        )

    def _evaluation(**kw):
        base = {
            "evaluation_id": evaluation_id,
            "lens_id": getattr(lens, "id", None),
            "lens_slug": getattr(lens, "slug", ""),
            "kind": lens.kind,
            "state": EVAL_OK,
            "findings": [],
            "blocking_check_ids": [],
            "warnings": warnings,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "unit_key": str(unit.unit_key or ""),
            "engine_ref": spec.provider if spec else None,
            "engine_model": spec.model if spec else None,
            "engine_family": judge_family or None,
            "translator_family": translator_family or None,
            "family_separation": separation,
        }
        base.update(kw)
        return LensEvaluation(**base)

    check_findings = checks.run(lens, unit)

    if lens.kind == QualityLensKind.POST_EDIT_EFFORT:
        # NO judge call at all (section 3.4). With no post-edit leg the lens returns
        # not_applicable, NEVER a 0: "nobody post-edited this" and "the post-editor rewrote
        # every word" are different facts and must not be the same number.
        if unit.baseline_text is None or unit.final_text is None:
            # The lens declines to SCORE, but a blocking check still holds the unit: "no
            # post-edit happened" is a statement about effort, not permission to deliver an
            # empty target (L2-R2). Score stays NULL, state stays not_applicable, and the
            # findings ride along so the digest can say what held it.
            _v, _passed, na_blocking = score_findings(lens, check_findings)
            _stamp(check_findings, spec, judge_family, unit, translator_family, separation)
            return _evaluation(
                state=EVAL_NOT_APPLICABLE,
                score=None,
                passed=None,
                blocked=bool(na_blocking),
                findings=check_findings,
                blocking_check_ids=na_blocking,
            )
        value = post_edit_score(unit.baseline_text, unit.final_text)
        _v, passed, blocking_ids = score_findings(lens, check_findings)
        passed = passed and value >= int(lens.threshold)
        _stamp(check_findings, spec, judge_family, unit, translator_family, separation)
        return _evaluation(
            score=value,
            passed=passed,
            blocked=not passed,
            findings=check_findings,
            blocking_check_ids=blocking_ids,
        )

    if spec is None:
        raise ValueError("quality_lens.judge_unit requires a judge engine (provider/model)")
    if examples is None:
        examples = examples_for(session, lens, unit) if session is not None else []
    prompt = build_prompt(lens, unit, examples)
    try:
        res = run_completion(
            spec,
            prompt,
            system=JUDGE_SYSTEM,
            task="quality_judge",
            payload={"source": unit.source_locale, "target": unit.target_locale},
            tap_meta=tap_meta,
        )
    except DeadEngineError as exc:
        # Live-configured but the engine is dead. A configured judge that could not run is
        # UNREADABLE, never a clean pass. The check findings ride along so the tool still names
        # a deterministic break (M6).
        raise LensVerdictUnreadable(
            "the judge engine is unavailable", check_findings=check_findings
        ) from exc
    cost = float(res.get("cost_usd") or 0.0)
    tin, tout = usage_token_counts(res.get("usage"))
    tin, tout = int(tin or 0), int(tout or 0)
    if res.get("error") is not None or not (res.get("text") or "").strip():
        # run_completion swallows every provider failure and returns error=<str> with an empty
        # text, and the billing-trap arm returns a blank text with error=None. BOTH are a
        # judge that did not deliver: fail closed rather than parse "" into zero findings and
        # report a perfect score. The cost trailer rides on the exception so a paid-but-empty
        # call is still booked honestly (N-M2); the check findings ride along too (M6).
        raise LensVerdictUnreadable(
            "the judge returned no verdict",
            cost_usd=cost,
            tokens_in=tin,
            tokens_out=tout,
            check_findings=check_findings,
        )
    try:
        # The WHOLE post-call section is inside the try (M1): a PAID call that then trips a bug in
        # parse/score/stamp must still book its real spend and stay a per-unit block, never a $0
        # zero-cost return and never a run-killer. Every arm below re-raises as
        # `LensVerdictUnreadable` with the cost trailer AND the check findings attached.
        if lens.kind == QualityLensKind.CHECKLIST:
            findings, passed_count, total = parse_checklist(
                res["text"], lens, _read(unit, "source_text"), _read(unit, "target_text")
            )
            value = round(100 * passed_count / total) if total else 0
        else:
            findings = parse_findings(
                res["text"], lens, _read(unit, "source_text"), _read(unit, "target_text")
            )
            value = None
        all_findings = findings + check_findings
        computed, passed, blocking_ids = score_findings(lens, all_findings)
        if value is None:
            value = computed
        else:
            # The checklist score is its own contract (the pass fraction); the gate clauses still
            # apply, so a blocking check finding holds the unit whatever the fraction says.
            passed = passed and value >= int(lens.threshold)
        _stamp(all_findings, spec, judge_family, unit, translator_family, separation)
    except LensVerdictUnreadable as exc:
        raise LensVerdictUnreadable(
            str(exc),
            cost_usd=cost,
            tokens_in=tin,
            tokens_out=tout,
            check_findings=check_findings,
        ) from exc
    except Exception as exc:  # M1: a PAID call that trips a non-unreadable post-call bug
        raise LensVerdictUnreadable(
            f"judge post-processing failed: {type(exc).__name__}",
            cost_usd=cost,
            tokens_in=tin,
            tokens_out=tout,
            check_findings=check_findings,
        ) from exc
    return _evaluation(
        score=value,
        passed=passed,
        blocked=not passed,
        findings=all_findings,
        blocking_check_ids=blocking_ids,
        cost_usd=cost,
        tokens_in=tin,
        tokens_out=tout,
    )


# ---------------------------------------------------------------------------
# The write side (N-H1) -- invoked ONLY by T3's quality.judge tool
# ---------------------------------------------------------------------------
def record_findings(
    session: Session,
    evaluation: LensEvaluation,
    *,
    org_id=None,
    customer_id=None,
    run_id=None,
    segment_id=None,
    document_id=None,
    staged_target_id=None,
    unit_key=None,
    source_locale="",
    target_locale="",
    content_type_id=None,
    commit=True,
) -> list:
    """Persist one evaluation's findings. THE SINGLE WRITER.

    EXACTLY-ONE-PARENT (M15) is enforced HERE, because SQLite cannot express it as a
    constraint and an unenforced comment would be a fiction. `org_id`/`customer_id` are
    DENORMALIZED onto every row from the caller's ring, so a findings query never needs a
    three-table join to establish confinement."""
    parents = [p for p in (run_id, segment_id, document_id) if p is not None]
    if len(parents) != 1:
        raise ValueError(
            "a QualityFinding needs exactly one parent (run_id, segment_id or document_id); "
            f"got {len(parents)}"
        )
    rows = []
    for f in evaluation.findings or []:
        row = QualityFinding(
            evaluation_id=evaluation.evaluation_id,
            org_id=org_id,
            customer_id=customer_id,
            run_id=run_id,
            segment_id=segment_id,
            document_id=document_id,
            staged_target_id=staged_target_id,
            unit_key=unit_key if unit_key is not None else (evaluation.unit_key or ""),
            lens_id=evaluation.lens_id,
            lens_slug=evaluation.lens_slug or "",
            origin=f.get("origin") or QualityFindingOrigin.JUDGE,
            kind=f.get("kind") or QualityFindingKind.SPAN,
            check_id=f.get("check_id"),
            severity=f.get("severity") or "",
            category=f.get("category"),
            span_start=f.get("span_start"),
            span_end=f.get("span_end"),
            span_text=f.get("span_text") or "",
            source_start=f.get("source_start"),
            source_end=f.get("source_end"),
            source_span_text=f.get("source_span_text") or "",
            note=f.get("note") or "",
            engine_ref=f.get("engine_ref"),
            engine_model=f.get("engine_model"),
            engine_family=f.get("engine_family"),
            translator_ref=f.get("translator_ref"),
            translator_model=f.get("translator_model"),
            translator_family=f.get("translator_family"),
            family_separation=f.get("family_separation"),
            source_locale=source_locale or "",
            target_locale=target_locale or "",
            content_type_id=content_type_id,
            adjudication_outcome=f.get("adjudication_outcome"),
        )
        session.add(row)
        rows.append(row)
    if commit and rows:
        session.commit()
        for row in rows:
            session.refresh(row)
    return rows
