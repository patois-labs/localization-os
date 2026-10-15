"""Workflow SHAPE taxonomy (SPEC-workflows-redesign Phase 3, Tier 2).

A PURE, schema-free classifier: given a workflow's ordered steps, derive the set of
shape tags that describe *how the steps compose* (straight-through, gated, correction
loop, triage, human checkpoint). Shapes are a TAG SET, not mutually exclusive — a
workflow can be gated + correction-loop + human-checkpoint at once — so `detect_shapes`
returns a `ShapeResult` carrying the raw set plus a single display-PRIMARY tag chosen by
the SPEC's recommended priority (Correction loop > Triage > Gated > Straight-through),
with Human-checkpoint as an additive chip.

Everything here is derived from stored `run_condition` / `branch` / `max_visits` /
`step_type` — no new column. The one shape the SPEC calls out as NOT a step property is
**fan-out** (multilingual reach): that comes from `Workflow.group_id` + sibling count and
is added by the DISPLAY site (`fan_out` slug + FAN_OUT label live here for the chip, but
`detect_shapes` never emits it — it has no visibility into groups).

Robustness contract (SPEC "handle malformed/dangling branch keys defensively; never
raise"): every input step may be a `WorkflowStep` ORM row (builder) OR a plain draft
`dict` (the Design-review screen), so all field reads go through `_f`. A branch target
that names a key no step carries (a dangling/renamed edge) is treated as NO edge — it
neither creates a loop nor a fork — and never raises. `detect_shapes([])` is a valid
straight-through.

Ordinal note: a branch's loop-back is defined by the SPEC as "targets an equal-or-earlier
`order_index`". We compare LIST POSITION (the step's index in the passed, order-sorted
list) — identical to `order_index` for `orchestrator.get_steps` output (ORDER BY
order_index) and for the draft's authored order, and it is exactly the ordinal the
`wf_graph` macro uses (`loop.index0`), so the graph and the classifier never disagree.

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

from app.models import StepType

# --- tag slugs (stable; used as JSON values, CSS hooks, and dict keys) --------
STRAIGHT_THROUGH = "straight-through"
GATED = "gated"
CORRECTION_LOOP = "correction-loop"
TRIAGE = "triage"
HUMAN_CHECKPOINT = "human-checkpoint"
FAN_OUT = "fan-out"
# SPEC-parallel-routing §6. Like human-checkpoint, this is ADDITIVE, never primary: a
# workflow that runs two checks together is still fundamentally gated / a correction
# loop / straight-through -- parallelism is how the checks are scheduled, not what the
# graph does. Derived purely from stored `parallel_group`, no new column.
PARALLEL_CHECKS = "parallel-checks"

# Human-facing labels (chips / CLI / headline). Calm, precise — never "agent".
SHAPE_LABELS = {
    STRAIGHT_THROUGH: "Straight-through",
    GATED: "Gated",
    CORRECTION_LOOP: "Correction loop",
    TRIAGE: "Triage",
    HUMAN_CHECKPOINT: "Human checkpoint",
    FAN_OUT: "Fan-out",
    PARALLEL_CHECKS: "Parallel checks",
}

# One-line "what this is" tooltip per shape (used on the table chip title).
SHAPE_HINTS = {
    STRAIGHT_THROUGH: "Each step feeds the next, top to bottom — no gates or loops.",
    GATED: "A check decides whether the flow continues.",
    CORRECTION_LOOP: "A failed check loops back to an earlier step to fix and re-check.",
    TRIAGE: "A check routes the flow down one of two forward paths.",
    HUMAN_CHECKPOINT: "A person reviews before the run finishes.",
    FAN_OUT: "One workflow with a variant per language.",
    PARALLEL_CHECKS: "Two or more checks run at the same time on the same text.",
}

# Seed sentences for the Workflow Studio "shape gallery" (Wave B): one-line
# descriptions the Design-a-workflow drafting LLM reliably expands into the named
# shape. Each is a complete, self-contained instruction — click a card and it drops
# into the description box as a starting point the operator can edit. Keyed by the
# same shape slugs so the gallery and the taxonomy can never drift.
SHAPE_SEEDS = {
    STRAIGHT_THROUGH: "Translate the source into the target language in a single pass and output "
    "the finished translation.",
    GATED: "Translate the text, then run an MQM LQA quality check that must score at "
    "least 90 for the translation to pass. Give THAT check a branch of on_pass 'end' "
    "and on_fail 'end': a passing translation finishes the run, and a failing check "
    "stops the run immediately without continuing to a review or post-edit step.",
    # 2026-09-16: the shape he actually builds. Three things the old one-liner left
    # to the drafter and the drafter got wrong every time (measured live, three drafts):
    # it opened on Translate with no TM step; it left the FIRST check unbranched, so a
    # PASSING translation fell through the (prev_lqa:failed-gated) fix step and into the
    # second check instead of finishing; and it described the findings in prose, so the
    # fix prompt never carried {lqa_findings}. Naming `on_pass 'end'` is what makes the
    # first check terminal -- 'end' is not a jump, so it is legal on any gate step, where
    # "on fail go to the fix step" cannot be a branch at all (forward jumps are
    # unsupported; that is what the fix step's run_condition is for).
    CORRECTION_LOOP: "Start with a TM match step that fills and locks the qualifying 100% "
    "translation-memory matches, then translate the rest. Run an MQM LQA check and give "
    "THAT check a branch of on_pass 'end' and on_fail 'next', so a passing translation "
    "finishes the run and only a failing one continues to a post-edit step whose prompt "
    "contains {prior}, {lqa_findings} and {target_locale} so it acts on the findings in "
    "the right language; then re-check "
    "with a second MQM LQA check that ends on pass and loops back to the post-edit step "
    "on fail, looping up to 2 times.",
    # qa-0916w #150: the old seed described the INTENT ("route only the low-scoring
    # segments") without saying how this system expresses it, and the drafter failed three
    # times out of three -- once with a hard validation error (a run_condition on prev_qe
    # with no QE-gated step), once with a FORWARD branch that was dropped as unsupported,
    # and once with no routing at all while the notes claimed it routed. Forward jumps do
    # not exist here, so "send the low scorers onward" is never a branch: it is on_pass
    # 'end' on the check plus a run_condition on the step that only the failures should
    # reach. Same fix as the correction loop above, for the same reason.
    TRIAGE: "Translate the text, then run an MQM LQA check and give THAT check a branch of "
    "on_pass 'end' and on_fail 'next', so a high-scoring translation finishes the run "
    "immediately; only a low-scoring one continues to a human post-edit step that carries "
    "run_condition prev_lqa:failed so it runs for the failures alone.",
    HUMAN_CHECKPOINT: "Translate the text, then pause for a human reviewer to approve or correct "
    "the result before the run finishes.",
}

# The five taxonomy shapes offered in the gallery, in a deliberate learn-by-reading
# order (simplest first, ending on the human checkpoint). FAN_OUT is excluded — it is
# a group property (a variant per language), not a step-composition seed you can draft.
_GALLERY_ORDER = [STRAIGHT_THROUGH, GATED, CORRECTION_LOOP, TRIAGE, HUMAN_CHECKPOINT]


def shape_seed_cards() -> list:
    """The shape-gallery cards for the Workflow Studio aids rail: one per taxonomy
    shape (excluding fan-out), each carrying its label, one-line hint, and the seed
    sentence a click inserts. Read straight from SHAPE_LABELS/SHAPE_HINTS/SHAPE_SEEDS
    so the gallery, the dashboard chips, and the CLI share one taxonomy source."""
    return [
        {
            "slug": slug,
            "label": SHAPE_LABELS[slug],
            "hint": SHAPE_HINTS[slug],
            "seed": SHAPE_SEEDS[slug],
        }
        for slug in _GALLERY_ORDER
    ]


# Display priority for the single PRIMARY tag (SPEC recommendation).
_PRIMARY_PRIORITY = [CORRECTION_LOOP, TRIAGE, GATED, STRAIGHT_THROUGH]

# Origin (Workflow.created_via) -> human label. NULL/unknown -> None (render nothing).
# "eval_promotion" (app/services/eval_promote.py, PROPOSAL-evaluations-lab section 9)
# is a THIRD genuine origin: this workflow came out of an Evaluations Lab experiment
# via the promotion door, not a hand build and not the draft authoring path. Without
# an entry here origin_label returns None and a promoted workflow renders no chip at
# all -- the provenance the promotion exists to create would be invisible.
# "showcase-seed" is stamped by scripts/showcase_seed.py (SHOWCASE_MARKER) on the demo
# workflows; the missing entry here rendered them origin=unknown (issue #183).
ORIGIN_LABELS = {
    "manual": "manual",
    "draft": "drafted",
    "eval_promotion": "promoted from an experiment",
    "showcase-seed": "seeded for the showcase demo",
}


def origin_label(created_via):
    """Human label for a Workflow.created_via stamp, or None for NULL/legacy/unknown
    (the caller renders no origin chip). "draft" reads as "drafted"."""
    return ORIGIN_LABELS.get(created_via)


# User-facing role verbs by StepType (SPEC positioning: workflow/step vocabulary,
# never "agent"). Used only when a step has no authored name.
_TYPE_VERB = {
    StepType.LLM_AGENT: "Translate",
    StepType.LQA: "Check",
    StepType.HUMAN_REVIEW: "Review",
    StepType.TM_MATCH: "Match TM",
    # M5: an external step DISPATCHES to a vendor -- honest, no model-call vocabulary.
    StepType.EXTERNAL: "Send to vendor",
}


# --- defensive field access (ORM row OR draft dict) --------------------------
def _f(step, name, default=None):
    """Read one field from a WorkflowStep ORM row OR a plain draft dict."""
    if isinstance(step, dict):
        return step.get(name, default)
    return getattr(step, name, default)


def _branch_of(step):
    """The step's branch as a non-empty dict, else None (blank/absent/malformed)."""
    b = _f(step, "branch")
    return b if isinstance(b, dict) and b else None


def _key_positions(steps):
    """Map each step's stable `key` -> its list position (branch targets resolve here)."""
    pos = {}
    for i, s in enumerate(steps):
        k = _f(s, "key")
        if k:
            pos[str(k)] = i
    return pos


def _target_token(t):
    """Normalize a branch slot value to a hashable token, or None for unset/blank/junk.
    Tokens: "next" | "end" | ("step", "<key>")."""
    if t == "next":
        return "next"
    if t == "end":
        return "end"
    if isinstance(t, dict):
        k = t.get("step")
        if k:
            return ("step", str(k))
    return None


def _classify_target(token, i, pos):
    """Classify a target token relative to source position `i`:
    "forward" | "loopback" | "none". A `next`/`end` target is always forward; a
    `{step: k}` target loops back when its position is <= i, forward when later; a
    dangling key (not in `pos`) is "none" (treated as no edge — defensive)."""
    if token in ("next", "end"):
        return "forward"
    if isinstance(token, tuple) and token[0] == "step":
        k = token[1]
        if k not in pos:
            return "none"  # dangling / renamed -> no edge (SPEC: never raise)
        return "loopback" if pos[k] <= i else "forward"
    return "none"


class ShapeResult:
    """The classification of one workflow's steps: a raw tag `tags` (frozenset) plus a
    single display `primary` and the additive chips beyond it."""

    __slots__ = ("steps_len", "tags")

    def __init__(self, tags, steps_len):
        self.tags = frozenset(tags)
        self.steps_len = steps_len

    @property
    def primary(self) -> str:
        for t in _PRIMARY_PRIORITY:
            if t in self.tags:
                return t
        return STRAIGHT_THROUGH  # defensive: an empty/odd set still names a primary

    @property
    def additive(self) -> list:
        """Chips shown ALONGSIDE the primary (human-checkpoint). Fan-out is added by
        the display site, which alone knows the group/sibling count."""
        out = []
        if HUMAN_CHECKPOINT in self.tags:
            out.append(HUMAN_CHECKPOINT)
        # SPEC-parallel-routing §6: additive, after the human checkpoint.
        if PARALLEL_CHECKS in self.tags:
            out.append(PARALLEL_CHECKS)
        return out

    @property
    def ordered_tags(self) -> list:
        """Primary first, then additive chips — the stable render order for the UI."""
        return [self.primary] + self.additive  # noqa: RUF005

    @property
    def primary_label(self) -> str:
        return SHAPE_LABELS[self.primary]

    def sorted_tags(self) -> list:
        """All tags as a deterministic sorted list (for JSON/CLI parity)."""
        return sorted(self.tags)


def chip_descriptors(result, fanout=False) -> list:
    """UI chip descriptors for a ShapeResult: an ordered list of
    {slug, label, hint, primary} — the primary tag first, then additive chips
    (human-checkpoint), then an optional FAN-OUT chip (fan-out is a display-site fact
    the classifier can't see — the caller passes it from group/sibling count). Kept here
    so the table, builder, and CLI single-source the same labels."""
    out = []
    for slug in result.ordered_tags:
        out.append(
            {
                "slug": slug,
                "label": SHAPE_LABELS[slug],
                "hint": SHAPE_HINTS[slug],
                "primary": slug == result.primary,
            }
        )
    if fanout:
        out.append(
            {
                "slug": FAN_OUT,
                "label": SHAPE_LABELS[FAN_OUT],
                "hint": SHAPE_HINTS[FAN_OUT],
                "primary": False,
            }
        )
    return out


def detect_shapes(steps) -> ShapeResult:
    """Classify a workflow's ordered steps into a ShapeResult. Pure; never raises.

    `steps` is an ordered iterable of WorkflowStep ORM rows OR draft dicts. Fan-out is
    NOT computed here (it is not a step property — see module docstring)."""
    steps = list(steps or [])
    pos = _key_positions(steps)

    has_run_condition = any(_f(s, "run_condition") for s in steps)
    has_branch = any(_branch_of(s) for s in steps)
    has_human = any(_f(s, "step_type") == StepType.HUMAN_REVIEW for s in steps)
    # SPEC-parallel-routing §6: a PARALLEL GROUP is >= 2 CONSECUTIVE steps sharing a
    # slug -- the same maximal-run definition the engine forks on (`_group_at`), so the
    # chip can never claim parallelism the run will not perform. A LONE member is not a
    # group (it does not fork), so it earns no tag; a slug SPLIT by other steps is
    # refused at save time and, if one ever reached here, reads as two lone members.
    has_parallel = False
    for i in range(1, len(steps)):
        g = _f(steps[i], "parallel_group")
        if g and _f(steps[i - 1], "parallel_group") == g:
            has_parallel = True
            break

    loopback = False
    triage = False
    for i, s in enumerate(steps):
        b = _branch_of(s)
        if not b:
            continue
        op = _target_token(b.get("on_pass"))
        of = _target_token(b.get("on_fail"))
        cop = _classify_target(op, i, pos)
        cof = _classify_target(of, i, pos)
        if cop == "loopback" or cof == "loopback":
            loopback = True
        # Triage (approx): a gate whose on_pass/on_fail are BOTH set, BOTH forward, to
        # DIFFERENT targets (binary fork). A dangling/loop-back slot disqualifies it.
        if op is not None and of is not None and cop == "forward" and cof == "forward" and op != of:
            triage = True

    tags = set()
    if has_human:
        tags.add(HUMAN_CHECKPOINT)
    if has_parallel:
        tags.add(PARALLEL_CHECKS)
    if not has_run_condition and not has_branch:
        tags.add(STRAIGHT_THROUGH)
    else:
        if loopback:
            tags.add(CORRECTION_LOOP)
        if triage:
            tags.add(TRIAGE)
        # Gated: a guard/branch that GATES the flow but neither loops back nor is a
        # two-way triage fork (e.g. a run_condition, or an LQA gate that ends early on
        # pass). Kept off when a loop-back or triage fork already describes the branch,
        # so the tag set stays minimal (no redundant "gated + triage" noise).
        if (has_run_condition or has_branch) and not loopback and not triage:
            tags.add(GATED)
    return ShapeResult(tags, len(steps))


# --- headline (Design-review screen + CLI) -----------------------------------
def step_label(step) -> str:
    """A short human label for a step: its authored name, else a role verb by type."""
    name = (_f(step, "name") or "").strip()
    if name:
        return name
    return _TYPE_VERB.get(_f(step, "step_type"), "Step")


def _step_gate_label(step) -> str:
    """`step_label` annotated with its gate, when it produces one: a QE gate shows its
    threshold ("(gate at 90%)"); an LQA step shows "(LQA gate)" (LQA passes on zero
    critical/major errors — there is no numeric threshold to quote)."""
    label = step_label(step)
    if _f(step, "qe_gate"):
        thr = _f(step, "qe_threshold")
        if thr not in (None, ""):
            return f"{label} (gate at {thr}%)"
        return f"{label} (gate)"
    if _f(step, "step_type") == StepType.LQA:
        return f"{label} (LQA gate)"
    return label


def _loopback_target_label(steps):
    """The label of the earliest step a loop-back edge returns to (prefer on_fail —
    the correction-loop convention), or None if there is no loop-back."""
    pos = _key_positions(steps)
    for i, s in enumerate(steps):
        b = _branch_of(s)
        if not b:
            continue
        for slot in ("on_fail", "on_pass"):
            tok = _target_token(b.get(slot))
            if isinstance(tok, tuple) and tok[0] == "step":
                k = tok[1]
                if k in pos and pos[k] <= i:
                    return step_label(steps[pos[k]])
    return None


_HEADLINE_LEAD = {
    STRAIGHT_THROUGH: "This is a straight-through pipeline",
    GATED: "This is a gated pipeline",
    TRIAGE: "This routes by check outcome",
    CORRECTION_LOOP: "This is a correction loop",
}


def shape_headline(steps, *, arrow=" → ", n_locales=None) -> str:
    """A one-line, server-rendered summary of what a workflow does — e.g.
    "This is a correction loop: Translate -> LQA (LQA gate) -> Fix; on a failed check it
    loops back to Fix and re-checks." Generated from `detect_shapes` + the real step
    names/thresholds. Calm voice, never "agent".

    `arrow` is the between-steps separator (default the Unicode arrow for web;
    pass " -> " for ASCII CLI). `n_locales` is the number of TARGET locales in the draft;
    when > 1 the headline notes the fan-out as "(base + N-1) variants" — i.e. it counts
    the VARIANT workflows (n_locales - 1), NOT the raw locale count, so it agrees with the
    review screen's fan-out rows and confirm button (both count `targets-1`).
    QA-10 (#40): the old code emitted the raw `n_locales`, so a 3-language draft read
    "3 language variants" in the headline while the rows/button read "2" — the same family
    described with two different numbers."""
    steps = list(steps or [])
    result = detect_shapes(steps)
    lead = _HEADLINE_LEAD.get(result.primary, "This workflow")
    parts = [_step_gate_label(s) for s in steps]
    chain = arrow.join(parts) if parts else "no steps yet"
    text = f"{lead}: {chain}"
    if result.primary == CORRECTION_LOOP:
        tgt = _loopback_target_label(steps)
        if tgt:
            text += f"; on a failed check it loops back to {tgt} and re-checks"
    extras = []
    if HUMAN_CHECKPOINT in result.tags:
        extras.append("a human checkpoint")
    if n_locales and n_locales > 1:
        # QA-10 (#40): count VARIANT workflows (base + N-1), matching the fan-out rows +
        # confirm button (len(targets) - 1), never the raw locale count.
        extras.append(f"{n_locales - 1} language variants")
    if extras:
        text += " (with " + " and ".join(extras) + ")"
    return text
