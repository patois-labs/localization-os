"""quality_lens: seed constants: copies of the classic mqm table (m18)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from dataclasses import dataclass
from dataclasses import field as dc_field

from sqlmodel import Session, select

from app.models import (
    QualityFindingKind,
    QualityFindingOrigin,
    QualityLens,
    QualityLensExample,
    QualityLensKind,
)
from app.services import checks, lqa_safety, postedit
from app.services.providers import (
    ProviderSpec,
)

SEED_MQM_DIMENSIONS = [
    "Accuracy",
    "Fluency",
    "Terminology",
    "Locale convention",
    "Style",
    "Verity",
    "Design",
    "Internationalization",
]


SEED_WEIGHTS = {"minor": 1, "major": 5, "critical": 10, "neutral": 0}


SEED_THRESHOLD = 90


SEED_SEVERITIES = ["neutral", "minor", "major", "critical"]


SEED_BLOCK_SEVERITY = "major"


DEFAULT_MAX_FINDINGS = 5


MAX_FINDINGS_CEILING = 20


MAX_EXAMPLES = 4  # per (lens, customer, locale pair) at prompt-build time (P2-D6)


GATE_BLOCK = "block"


GATE_FLAG = "flag"


GATE_POLICIES = (GATE_BLOCK, GATE_FLAG)


EVAL_OK = "ok"


EVAL_NOT_APPLICABLE = "not_applicable"  # the Lab's own rule: never a 0


BUILTIN_MQM = "mqm"


BUILTIN_ESA = "esa"


BUILTIN_CHECKLIST = "checklist"


BUILTIN_POST_EDIT_EFFORT = "post_edit_effort"


BUILTIN_SLUGS = (BUILTIN_MQM, BUILTIN_ESA, BUILTIN_CHECKLIST, BUILTIN_POST_EDIT_EFFORT)


JUDGE_FORBIDDEN_KEYS = (
    "origin",
    "check_id",
    "blocking",
    "precision",
    "engine_ref",
    "engine_model",
    "engine_family",
    "translator_ref",
    "translator_model",
    "translator_family",
    "family_separation",
    "adjudication_outcome",
    "post_edit_fate",
    "evaluation_id",
    "lens_id",
    "lens_slug",
    "adjudicated",
)


_ASSERTION_SEED = (
    "The translation preserves every fact and number from the source.",
    "The translation keeps every placeholder and inline tag intact.",
    "The translation reads naturally to a native speaker of the target locale.",
)


# --- seed constants: COPIES of the classic MQM table (M18) --------------------
# Pinned equal to lqa.MQM_DIMENSIONS / lqa.PENALTY / lqa.LQA_PASS_THRESHOLD by the suite.
# The vocabulary a judge may use. `neutral` is in it because `effective_severity` downgrades
# a WITHHELD (adjudicated) finding to `neutral`, and that value must carry a weight.
# per (lens, customer, locale pair) at prompt-build time (P2-D6)
# the Lab's own rule: never a 0
# Keys a judge must NEVER author. The parser builds every finding from scratch out of the
# contract's own fields, which is strictly stronger than a blacklist: a judge that emits
# `origin`, `adjudication_outcome`, `engine_ref` or `post_edit_fate` would otherwise forge
# provenance (an `adjudication_outcome="withheld"` silences its own major finding in the
# score). This tuple names the shapes the suite red-teams.
class LensVerdictUnreadable(Exception):
    """The P2 sibling of `lqa.JUDGE_UNAVAILABLE`: a judge ran (and may have been paid) but did
    not deliver a verdict we can honestly score. The unit fails the gate CLOSED and the
    transcript records that the judge did not deliver. It is NEVER a clean pass.

    It carries the call's REAL spend, because the tool boundary that catches it must still
    book what the call cost (N-M2): a blocked-or-unreadable return that omits cost/tokens
    books the call at $0, so a judge that burns tokens and returns garbage spends invisibly
    and the per-run budget never converges."""

    def __init__(self, message, *, cost_usd=0.0, tokens_in=0, tokens_out=0, check_findings=None):
        super().__init__(message)
        self.cost_usd = float(cost_usd or 0.0)
        self.tokens_in = int(tokens_in or 0)
        self.tokens_out = int(tokens_out or 0)
        # M6: the DETERMINISTIC check findings (precision 1.0, produced with no model) computed
        # BEFORE the judge ran. An unreadable JUDGE verdict must NOT discard them -- the tool
        # persists them so a `placeholders` break still names itself instead of "unreadable".
        self.check_findings = list(check_findings or [])


class InvalidLensConfig(ValueError):
    """A lens (or a few-shot example) refused at WRITE time. Every rule this raises for exists
    because the alternative is a gate that reports a policy it does not enforce."""


@dataclass
class LensUnit:
    """The unit under evaluation: the text pair plus everything a check or a prompt needs.

    Deliberately a plain value object, not an ORM row: `judge_unit` is PURE with respect to
    the quality domain (N-H1), and T3 builds one of these from the `AdvancedStagedTarget` row
    it loaded, never from the planner's args (N2-H2, the clean-verdict laundering rule).

    A DATACLASS on purpose, and this is not a style choice. The refusal choke-point tripwire
    (`tests/verify_refusal_choke_point.py`) fails ANY `*.target_text = ...` assignment outside
    its allowlist, because that shape is how machine text historically reached a segment
    without passing `quality.commit_mt_output`. A hand-written `__init__` here would have to
    be exempted whole-file, which would also exempt every FUTURE target_text write in this
    growing module. Declaring the fields instead means the source carries no such assignment
    at all, so the guard keeps its full strength over this file. Nothing here ever writes a
    Segment, a StringKey or any other content row; `_read` below normalizes on the way OUT."""

    unit_key: str = ""
    source_text: str = ""
    target_text: str = ""
    source_locale: str = ""
    target_locale: str = ""
    glossary_terms: list = dc_field(default_factory=list)
    char_limit: object = None
    # The post-edit leg (the `post_edit_effort` lens). Either one None = no post-edit
    # happened, which is NOT_APPLICABLE, never a 0.
    baseline_text: object = None
    final_text: object = None
    translator_ref: object = None
    translator_model: object = None
    content_type_id: object = None


def _read(unit, name) -> str:
    """One text field off a unit as a string. A caller that passes None for a text field gets
    "" here rather than a "None" rendered into a judge prompt."""
    return getattr(unit, name, "") or ""


class LensEvaluation:
    """The value object `judge_unit` RETURNS (section 3.5). It is not a table: T3's tool
    persists it through `record_findings`, and T2's meta-eval reads it and writes nothing."""

    __slots__ = (
        "blocked",
        "blocking_check_ids",
        "cost_usd",
        "engine_family",
        "engine_model",
        "engine_ref",
        "evaluation_id",
        "family_separation",
        "findings",
        "kind",
        "lens_id",
        "lens_slug",
        "passed",
        "score",
        "state",
        "tokens_in",
        "tokens_out",
        "translator_family",
        "unit_key",
        "warnings",
    )

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    def as_payload(self) -> dict:
        """The self-describing transcript/API payload (L6): the surfaces return payloads, not
        summary strings, so "why was this unit held" must be answerable from this dict alone.
        Spans render as the quoted substring, never as bare integers."""
        return {
            "evaluation_id": self.evaluation_id,
            "lens_slug": self.lens_slug,
            "kind": self.kind,
            "state": self.state,
            "score": self.score,
            "passed": self.passed,
            "blocked": self.blocked,
            "blocking_check_ids": list(self.blocking_check_ids or []),
            "warnings": list(self.warnings or []),
            "unit_key": self.unit_key,
            "engine": self.engine_ref,
            "engine_model": self.engine_model,
            "family_separation": self.family_separation,
            "findings": [
                {
                    "kind": f.get("kind"),
                    "severity": f.get("severity"),
                    "effective_severity": effective_severity(f),
                    "category": f.get("category"),
                    "origin": f.get("origin"),
                    "check_id": f.get("check_id"),
                    "blocking": bool(f.get("blocking")),
                    "precision": f.get("precision"),
                    "span": f.get("span_text") or "",
                    "source_span": f.get("source_span_text") or "",
                    "note": f.get("note") or "",
                }
                for f in (self.findings or [])
            ],
        }


# ---------------------------------------------------------------------------
# Lens registry + write-time validation (section 2.1, N-M1)
# ---------------------------------------------------------------------------
def builtin_specs() -> list[dict]:
    """The four seeded builtins (section 3.4). Kept as data so `ensure_builtins` and the
    suite read the SAME definition."""
    judge_checks = list(checks.CHECK_IDS)
    return [
        {
            "slug": BUILTIN_MQM,
            "title": "MQM",
            "kind": QualityLensKind.JUDGE,
            "dimensions": list(SEED_MQM_DIMENSIONS),
            "severities": list(SEED_SEVERITIES),
            "weights": dict(SEED_WEIGHTS),
            "block_severity": SEED_BLOCK_SEVERITY,
            "threshold": SEED_THRESHOLD,
            "checks": judge_checks,
            # The classic path adjudicates approved-term false positives; MQM has the
            # taxonomy that lqa_safety rule 6 keys on, so the path is available here.
            "adjudication": True,
            "assertions": [],
        },
        {
            "slug": BUILTIN_ESA,
            "title": "ESA (spans and severity)",
            "kind": QualityLensKind.JUDGE,
            # NO taxonomy: the Advanced default (parent D4). A category the judge emits
            # anyway is stripped, not rejected.
            "dimensions": [],
            "severities": list(SEED_SEVERITIES),
            # Row 1: the same weights as MQM, so the two builtins differ ONLY in taxonomy at
            # launch and any score delta is attributable to the lens, not to a reweighting.
            "weights": dict(SEED_WEIGHTS),
            "block_severity": SEED_BLOCK_SEVERITY,
            "threshold": SEED_THRESHOLD,
            "checks": judge_checks,
            # OFF (row 17 / P2-D13): lqa_safety rule 6 says an approved-term MISUSE finding is
            # NEVER adjudicated, and that rule keys on the DIMENSION string. Under a
            # no-taxonomy lens the category is NULL, so rule 6 cannot fire and a misuse
            # finding would be silently withheld, which is the exact inversion the rule exists
            # to prevent.
            "adjudication": False,
            "assertions": [],
        },
        {
            "slug": BUILTIN_CHECKLIST,
            "title": "Checklist",
            "kind": QualityLensKind.CHECKLIST,
            "dimensions": [],
            "severities": list(SEED_SEVERITIES),
            "weights": dict(SEED_WEIGHTS),
            "block_severity": SEED_BLOCK_SEVERITY,
            "threshold": SEED_THRESHOLD,
            # The checklist's score is the pass fraction over its own assertions; mixing a
            # penalty sum from the deterministic checks into it would make the number mean two
            # things at once. T3 can enable checks on a customer fork.
            "checks": [],
            "adjudication": False,
            "assertions": list(_ASSERTION_SEED),
        },
        {
            "slug": BUILTIN_POST_EDIT_EFFORT,
            "title": "Post-edit effort",
            "kind": QualityLensKind.POST_EDIT_EFFORT,
            "dimensions": [],
            "severities": list(SEED_SEVERITIES),
            "weights": dict(SEED_WEIGHTS),
            "block_severity": SEED_BLOCK_SEVERITY,
            "threshold": SEED_THRESHOLD,
            "checks": [],
            "adjudication": False,
            "assertions": [],
        },
    ]


def _canon_severities(lens) -> list[str]:
    return [str(s).strip().lower() for s in (getattr(lens, "severities", None) or [])]


def validate_lens(lens) -> None:
    """Refuse a lens that cannot enforce what it reports (section 2.1, N-M1). Raises
    `InvalidLensConfig`; returns None when the lens is writable.

    THE RULE THAT MATTERS MOST is `set(weights) == set(severities)`. `rank()` and the penalty
    sum both read `lens.weights.get(sev, 1)`, so a severity in the vocabulary with no weight
    silently ranks 1, and that breaks the gate in BOTH directions (the spec's N-M1 prose gets
    the direction backwards; errata noted in section 2.1 of the worktree spec):

      * an uncovered BLOCK_SEVERITY ranks 1, which is the FLOOR, so `rank(f) >= rank(block)`
        is true for every finding that carries any weight at all: the lens OVER-blocks, and a
        minor holds the unit;
      * an uncovered FINDING severity ranks 1 too, so a `critical` absent from the weight
        table sits BELOW major's 5 and sails through: the lens UNDER-blocks, which is the
        dangerous direction and the one the suite pins counterfactually.

    One rule refuses both, which is why it is stated as set equality rather than as two
    separate membership checks."""
    kind = str(getattr(lens, "kind", "") or "")
    if kind not in QualityLensKind.ALL:
        raise InvalidLensConfig(
            f"unknown lens kind {kind!r}; expected one of {QualityLensKind.ALL}"
        )
    sevs = _canon_severities(lens)
    if not sevs:
        raise InvalidLensConfig("a lens needs at least one severity in its vocabulary")
    if len(set(sevs)) != len(sevs):
        raise InvalidLensConfig("severities must be unique after canonicalization")
    weights = getattr(lens, "weights", None) or {}
    if not isinstance(weights, dict):
        raise InvalidLensConfig("weights must be a mapping of severity to integer weight")
    lowered_keys = [str(k).strip().lower() for k in weights]
    if len(set(lowered_keys)) != len(lowered_keys):
        raise InvalidLensConfig(
            "weight keys collide after lowercasing; each severity may appear only once"
        )
    wkeys = set(lowered_keys)
    if wkeys != set(sevs):
        raise InvalidLensConfig(
            "weights must cover exactly the severity vocabulary: missing "
            f"{sorted(set(sevs) - wkeys)}, unknown {sorted(wkeys - set(sevs))}"
        )
    for key, value in weights.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidLensConfig(f"weight for {key!r} must be an integer, got {value!r}")
        if value < 0:
            raise InvalidLensConfig(f"weight for {key!r} must not be negative")
    block = str(getattr(lens, "block_severity", "") or "").strip().lower()
    if block not in sevs:
        raise InvalidLensConfig(
            f"block_severity {block!r} is not in the severity vocabulary {sevs!r}"
        )
    if getattr(lens, "adjudication", False) and "neutral" not in sevs:
        # `effective_severity` downgrades a WITHHELD finding to "neutral"; without a weight
        # for it the downgrade would score as a penalty-1 minor.
        raise InvalidLensConfig(
            "a lens with adjudication on must carry 'neutral' in its severity vocabulary"
        )
    if int(getattr(lens, "normalize_per_words", 0) or 0) != 0:
        # M7: written correctly it normalizes the PENALTY, not the score. The column exists so
        # P3's document lenses do not need a migration; the scorer never reads it.
        raise InvalidLensConfig(
            "normalize_per_words is reserved and unimplemented in P2; it must be 0"
        )
    assertions = [
        str(a).strip() for a in (getattr(lens, "assertions", None) or []) if str(a).strip()
    ]
    if kind == QualityLensKind.CHECKLIST and not assertions:
        raise InvalidLensConfig("a checklist lens needs at least one assertion")
    threshold = getattr(lens, "threshold", SEED_THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 100:
        raise InvalidLensConfig("threshold must be an integer in 0..100")
    cap = getattr(lens, "max_findings", DEFAULT_MAX_FINDINGS)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_FINDINGS_CEILING:
        raise InvalidLensConfig(f"max_findings must be an integer in 1..{MAX_FINDINGS_CEILING}")
    policy = str(getattr(lens, "gate_policy", "") or "")
    if policy not in GATE_POLICIES:
        raise InvalidLensConfig(f"gate_policy must be one of {GATE_POLICIES}")
    enabled = [str(c) for c in (getattr(lens, "checks", None) or [])]
    unknown = [c for c in enabled if c not in checks.CHECK_IDS]
    if unknown:
        raise InvalidLensConfig(f"unknown check ids {unknown!r}; expected {checks.CHECK_IDS}")
    if enabled and not {"minor", "major"} <= set(sevs):
        # Same class of hole as N-M1: `checks.run` emits `minor`/`major`, and a vocabulary
        # missing them would make every check finding rank 1 through `weights.get(sev, 1)`.
        raise InvalidLensConfig(
            "a lens that runs deterministic checks must carry 'minor' and 'major' in its "
            "severity vocabulary"
        )
    opted = [str(c) for c in (getattr(lens, "blocking_checks", None) or [])]
    bad_opt = [c for c in opted if c not in checks.OPT_IN_BLOCKING]
    if bad_opt:
        # Row 5 is a ratified policy, not a default: approved-term absence is advisory ONLY,
        # so opting it into blocking is refused rather than silently ignored.
        raise InvalidLensConfig(
            f"only {sorted(checks.OPT_IN_BLOCKING)} may be opted into blocking; got {bad_opt!r}"
        )
    off = [c for c in opted if c not in checks.enabled_checks(lens)]
    if off:
        raise InvalidLensConfig(f"blocking_checks names checks the lens does not run: {off!r}")
    dims = getattr(lens, "dimensions", None) or []
    if not isinstance(dims, list) or any(not str(d).strip() for d in dims):
        raise InvalidLensConfig("dimensions must be a list of non-empty names")


_EXAMPLE_ANCHOR_KEYS = ("score", "rating", "quality")


def validate_example_payload(payload) -> None:
    """Refuse a few-shot example that carries a NUMBER (P2-D6, parent section 6.4). Curated
    span examples are in; scored examples ANCHOR the judge on a value, and the entire point of
    P2 is that the judge stops producing values."""
    if not isinstance(payload, dict):
        raise InvalidLensConfig("a few-shot example must be an object")
    carried = [k for k in _EXAMPLE_ANCHOR_KEYS if payload.get(k) not in (None, "")]
    if carried:
        raise InvalidLensConfig(
            f"a few-shot example may not carry {carried!r}: a scored example anchors the judge "
            "on a number, and a lens judge emits findings, never a score"
        )


def _lens_in_ring(session: Session, slug, *, org_id, customer_id, exclude_id=None):
    """The row holding `slug` in EXACTLY this (org_id, customer_id) ring, or None.

    The ring columns are NULLABLE and a SQL `= NULL` never matches, so the ring comparison is
    done in Python rather than in the WHERE clause: an org-level lens (org_id NULL) is a real,
    common shape, and a query that silently returned nothing for it would make the uniqueness
    check below pass by accident on exactly the rows it most needs to catch. Ordered by id so
    the answer is deterministic even on a database that already carries a duplicate. Includes
    ARCHIVED rows deliberately: a slug is still taken while the row exists, so archiving is
    not a way to smuggle a second live lens onto the same slug."""
    rows = session.exec(
        select(QualityLens).where(QualityLens.slug == str(slug or "")).order_by(QualityLens.id)
    ).all()
    for row in rows:
        if row.id is not None and row.id == exclude_id:
            continue
        if row.org_id == org_id and row.customer_id == customer_id:
            return row
    return None


def get_lens(session: Session, slug, *, org_id=None, customer_id=None):
    """The live lens for a slug inside one ring, or None. A customer-owned fork wins over the
    org-level row; an ARCHIVED lens is never resolved (L4: archive is a soft delete, and a
    soft-deleted lens must not keep gating deliveries).

    DETERMINISTIC: ordered by id, so a database that already carries a duplicate (written
    before `save_lens` existed, or by hand) resolves to the same row on every call rather than
    to whatever the storage engine happened to return first."""
    rows = session.exec(
        select(QualityLens)
        .where(QualityLens.slug == str(slug or ""))
        .where(QualityLens.archived_at == None)  # noqa: E711 -- SQLAlchemy IS NULL
        .order_by(QualityLens.id)
    ).all()
    for want in (customer_id, None):
        for row in rows:
            if row.org_id == org_id and row.customer_id == want:
                return row
    return None


DEFAULT_LENS_SETTING = "advanced_default_lens"  # the install default lens slug (D4: 'esa')


_EDITABLE_LENS_FIELDS = (
    "title",
    "kind",
    "dimensions",
    "severities",
    "weights",
    "block_severity",
    "threshold",
    "max_findings",
    "gate_policy",
    "checks",
    "blocking_checks",
    "adjudication",
    "normalize_per_words",
    "assertions",
)


# ---------------------------------------------------------------------------
# The T3 CRUD spine (SPEC-quality-lenses-p2 §5.6): the surfaces call THESE, never session.add.
# ---------------------------------------------------------------------------
# the install default lens slug (D4: 'esa')
# The fields a CRUD caller (web/API/CLI/MCP) may set on a lens. `slug`, `builtin`, `archived_at`
# and the provenance columns are NEVER caller-set: a builtin is read-only (an edit FORKS), and a
# slug is minted, not chosen freely on an edit. `create_lens` takes the slug explicitly.
def lens_view(lens) -> dict:
    """One lens as a JSON-ready dict -- the ONE shape the web form, the run view, the API, MCP and
    CLI all render, so a lens can never look different on one surface than another."""
    return {
        "id": lens.id,
        "org_id": lens.org_id,
        "customer_id": lens.customer_id,
        "slug": lens.slug,
        "title": lens.title,
        "builtin": bool(lens.builtin),
        "kind": lens.kind,
        "dimensions": list(lens.dimensions or []),
        "severities": list(lens.severities or []),
        "weights": dict(lens.weights or {}),
        "block_severity": lens.block_severity,
        "threshold": lens.threshold,
        "max_findings": lens.max_findings,
        "gate_policy": lens.gate_policy,
        "checks": list(lens.checks or []),
        "blocking_checks": list(lens.blocking_checks or []),
        "adjudication": bool(lens.adjudication),
        "normalize_per_words": lens.normalize_per_words,
        "assertions": list(lens.assertions or []),
        "archived": lens.archived_at is not None,
        "archived_at": lens.archived_at.isoformat() if lens.archived_at else None,
        "created_by": lens.created_by,
        "created_at": lens.created_at.isoformat() if lens.created_at else None,
    }


def get_lens_by_id(session: Session, lens_id, *, org_id=None, customer_id=None):
    """A lens by id, IF it is inside this ring, else None (non-enumerable: a foreign or missing
    lens both look like not-found). Archived rows ARE returned here (a show/CRUD read may want to
    see an archived lens); the RUN resolver `get_lens` excludes them."""
    try:
        lid = int(lens_id)
    except (TypeError, ValueError):
        return None
    lens = session.get(QualityLens, lid)
    if lens is None:
        return None
    if lens.org_id not in (None, org_id) or lens.customer_id not in (None, customer_id):
        return None
    return lens


def lens_owned_by_ring(lens, *, org_id=None, customer_id=None) -> bool:
    """True when `lens` BELONGS to exactly this ring, not merely when it is visible from it.

    `get_lens_by_id` is a READ gate: a lens with a NULL org or NULL customer is shared down to
    every narrower ring, which is right for reading and for resolving a run's gate. It is wrong
    for a WRITE: an in-place edit or archive of a shared lens by a customer-scoped (or any
    org-scoped) caller changes it for every ring it is shared with (GitHub #464 follow-up).
    A write must name the lens's own (org_id, customer_id) pair."""
    return lens.org_id == org_id and lens.customer_id == customer_id


def _next_fork_slug(session: Session, base: str, *, org_id, customer_id) -> str:
    """A free slug for a fork/new lens inside the ring: `base`, else `base-2`, `base-3`, ... The
    same in-ring uniqueness `save_lens` enforces, resolved to a NAME rather than a collision."""
    candidate = str(base or "lens").strip() or "lens"
    n = 1
    while _lens_in_ring(session, candidate, org_id=org_id, customer_id=customer_id) is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def archive_lens(session: Session, lens_id, *, org_id=None, customer_id=None):
    """Soft-archive a lens (§5.6, L4: never a hard delete). An archived lens stops resolving as a
    RUN gate (`get_lens` excludes it) but is never removed, so the findings it produced keep a
    lens to name. A builtin CAN be archived in-ring (it is the ring's own seeded copy). Returns the
    row, or None if out of ring."""
    from app.models import _utcnow

    lens = get_lens_by_id(session, lens_id, org_id=org_id, customer_id=customer_id)
    if lens is None:
        return None
    if not lens_owned_by_ring(lens, org_id=org_id, customer_id=customer_id):
        # A lens shared down from a wider ring is visible here, never archivable from here
        # (non-enumerable: the same None an out-of-ring id gets).
        return None
    if lens.archived_at is None:
        lens.archived_at = _utcnow()
        session.add(lens)
        session.commit()
        session.refresh(lens)
    return lens


def _apply_lens_fields(row, fields) -> None:
    """Copy the editable fields off a CRUD payload onto a lens row (never slug/builtin/provenance).
    Missing fields keep the row's current value (a PATCH-shaped merge), so a partial update does
    not blank the rest of the config."""
    for name in _EDITABLE_LENS_FIELDS:
        if name in fields and fields[name] is not None:
            setattr(row, name, fields[name])


def examples_for(session: Session, lens, unit, *, customer_id=None):
    """At most 4 curated span examples for this (lens, customer, locale pair), ordered by id
    (P2-D6). A READ: `judge_unit` stays pure with respect to the quality domain."""
    if session is None or getattr(lens, "id", None) is None:
        return []
    rows = session.exec(
        select(QualityLensExample)
        .where(QualityLensExample.lens_id == lens.id)
        .order_by(QualityLensExample.id)
    ).all()
    out = []
    for row in rows:
        if row.source_locale and row.source_locale != unit.source_locale:
            continue
        if row.target_locale and row.target_locale != unit.target_locale:
            continue
        if customer_id is not None and row.customer_id not in (None, customer_id):
            continue
        out.append(row)
        if len(out) >= MAX_EXAMPLES:
            break
    return out


JUDGE_SYSTEM = "You find translation errors and report them as spans. Output only JSON."


_JUDGE_JSON_CONTRACT = (
    'Return ONLY JSON: {"findings":[{"kind":"span"|"omission","span":"...",'
    '"source_span":"...","severity":"...","category":"..."|null,"note":"..."}]}'
)


_CHECKLIST_JSON_CONTRACT = (
    'Return ONLY JSON: {"results":[{"assertion_index":0,"passed":true|false,'
    '"span":"..."|null,"note":"..."}]}'
)


# ---------------------------------------------------------------------------
# Prompts (section 3.1, 3.4)
# ---------------------------------------------------------------------------
def _example_block(examples) -> str:
    """Rendered few-shot SPAN examples. NO scores, ever (P2-D6)."""
    lines = []
    for ex in examples or []:
        span = ex.target_text[(ex.span_start or 0) : ex.span_end] if ex.span_end else ""
        parts = [f"  Source: {ex.source_text}", f"  Translation: {ex.target_text}"]
        detail = f'  Finding: span "{span}" severity {ex.severity}'
        if ex.category:
            detail += f" category {ex.category}"
        if ex.note:
            detail += f" note {ex.note}"
        parts.append(detail)
        lines.append("\n".join(parts))
    if not lines:
        return ""
    return "Examples:\n" + "\n".join(lines) + "\n"


def build_judge_prompt(lens, unit, examples=()) -> str:
    """The findings prompt (section 3.1).

    THE THRESHOLD IS NEVER IN THE PROMPT, and neither are the words that anchor a model on a
    number (parent section 6.4). The suite asserts the rendered prompt contains neither
    `str(lens.threshold)` nor the words "score", "pass" or "threshold"."""
    src = unit.source_locale or "source"
    tgt = unit.target_locale or "target"
    sevs = ", ".join(_canon_severities(lens))
    lines = [
        f"Find the errors in this {src}->{tgt} translation.",
        "For each error return the exact substring of the TRANSLATION it covers and a severity.",
        "For an OMISSION, put the substring of the SOURCE that was left out in source_span "
        '(leave span empty) and set kind="omission".',
        f"Severities: {sevs}.",
    ]
    dims = [str(d) for d in (lens.dimensions or [])]
    if dims:
        lines.append("Categories: " + ", ".join(dims) + ".")
    else:
        lines.append("Do not categorize the errors.")
    block = _example_block(examples)
    if block:
        lines.append(block.rstrip("\n"))
    lines.append(f"Source: {_read(unit, 'source_text')}")
    lines.append(f"Translation: {_read(unit, 'target_text')}")
    lines.append("")
    lines.append(_JUDGE_JSON_CONTRACT)
    return "\n".join(lines)


def build_checklist_prompt(lens, unit, examples=()) -> str:
    """The checklist lens gets its OWN prompt and parser contract (M6): one boolean per
    assertion, indexed, so the parser can demand a complete and unique index set."""
    src = unit.source_locale or "source"
    tgt = unit.target_locale or "target"
    lines = [
        f"Check this {src}->{tgt} translation against each numbered assertion.",
        "Answer every assertion exactly once, by its index, with a strict true or false.",
        "When an assertion is false, return the exact substring of the TRANSLATION that shows it.",
        "Assertions:",
    ]
    for i, assertion in enumerate(lens.assertions or []):
        lines.append(f"  {i}. {assertion}")
    block = _example_block(examples)
    if block:
        lines.append(block.rstrip("\n"))
    lines.append(f"Source: {_read(unit, 'source_text')}")
    lines.append(f"Translation: {_read(unit, 'target_text')}")
    lines.append("")
    lines.append(_CHECKLIST_JSON_CONTRACT)
    return "\n".join(lines)


_PLACEHOLDER = "..."


# ---------------------------------------------------------------------------
# The parser (section 3.2) -- every failure rejects the WHOLE verdict
# ---------------------------------------------------------------------------
def _is_placeholder_finding(f) -> bool:
    """A finding is TEMPLATE NOISE iff EVERY judged field still carries the literal "..."
    from the prompt's own template. all(), never any(): a real finding may leave ONE field
    lazy, and rejecting that would drop a genuine finding."""
    return isinstance(f, dict) and all(
        f.get(k) == _PLACEHOLDER for k in ("span", "severity", "note")
    )


def _reject_non_findings(cand) -> bool:
    """The `reject` belt for llm_json.loads: True when `cand` is a dict that is NOT a real
    findings verdict, so a stray dict or a template echo in the preamble cannot pre-empt the
    real verdict that follows it.

    THE BELT IS NOT THE GUARD. It screens TOLERANT-PATH candidates only and explicitly does
    not screen a clean single value returned by the fast path, so every shape check in
    `parse_findings` runs UNCONDITIONALLY on the returned object as well. A reader who trusts
    the belt alone ships a fail-open."""
    if not isinstance(cand, dict):
        return False
    found = cand.get("findings")
    if not isinstance(found, list):
        return True  # no `findings` key, or a non-list one: a stray dict, not a verdict
    if found and all(_is_placeholder_finding(f) for f in found if isinstance(f, dict)):
        return True  # the prompt's own template, parroted back
    # An all-non-dict list filters to zero findings: a false clean pass.
    return bool(found) and not any(isinstance(f, dict) for f in found)


def _reject_non_results(cand) -> bool:
    """The checklist belt, same doctrine as `_reject_non_findings`."""
    if not isinstance(cand, dict):
        return False
    return not isinstance(cand.get("results"), list)


def _normalize_envelope(data, key, kind_label):
    """Section 3.2 step 1 (H2). A TOP-LEVEL ARRAY is a legitimate judge emission (some judges
    reserve the object form for the zero-finding case; this was a live-reproduced HIGH on the
    classic path), so it is normalized to {key: [...]}. Both fail-closed arms come with it:

      * a NON-EMPTY list with ZERO dict members rejects. It would filter to zero findings and
        score a FALSE CLEAN PASS;
      * an empty list is the legitimate zero-finding verdict;
      * any other JSON value (str/int/float/bool/None) rejects EXPLICITLY here rather than by
        an incidental exception from a later `.get()`.
    """
    if isinstance(data, list):
        if data and not any(isinstance(e, dict) for e in data):
            raise LensVerdictUnreadable(
                f"{kind_label} verdict was a non-empty array with no objects in it"
            )
        return {key: data}
    if not isinstance(data, dict):
        raise LensVerdictUnreadable(f"{kind_label} verdict was not an object or an array")
    return data


def _is_int(value) -> bool:
    """A real int. `bool` is an int subclass, and `True` as an offset would read as 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _locate(text, raw, *, prefix, label):
    """Resolve one anchor against `text`: (start, end, span_text), or None when the finding
    carries no anchor of this kind. Raises `LensVerdictUnreadable` when it carries a BROKEN
    one: an unreadable anchor is not a partial accept.

    Offsets are Python CHARACTER indices, not bytes. A string span that occurs more than once
    (L1) must be qualified by explicit offsets or an `occurrence` ordinal; ambiguous and
    unqualified rejects, because scoring the wrong occurrence silently mislocates the error."""
    span_key = "span" if prefix == "" else prefix + "span"
    start_key = prefix + "start"
    end_key = prefix + "end"
    start = raw.get(start_key)
    end = raw.get(end_key)
    span = raw.get(span_key)
    span = span if isinstance(span, str) else None
    if start is not None or end is not None:
        if not (_is_int(start) and _is_int(end)):
            raise LensVerdictUnreadable(f"{label} offsets must be integers")
        if not (0 <= start < end <= len(text)):
            raise LensVerdictUnreadable(
                f"{label} offsets {start}..{end} are outside 0..{len(text)}"
            )
        located = text[start:end]
        if span is not None and span.strip() and located != span and located != span.strip():
            # Both were supplied and they disagree: we cannot tell which the judge meant, so
            # we fail closed rather than pick one.
            raise LensVerdictUnreadable(f"{label} offsets do not cover the quoted span")
        return start, end, located
    if span is None or not span.strip():
        return None
    needle = span if span in text else span.strip()
    count = text.count(needle) if needle else 0
    if not count:
        raise LensVerdictUnreadable(f"{label} span does not occur in the text")
    occurrence = raw.get("occurrence")
    if occurrence is None:
        if count > 1:
            raise LensVerdictUnreadable(
                f"{label} span occurs {count} times and carries neither offsets nor an "
                "occurrence ordinal"
            )
        occurrence = 1
    elif not _is_int(occurrence) or not 1 <= occurrence <= count:
        # Checked on EVERY finding that supplies an ordinal, not only on the ambiguous ones
        # (L2): coercing an out-of-range ordinal to 1 on a span that occurs once would take
        # a judge that named occurrence 4 and silently score occurrence 1 instead.
        raise LensVerdictUnreadable(f"{label} occurrence must be an integer in 1..{count}")
    # NON-OVERLAPPING, to match `str.count`'s own semantics (L1): advancing by ONE character
    # would resolve occurrence 2 of "aa" in "aaaa" to index 1, an overlap `count` never
    # counted, so the ordinal and the count would be measuring different things.
    pos, cursor = -1, 0
    for _ in range(occurrence):
        pos = text.find(needle, cursor)
        cursor = pos + len(needle)
    return pos, pos + len(needle), needle


# ---------------------------------------------------------------------------
# The score (section 3.3)
# ---------------------------------------------------------------------------
def rank(lens, severity) -> int:
    """H4: the block ordering derives from the WEIGHTS, never from the position of a severity
    in the list. A lens that reweights its vocabulary gets a consistent ordering for free;
    index ordering would silently invert on a reordered `severities` list."""
    return int(canon_weights(lens).get(str(severity or "").strip().lower(), 1))


def canon_weights(lens) -> dict:
    """The weight table with stripped lowercase keys (#464). `save_lens` stores keys lowercased,
    but a lens written before that fix keeps its authored case and is never re-saved, so the
    READ side canonicalizes too; otherwise every lookup misses and defaults to 1."""
    weights = getattr(lens, "weights", None) or {}
    return {str(k).strip().lower(): v for k, v in weights.items()}


def effective_severity(finding) -> str:
    """H5: ONE helper, used in BOTH the penalty sum and the block clause. Using the raw
    severity in one and the adjudicated value in the other is exactly how an adjudicated
    finding scores as harmless while still failing the gate, or the reverse."""
    if (finding or {}).get("adjudication_outcome") == "withheld":
        return "neutral"
    return str((finding or {}).get("severity") or "").strip().lower()


def post_edit_score(baseline, final) -> int:
    """M5: `postedit.distances` returns (char, word, pct) and `pct` is ALREADY on a 0-100
    scale, 4dp, so the score is `max(0, 100 - round(pct))`. Reusing that primitive (rather
    than forking a distance) is the sacreBLEU lesson: a distance computed with a different
    tokenizer is a different number."""
    _char, _word, pct = postedit.distances(baseline or "", final or "")
    return max(0, 100 - round(pct))


# ---------------------------------------------------------------------------
# The shared entry point (H10) -- PURE with respect to the quality domain
# ---------------------------------------------------------------------------
def _spec(engine):
    """A ProviderSpec from whatever the caller named the judge engine: a ProviderSpec, a
    committee-shaped {"provider","model"} dict, or a bare provider ref."""
    if engine is None:
        return None
    if isinstance(engine, ProviderSpec):
        return engine
    if isinstance(engine, dict):
        provider = str(engine.get("provider") or "").strip().lower()
        if not provider:
            return None
        return ProviderSpec(provider=provider, model=(engine.get("model") or None))
    provider = str(engine or "").strip().lower()
    return ProviderSpec(provider=provider, model=None) if provider else None


def engine_family(ref, model) -> str:
    """The judge-bias FAMILY of an engine. "" means UNRESOLVED, and unresolved is NEVER a free
    pass: an `engine:<id>` ref is an EngineProfile rather than a model id, so it is resolved
    THROUGH its profile (model id first, then kind) exactly as the P1 adjudicator does. A
    bare-ref comparison reported "family separation: yes" while the adjudicator WAS the engine
    that produced the candidate."""
    from app.services import engines, eval_metrics

    family = eval_metrics.model_family(model)
    if family:
        return family
    ref_s = str(ref or "").strip()
    if not ref_s:
        return ""
    canon = engines.canonicalize_ref(ref_s)
    if canon.lower().startswith("engine:"):
        try:
            pid = int(canon.split(":", 1)[1])
        except (TypeError, ValueError):
            return ""
        try:
            profile = engines.get_profile(pid)
        except Exception:
            return ""  # an advisory label must never fail a run mid-evaluation
        if profile is None:
            return ""
        return eval_metrics.model_family(profile.model) or eval_metrics.model_family(profile.kind)
    return eval_metrics.model_family(ref_s)


def _separation(judge_family, translator_family):
    """Three states, because "we could not tell" is not "they are different": True (families
    resolved and different), False (same family), None (unresolved on either side, rendered as
    unknown on every surface, NEVER as separation)."""
    if not judge_family or not translator_family:
        return None
    return judge_family != translator_family


def _stamp(findings, spec, judge_family, unit, translator_family, separation) -> None:
    """OUR provenance, stamped after the parse: the judge pairing on EVERY finding (section
    7), never anything the judge authored.

    `engine_*` NAMES THE JUDGE, so it is NULL on a `check`-origin finding (L4): no model
    produced it, and stamping the judge engine onto a deterministic check would persist a
    provenance claim that is simply false -- a findings query grouped by engine would credit
    a placeholder-integrity flag to a model that had no part in it, and the family-separation
    figure would be computed over rows no judge authored. The three columns are NULLABLE, so
    None is representable and needs no sentinel. The TRANSLATOR pairing IS stamped on a check
    finding: the checked text really was produced by that engine, which is the fact the
    pairing records."""
    for f in findings:
        judged = f.get("origin") != QualityFindingOrigin.CHECK
        f["engine_ref"] = (spec.provider if spec else None) if judged else None
        f["engine_model"] = (spec.model if spec else None) if judged else None
        f["engine_family"] = (judge_family or None) if judged else None
        f["translator_ref"] = unit.translator_ref
        f["translator_model"] = unit.translator_model
        f["translator_family"] = translator_family or None
        # Separation is a JUDGE-vs-translator comparison; with no judge on this row there is
        # nothing to compare, so it stays None (the "unknown" state), never a claimed True.
        f["family_separation"] = separation if judged else None


# ---------------------------------------------------------------------------
# Adjudication, on findings rather than MQM strings (section 5.4)
# ---------------------------------------------------------------------------
def adjudicable(lens, finding, term) -> bool:
    """Is this finding adjudicable against this legitimately-applied approved term?

    The classic allow-list is MQM-STRING-MATCHED (`_CONTAINS_DIMS`/`_EXACT_DIMS`, matched by
    `dim.startswith(...)`), so under a no-taxonomy lens the category is NULL, every arm
    returns False, and adjudication silently never fires. This sibling decides on the SPAN and
    consults `lens.dimensions` only when the lens HAS a taxonomy.

    IT DECLINES ON AN OMISSION, never crashes: an omission carries no target span, and every
    span predicate in the chain would read None. Declining is also correct on the merits,
    since adjudication asks whether a flagged TARGET span is a legitimately-applied approved
    term, and an omission has no target span to legitimise.

    `lqa_safety.adjudicate` is NOT edited by trains 1 to 3; this is a sibling the advanced
    tier calls, and P2b unifies them."""
    if not getattr(lens, "adjudication", False):
        # Default OFF for a no-taxonomy lens (row 17 / P2-D13): lqa_safety rule 6 (an
        # approved-term MISUSE finding is NEVER adjudicated) keys on the dimension string, so
        # under ESA it cannot fire and a misuse finding would be silently withheld.
        return False
    if not lqa_safety.adjudication_enabled():
        return False  # the install-wide Phase 3 kill switch, honored regardless
    if not isinstance(finding, dict) or not isinstance(term, dict):
        return False
    if finding.get("kind") == QualityFindingKind.OMISSION:
        return False
    span = finding.get("span_text")
    span = span.strip() if isinstance(span, str) else ""
    if not span:
        return False
    if lens.dimensions:
        # A taxonomy is present, so rules 5 and 6 can be decided exactly as the classic path
        # decides them (the misuse rule included), on the finding's own category.
        return lqa_safety._dim_matches(finding.get("category"), span, term)
    surface = lqa_safety._approved_surface(term)
    case_sensitive = bool(term.get("case_sensitive"))
    # The span targets the approved term AND the term is a SUBSTANTIAL FRACTION of the span:
    # the 2026-07-29 proportionality bound, preserved so a finding about a whole clause is not
    # silenced by one approved term buried inside it.
    return lqa_safety._span_targets_term(surface, span, case_sensitive) and (
        lqa_safety._span_is_mostly_term(surface, span, case_sensitive)
    )
