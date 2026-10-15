"""workflows: part 2 (layered pure move)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports WorkflowForeignCustomerError,
_require_valid_lqa_threshold, _coerce_tm_tristate_bool, _require_valid_tm_scalar,
_require_valid_tm_pool_selection, _step_ref_key, parse_run_condition_str, _rc_ref_qualifies from
app.services.workflows_part2_leaf.

Documented in: docs/architecture/workflow-engine.md
"""

import re as _re
import threading
from typing import NamedTuple

from sqlmodel import Session, select

from app.models import (
    Project,
    PromptTemplate,
    Scope,
    StepType,
    TargetLocale,
    Workflow,
    WorkflowGroup,
    WorkflowStep,
)
from app.services import ids, prompts
from app.services.workflows_common import (
    _BRANCH_SENTINELS,
    _MAX_WAIT_HOURS,
    _RC_STEP_PREFIX,
    _TM_PROTECT_FALSE,
    _TM_PROTECT_TRUE,
    WorkflowError,
    _coerce_pt_id,
    _coerce_tm_pool_selection,
    _is_step_ref,
    _step_attr,
    slugify_key,
)
from app.services.workflows_part2_leaf import (
    WorkflowForeignCustomerError,
    _coerce_tm_tristate_bool,
    _rc_ref_qualifies,
    _require_valid_lqa_threshold,
    _require_valid_tm_pool_selection,
    _require_valid_tm_scalar,
    _step_ref_key,
    parse_run_condition_str,
)

__all__ = [
    "_RC_STEP_PREFIX",
    "_TM_PROTECT_FALSE",
    "_TM_PROTECT_TRUE",
    "WorkflowForeignCustomerError",
    "_coerce_tm_pool_selection",
    "_coerce_tm_tristate_bool",
    "_is_step_ref",
    "_rc_ref_qualifies",
    "_require_valid_lqa_threshold",
    "_require_valid_tm_pool_selection",
    "_require_valid_tm_scalar",
    "_step_ref_key",
    "parse_run_condition_str",
]


def _ordered(steps):
    """The steps sorted by order_index -- the spine every prefix-shaped predicate reads."""
    return sorted(steps, key=lambda s: _step_attr(s, "order_index", 0) or 0)


def _pgroup(s):
    """A step's parallel-group slug, normalised so '' and None are the same 'no group'."""
    return _step_attr(s, "parallel_group") or None


def _existing_keys(prior_steps) -> set:
    """The set of non-blank keys already used by the workflow's OTHER steps."""
    return {k for k in (_step_attr(s, "key") for s in prior_steps) if k}


PARALLEL_PHASE1_TYPES = (StepType.LQA,)


# --- parallel step routing (SPEC-parallel-routing §5.1) ----------------------
# Save-time validate-and-sanitize for a step's `parallel_group`, plus the whole-graph
# MEMBERSHIP rules. Same posture as the branch graph: fail CLOSED at save time, data
# only, never eval'd. The slug is authored exactly like `key` (slugify_key) but is
# deliberately NOT unique -- sharing it is what makes two steps one group.
def validate_parallel_group(value):
    """Slugify a step's parallel-group value. Returns the clean slug, or None for a
    blank/absent value (not in a group -- byte-identical to today). Raises WorkflowError
    on a non-blank value that slugifies to nothing, so a typo'd slug is surfaced rather
    than silently dropping the step out of its group (which would flatten the lanes with
    no error -- the exact failure mode this whole feature has to avoid)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    slug = slugify_key(raw)
    if not slug:
        raise WorkflowError(
            f"parallel group {value!r} is not a valid slug (use letters, digits, '-' or '_')."
        )
    return slug


def _step_label(s):
    """The human handle an error names a step by: its key, else its name, else '#id'.

    For DISPLAY only -- it is the second element of an error triple, which the
    before/after diff does not key on. Anything embedded in the MESSAGE must use
    `_stable_ref` instead (see there)."""
    sid = _step_attr(s, "id")
    return _step_attr(s, "key") or _step_attr(s, "name") or (f"#{sid}" if sid is not None else "?")


def _stable_ref(s):
    """The handle a message that PARTICIPATES IN THE BEFORE/AFTER DIFF may name a step
    by: its key, else '#id'. Deliberately NOT the name.

    The diff keys on (step_id, message), so any part of a message that can change for a
    reason unrelated to the edit makes a PRE-EXISTING error read as NEW and blocks an
    innocent edit. `_step_label` falls back to the step's NAME, so renaming a KEYLESS
    group member would have done exactly that to the rule-5 branch message (adversarial
    review, residual 3). A key is stable by construction (it is the graph's addressing
    scheme) and an id is stable absolutely, so neither can drift under a rename."""
    sid = _step_attr(s, "id")
    return _step_attr(s, "key") or (f"#{sid}" if sid is not None else "?")


_SINGLETON_GROUP_IS_INERT = True


# WHY A SINGLETON GROUP IS INERT RATHER THAN REFUSED (documented deviation from
# SPEC-parallel-routing §5.1.1, which says "a group has >= 2 members ... a singleton
# group is refused"). Every authoring surface in this repo edits ONE STEP AT A TIME
# (add-step appends; the builder's per-step form and `workflow edit-step` update a
# single row). Under a hard >= 2 rule the FIRST step to join a group is always a
# singleton, so it is always refused -- and there is no order of operations that
# escapes it: the second member can never be added, and grouping two EXISTING steps by
# editing them is refused on the first edit too. The rule as written is unauthorable.
#
# The semantics are preserved exactly, because the spec's own rationale for refusing a
# singleton is "it's just a step": `orchestrator._group_at` only forks a run of TWO OR
# MORE members, so a lone member executes down the ordinary single-step path,
# byte-identically. The state is therefore transient-and-harmless rather than silent:
# `workflow show` and the builder's step-head chip both render the slug, so a mistyped
# slug (the real hazard -- two intended lanes that quietly became two singletons) is
# visible on the surfaces an author is already looking at.
def parse_branch_str(target: str):
    """Parse a compact CLI branch target into the stored form:
      'next'/'end'      -> the sentinel string
      '<key>'           -> {'step': '<slug>'}   (jump to that step; earlier = loop-back)
    Blank -> None (no target for this slot)."""
    t = (target or "").strip()
    if not t:
        return None
    if t in _BRANCH_SENTINELS:
        return t
    return {"step": slugify_key(t)}


def _validate_branch_target(target, keys):
    """Normalize + resolve ONE branch target against the workflow's key set. Returns
    'next' | 'end' | {'step': '<key>'}. Raises WorkflowError on a malformed shape or an
    unknown/blank step key (fail closed). None -> 'next' (fall through)."""
    if target is None:
        return "next"
    if isinstance(target, str):
        t = target.strip()
        if not t:
            return "next"
        if t in _BRANCH_SENTINELS:
            return t
        target = {"step": t}  # bare-key shorthand
    if not isinstance(target, dict):
        raise WorkflowError(
            f"branch target {target!r} is malformed (use 'next', 'end', or {{'step': '<key>'}})."
        )
    extra = set(target) - {"step"}
    if extra or "step" not in target:
        raise WorkflowError(
            f"branch target {target!r} is malformed (only {{'step': '<key>'}} is allowed)."
        )
    key = slugify_key(target.get("step"))
    if not key:
        raise WorkflowError("a branch target step key must not be blank.")
    if key not in keys:
        raise WorkflowError(
            f"branch target step {key!r} does not exist in this workflow -- add (and key) "
            "that step first, or target 'next'/'end'."
        )
    return {"step": key}


def _coerce_wait_hours(v):
    """'' / None -> None (inherit the install TTL); a non-negative, in-range int -> int;
    a negative / non-numeric / absurdly-large value -> None (M6: never a raw OverflowError
    at commit). The external step's per-step bounded-wait override."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= _MAX_WAIT_HOURS else None


def _validate_step_vendor(session: Session, workflow_id: int, vendor_id) -> None:
    """Fail-closed vendor validation for an EXTERNAL step at SAVE time. The vendor is
    REQUIRED, must be selectable (exists, not archived), and -- when the workflow is
    customer-scoped -- must be LINKED to that customer. Raises WorkflowError (changing
    nothing) otherwise. The run-time branch (vendor_for_customer) re-checks the link
    against the document's actual customer, so a global workflow's external step is
    validated per-run there; this save-time guard catches the obvious breakage early.

    CROSS-ORG (2026-08-08): the same-org gate is dropped ONLY for a CUSTOMER-scoped
    workflow, where a vendor linked to that customer may live in another org (linking is
    unrestricted across orgs) and must be attachable — the customer LINK is the meaningful
    save-time gate there. A GLOBAL workflow has no fixed customer, so the vendor is still
    resolved IN THE WORKFLOW'S ORG (`org_id=wf.org_id`) to close a vendor-existence oracle
    (a global step cannot probe foreign vendor ids). The run-time `vendor_for_customer`
    is the ultimate fail-closed authorization regardless."""
    # ffadmin Wave 3 (MEDIUM-2): the ONE save-time seam both add_step and update_step
    # (Guard 6) funnel through when a step is (or is becoming) external and its vendor /
    # type is touched. When the Vendors feature is off, refuse to AUTHOR (create or edit)
    # a step that references a vendor -- naming the switch. EXISTING external step
    # definitions are untouched (data preserved; they run again on re-enable): editing a
    # non-vendor field of an existing external step never reaches here (Guard 6 fires only
    # when vendor_id / step_type is supplied).
    from app import features as _features
    from app.services import vendors as _vendors

    if not _features.is_enabled("vendors"):
        raise WorkflowError(
            "vendors are disabled (setting vendors_enabled=0); an external (vendor) step "
            "cannot be added or edited while off."
        )
    if not vendor_id:
        raise WorkflowError(
            "an external step requires a vendor -- select the vendor to dispatch to."
        )
    wf = session.get(Workflow, workflow_id)
    cust_id = getattr(wf, "customer_id", None)
    is_customer_scoped = getattr(wf, "scope", None) == Scope.CUSTOMER and cust_id is not None
    # Customer-scoped: org-agnostic (the LINK below is the gate). Global: ring by wf.org.
    # #73: a customer-scoped step resolves the vendor cross-org via access.UNSCOPED (the
    # DELIBERATE "every org" scope) -- a bare None now fails closed in selectable/visible
    # under the gate. The global branch still rings by the workflow's own (row) org.
    from app.services import access

    v = _vendors.selectable(
        session,
        vendor_id,
        org_id=access.UNSCOPED if is_customer_scoped else getattr(wf, "org_id", None),
    )
    if v is None:
        raise WorkflowError(f"vendor {vendor_id} is not available (unknown or archived).")
    # A CUSTOMER-scoped workflow only runs for that one customer, so require the link now.
    if is_customer_scoped:  # noqa: SIM102
        if not _vendors.is_linked(session, v.id, cust_id):
            raise WorkflowError(
                f"vendor {vendor_id} is not connected to this workflow's customer -- link "
                "the vendor to the customer first (Vendors panel), then select it."
            )


def validate_bound_template(session: Session, step_type: str, prompt_template_id) -> int | None:
    """Resolve + validate a step's prompt_template_id at BIND time (add/edit step).
    Returns the coerced id (None when unset/blank -> no binding). Fail-closed with a
    WorkflowError, changing nothing, when a NON-BLANK id:
      * names no existing template (a dangling bind is refused up front rather than
        silently falling back at run time); or
      * (llm_agent only) names a template whose non-empty body would FAIL the same
        source-placeholder check inline prompt_text must pass -- so a bound template can
        never send the model no source (prompts.validate_step_prompt, the shared rule).

    Only enforced when the id could actually take effect on an llm_agent step; lqa/
    human_review are handled upstream by refuse_dead_fields (prompt_template_id is DEAD
    there), so a meaningful id is already refused before this is reached. A blank body
    is allowed (it resolves to the default at run time, exactly like a blank inline
    prompt -- validate_step_prompt is a no-op on empty). BIND-time is the enforcement
    point: a PromptTemplate's body is immutable after creation (there is no body-edit
    surface -- create/rename/delete only), so a validly-bound template can never later
    be edited into an invalid one, and delete is reference-count-guarded, so no bound
    step is ever silently orphaned."""
    pt_id = _coerce_pt_id(prompt_template_id)
    if pt_id is None or step_type != StepType.LLM_AGENT:
        return pt_id
    tmpl = session.get(PromptTemplate, pt_id)
    if tmpl is None:
        raise WorkflowError(
            f"prompt template {pt_id} not found -- pick an existing template, or leave "
            "the binding empty."
        )
    try:
        prompts.validate_step_prompt(step_type, tmpl.body or "")
    except prompts.PromptValidationError as exc:
        raise WorkflowError(
            f"prompt template {pt_id} ({tmpl.name!r}) cannot be bound to this step: {exc}"
        ) from exc
    return pt_id


def resolve_step_ref(rest, ref):
    """Index of `ref` (a step KEY or a numeric step id) within `rest` -- the ordered step
    list WITHOUT the step being moved -- so `move-step --before/--after` (CLI) and the
    `/api/.../steps/{sid}/move` `before`/`after` body resolve IDENTICALLY (house parity
    law). `rest` is the right list because --before/--after describe the FINAL
    arrangement: the moved step is gone from its old slot, so 'before X' is X's index in
    the remainder.

    AMBIGUITY IS REFUSED, NOT RESOLVED. A numeric ref can match one step's KEY and a
    DIFFERENT step's ID (keys are free-form slugs, so '3' is a legal key). Silently
    letting the key win would move the step somewhere the author did not name, with no
    way to tell from the output -- so this raises WorkflowError instead. Returns the
    index, or None when nothing matches (the caller turns that into its own not-found
    refusal)."""
    ref = (ref or "").strip()
    # coerce_id never raises on a Unicode digit / >2**63 ref -- an unparseable ref simply
    # matches no step by id (falls to the key match), never a traceback at int(ref).
    ref_id = ids.coerce_id(ref)
    by_key = [i for i, s in enumerate(rest) if _step_attr(s, "key") and _step_attr(s, "key") == ref]
    by_id = [i for i, s in enumerate(rest) if ref_id is not None and _step_attr(s, "id") == ref_id]
    if by_key and by_id and by_key[0] != by_id[0]:
        raise WorkflowError(
            "%r is ambiguous: it is the KEY of step #%d and the ID of step #%d. "  # noqa: UP031
            "Use an explicit position, or rename one of them."
            % (ref, _step_attr(rest[by_key[0]], "id"), _step_attr(rest[by_id[0]], "id"))
        )
    if by_key:
        return by_key[0]
    if by_id:
        return by_id[0]
    return None


# --- reorder a step (SPEC-workflow-canvas §4.1, the reorder seam) ---------------
def _graph_row(s, order_index) -> dict:
    """A step rendered as the plain dict `_graph_edit_errors` reads (via `_step_attr`),
    at a CANDIDATE order_index. The field set is exactly the one the checker inspects
    (`_GRAPH_FIELDS`), so a candidate list is judged by the same rules as real rows."""
    return {
        "id": _step_attr(s, "id"),
        "order_index": order_index,
        "name": _step_attr(s, "name"),
        "step_type": _step_attr(s, "step_type"),
        "qe_gate": _step_attr(s, "qe_gate", False),
        "key": _step_attr(s, "key"),
        "run_condition": _step_attr(s, "run_condition"),
        "branch": _step_attr(s, "branch"),
        "parallel_group": _step_attr(s, "parallel_group"),
    }


_UNSET = object()


_MOVE_LOCK = threading.Lock()


# The sentinel that distinguishes "not supplied -> leave unchanged" from an explicit
# value (including None / "" which are meaningful edits). Defined HERE rather than beside
# update_step because reorder_step (below) needs it as a default too.
# Serializes the whole read-guards-then-write of a move so it is ATOMIC across every
# request thread in this process. Without it two concurrent moves are a TOCTOU: both read
# the same `all_steps`, both compute a full 0..n-1 renumbering from that same snapshot,
# and the second's write silently discards the first's committed move (the review
# reproduced exactly this against the old compensating-write design, where a rollback
# reverted a DIFFERENT request's committed move). Held only around the brief
# select+validate+update+commit. IN-PROCESS ONLY -- a second OS process (a CLI move while
# the server is live) is not serialized by this lock; closing that residual window needs
# DB-level row locking / optimistic versioning, and is documented here rather than closed,
# exactly as orchestrator._RUN_CREATE_LOCK documents its own limit.
def _tm_first_note(before_first, before_first_index, after_first):
    """The honest, human sentence for a move that changed WHO IS TM-FIRST ELIGIBLE --
    or None when nothing worth saying changed.

    The TM-first short-circuit fires on `order_index == 0` and only means anything for a
    TRANSLATE step (a check at index 0 has nothing to check). So eligibility is not "which
    step is first in the list", it is "a translate step whose order_index is ACTUALLY 0",
    and this compares that property before and after.

    THE DISTINCTION IS NOT PEDANTIC -- IT IS THE OTHER HALF OF THE GAP-HEAL FINDING.
    `delete_step` leaves holes, so a workflow can sit at order_index 1,2,3 with NOTHING at
    0 and TM-first therefore OFF. A move renumbers contiguously, which slides the first
    step down to 0 and ARMS TM-first on a step the user never touched. Comparing which
    STEP occupies the head of the list misses that completely (it is the same step both
    times) and the note stayed silent on a real behaviour change. Hence
    `before_first_index`, which the caller must capture BEFORE the renumbering loop -- by
    the time the note is built the ORM object has been refreshed and its order_index
    already reads 0, so the pre-move value cannot be recovered from it.

    After the move the head of the list is at index 0 by construction (renumbering is
    contiguous 0..n-1), so "eligible after" is simply "the new head is a translate step".

    Built in the SEAM so the web notice and the CLI line say the same thing (parity by
    construction)."""

    def _is_mt(s):
        return s is not None and _step_attr(s, "step_type") == StepType.LLM_AGENT

    def _name(s):
        # NAME first here, unlike `_step_label`/`_stable_ref`. Those feed the before/after
        # diff, where a name is unusable because it can drift under an unrelated rename;
        # this string is pure prose for a human, and "Translate" reads better than "t".
        return _step_attr(s, "name") or _step_attr(s, "key") or ("#{}".format(_step_attr(s, "id")))

    # WHO WAS eligible: the head of the list, but only if it was REALLY at index 0.
    was = before_first if (_is_mt(before_first) and (before_first_index or 0) == 0) else None
    # WHO IS eligible now: the new head, which renumbering just put at index 0.
    now = after_first if _is_mt(after_first) else None
    if was is not None and now is not None:
        if _step_attr(was, "id") == _step_attr(now, "id"):
            return None  # same step, still eligible -- nothing to say
        return (
            f"TM pre-fill eligibility moved from {_name(was)!r} to {_name(now)!r} -- a segment with a 100% "  # noqa: E501
            "translation-memory match now skips the model there instead."
        )
    if now is not None:
        return (
            f"TM pre-fill eligibility moved to {_name(now)!r} -- a segment with a 100% "
            "translation-memory match can now skip the model there."
        )
    if was is not None:
        return f"TM pre-fill eligibility moved away from {_name(was)!r} -- it now always calls the model."  # noqa: E501
    return None


_GRAPH_FIELDS = (
    "order_index",
    "id",
    "key",
    "name",
    "step_type",
    "qe_gate",
    "run_condition",
    "branch",
    "parallel_group",
)


_GRAPH_SHAPING_FIELDS = ("key", "step_type", "qe_gate", "run_condition", "branch", "parallel_group")


# --- shared-base + override-delta propagation (SPEC-workflows-redesign 4b) -------
# Editing a BASE step sweeps the change onto every non-pinned VARIANT step cloned
# from it, so a 16-locale multilingual workflow is maintainable instead of 16 hand-
# synced copies. "Base" is EMERGENT (reference-based, no new concept): a step B is a
# base of variant step V iff V.adapted_from_id == B.id. Propagation walks exactly
# those V. This makes the DEFAULT/base workflow's steps the bases (variants point at
# them; a variant's own steps point back), with no is_default coupling.
#
# The sacred don't-clobber invariant is enforced by TWO independent keys (see
# WorkflowStep.overridden): a variant step is written ONLY when (1) adapted_from_id ==
# the edited base step id (a live provenance link) AND (2) overridden is False. A
# LEGACY variant has adapted_from_id NULL so it fails (1) and is NEVER touched — no
# backfill, conservative by construction.
#
# SCOPE: field EDITS only. Structural edits (add/delete steps) are NOT propagated —
# they stay per-workflow (a variant may legitimately have extra/fewer steps). This is
# documented as out-of-scope; only the fields in _EDIT_FIELDS sweep.
#
# The fields _graph_edit_errors inspects, so a candidate variant row can be validated.
# SPEC-parallel-routing §5.1: membership is a graph property (it decides
# what "prior" means for a guard and where a branch may re-enter), so a
# propagated parallel_group must be re-validated per variant like the rest.
# The fields that actually CHANGE THE SHAPE of a run -- what executes, in what order,
# under what guard. A strict subset of _GRAPH_FIELDS, which is deliberately WIDER: that
# tuple is the candidate-row field list `_graph_edit_errors` reads, so it also carries
# `id` and `order_index` (row identity, never edited) and `name` (used only to LABEL an
# error). Using _GRAPH_FIELDS as the run-safety trigger therefore made a plain RENAME
# look like a graph change and skipped propagation to any variant with a run in flight
# (adversarial review, residual 1) -- a false refusal on the most innocuous edit there
# is. Renaming a step cannot affect a run: nothing routes by name.
def _variant_report_entry(vwf, step) -> dict:
    """A stable, render-friendly descriptor of a variant a base edit affected — the
    unit the web notice + CLI report + /api response all list."""
    return {
        "workflow_id": vwf.id,
        "name": vwf.name,
        "step_id": step.id,
        "label": _pair(vwf.source_locale, vwf.target_locale),
    }


def set_step_override(session: Session, step_id: int, overridden: bool) -> WorkflowStep:
    """Pin (overridden=True) or unpin (False) a VARIANT step — the DELIBERATE act behind
    the builder's pin/unpin control and the CLI `edit-step --pin/--unpin` flags.

    Pinning freezes the step against base-edit propagation. UNPINNING rejoins propagation
    from the NEXT base edit onward — it does NOT retro-sync the step to the current base
    (no silent catch-up; a deliberate diverged value stays until the next base edit
    overwrites its changed fields). Raises WorkflowError on an unknown id.

    Unlike a field edit (update_step), this does NOT block on an in-flight run: it flips a
    metadata flag only, never the step's run-time config, so it is safe mid-flight. No
    propagation runs (a pin/unpin is not a content change). No-op-safe (same value = inert)."""
    step = session.get(WorkflowStep, step_id)
    if step is None:
        raise WorkflowError(f"workflow step {step_id} not found.")
    step.overridden = bool(overridden)
    session.add(step)
    session.commit()
    session.refresh(step)
    return step


# --- workflow rename + delete (SPEC-rename-delete, Branch 1) -----------------
def rename_workflow(session: Session, workflow_id: int, name: str) -> Workflow:
    """Rename a workflow. Workflow names are legally NON-unique (spec: no new
    uniqueness rule), so there is no dup-check — the rename always succeeds for a
    non-blank name. All cross-entity references are by id (WorkflowStep.workflow_id,
    WorkflowRun.workflow_id, RoutingRule.workflow_id, Customer.default_workflow_id,
    Workflow.group_id) so a rename propagates automatically and nothing disconnects.

    Raises WorkflowError on an unknown id or a blank name. The CALLER logs the audit
    old->new entry (it holds the pre-fetched row for its 404 gate)."""
    wf = session.get(Workflow, workflow_id)
    if wf is None:
        raise WorkflowError(f"workflow {workflow_id} not found.")
    name = (name or "").strip()
    if not name:
        raise WorkflowError("workflow name must not be blank.")
    wf.name = name
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return wf


# --- archive / unarchive (residual sweep: archive verbs; Customer.archived_at precedent) --
class WorkflowArchiveError(WorkflowError):
    """An archive/unarchive was a no-op-with-a-reason (already archived / not archived).
    Subclasses WorkflowError so existing except-handlers still catch it, but is a distinct
    type so a surface can render a precise 400/EXIT_USAGE message."""


class WorkflowArchivedError(WorkflowError):
    """A NEW run/dispatch was refused because its workflow is archived. Archive blocks
    LAUNCHING new work (an archived workflow is excluded from dispatch/variant resolution
    and cannot be re-run) but never kills an in-flight run. Surfaced as a clean refusal."""


class WorkflowCustomerMismatchError(WorkflowArchivedError):
    """A NEW run was refused (#542 item 3) because its workflow belongs to a DIFFERENT
    customer than the document. Allowed: a global workflow (customer_id NULL) or one of the
    document's own customer. Subclasses WorkflowArchivedError ONLY so every launch surface's
    existing launch-refusal handler (400 / usage error / MCP error) renders it with no
    per-route change; the message names the real cause."""


def prompt_template_reference_count(session: Session, template_id: int, workflow_ids=None) -> dict:
    """How many workflow steps pin this template (the only referrer:
    WorkflowStep.prompt_template_id, a real FK). Never mutates.

    `workflow_ids` (optional): when given, count ONLY steps whose workflow is in this
    set — used by the Wave C cross-links to show a RING-CONFINED viewer a count over the
    workflows they can actually see (never cross-customer volume). An empty set counts
    zero; None (the default, the delete-guard caller) counts every referrer."""
    stmt = select(WorkflowStep.id).where(WorkflowStep.prompt_template_id == template_id)
    if workflow_ids is not None:
        ids = list(workflow_ids)
        stmt = stmt.where(WorkflowStep.workflow_id.in_(ids)) if ids else stmt.where(False)
    steps = len(session.exec(stmt).all())
    return {"steps": steps, "blockers": steps, "total": steps}


def prompt_template_bound_workflow_ids(session: Session, template_id: int) -> list[int]:
    """Distinct workflow ids whose steps pin this template (SPEC Workflow Studio Wave C
    cross-links). The referrer is WorkflowStep.prompt_template_id -> WorkflowStep.
    workflow_id; a workflow with two pinning steps counts once. Never mutates. The CALLER
    is responsible for intersecting these ids with the viewer's VISIBLE workflows before
    showing names (so a foreign customer's workflow name never leaks) — this seam returns
    the raw truth, mirroring prompt_template_reference_count's honesty."""
    wf_ids = session.exec(
        select(WorkflowStep.workflow_id).where(WorkflowStep.prompt_template_id == template_id)
    ).all()
    return sorted(set(wf_ids))


# --- multilingual workflow groups (P5, item 4) -------------------------------
#
# A WorkflowGroup holds a family of per-language-pair Workflow variants. The
# "copy to language pair(s)" action clones a source variant onto other pairs,
# optionally LLM-adapting each step's free-text instruction from the source pair
# to the target pair (es "usted" -> fr "vous"). Dispatch (resolve_variant) picks
# the right variant for a run's target locale, falling back to the group default.
#
# The adaptation call goes through orchestrator's provider seam via run_completion
# (the SAME path every LLM task uses) so the spend cap + MockProvider apply — in
# tests, keys are blanked, so it resolves to the deterministic mock and costs
# nothing while still exercising the rewrite plumbing.


def _pair(src: str | None, tgt: str | None) -> str:
    """A display key for a variant's pair ('src->tgt', or 'universal' when both
    sides are unset — a group-default/universal variant)."""
    if not src and not tgt:
        return "universal"
    return f"{src or '*'}->{tgt or '*'}"


_PAIR_SUFFIX_ONE_RE = _re.compile(r"\s*\[\s*([^\[\]]*?)\s*\]\s*$")


_LOC_ATOM_RE = _re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")


# A trailing pair suffix as `pair_name` writes it: " [en-US->fr-FR]" / " [universal]".
# Anchored at end-of-string, no nested brackets. The inner text is then checked against
# _is_pair_suffix_body, NOT just "contains an arrow": adversarial review (R6/R7) showed
# an arrow test eats legitimate names -- "MT pipeline [MT->PE]" and "Report [2024->2025]"
# both lost their bracketed suffix. A suffix counts only when BOTH sides of the arrow are
# real language codes (the languages_catalog authority, the same one every locale ENTERS
# through) or the wildcard `*` _pair writes for a half-set pair.
# Cheap shape gate before the catalog lookup: a BCP-47-ish `ll` / `ll-RR` / `ll-Xxxx-RR`.
def _is_pair_atom(atom: str) -> bool:
    """One side of a `[src->tgt]` suffix: the `*` wildcard `_pair` writes for an unset
    side, or a code the language catalog knows. Catalog-checked (not merely shaped) so
    `[MT->PE]` — shaped like a pair, but PE is no language — is left alone."""
    from app.services import languages_catalog

    atom = (atom or "").strip()
    if atom == "*":
        return True
    if not _LOC_ATOM_RE.match(atom):
        return False
    return languages_catalog.is_valid(atom)


def _same_pair(a_src, a_tgt, b_src, b_tgt) -> bool:
    """Locale-normalized pair equality (es_ES / ES-es / es-ES all compare equal), used by
    the copy_to_pairs duplicate guard so a re-copy spelled differently is still caught."""
    from app.services import languages_catalog

    return languages_catalog.normalize(a_src or "") == languages_catalog.normalize(
        b_src or ""
    ) and languages_catalog.normalize(a_tgt or "") == languages_catalog.normalize(b_tgt or "")


def variant_visible(user, v) -> bool:
    """Can `user` (a User row, or None) see this group member?

    N1 (re-review): the ONE definition of a variant's visibility, shared by the web
    `_visible_group_variants`, the coverage readout, the covered-targets picker set and
    the copy duplicate guard — four surfaces that were each deriving (or ignoring) it
    separately. It is the SAME pair of access predicates a workflow reached by id goes
    through (`_wf_or_404`): the org gate, then the customer ring, where a GLOBAL
    (customer_id NULL) variant stays visible to everyone.

    `user=None` means NO confinement and is the default everywhere, so the CLI, the
    draft fan-out and every auth-off install are byte-identical to before."""
    from app.services import access

    if user is None:
        return True
    return access.same_org(user, getattr(v, "org_id", None)) and not access.customer_ring_blocks(
        access.current_customer_id(user), getattr(v, "customer_id", None)
    )


def create_group(
    session: Session,
    name: str,
    customer_id=None,
    description: str = "",
    org_id=None,
    created_by: str = "",
    bound_customer_id=None,
) -> WorkflowGroup | None:
    """Create a workflow group. Returns the new WorkflowGroup, or None when the name
    is blank (routes/CLI no-op on blank input, mirroring create_workflow).

    `customer_id` NULL = a GLOBAL group (offered everywhere); an int = a CUSTOMER
    group (offered only on that customer's projects, per P4 scoping). `org_id`
    stamps the creator's org (multi-org leak guard); None leaves it NULL (gate off,
    byte-identical single-tenant).

    `bound_customer_id` (#468) is the caller's own customer binding when the caller is
    a customer-bound account (None = staff / trusted CLI, unchanged): a bound caller's
    group is always its own customer's (blank -> its own; another customer ->
    WorkflowForeignCustomerError), never a global group every customer shares."""
    name = (name or "").strip()
    if not name:
        return None
    cid = ids.coerce_id(customer_id)
    bound = ids.coerce_id(bound_customer_id)
    if bound is not None:
        if cid is not None and cid != bound:
            raise WorkflowForeignCustomerError(cid)
        cid = bound
    if cid is not None:
        # #488 item 2: the group's customer must exist and, when an org is given (multi-org
        # on), live in that org. A nonexistent id used to reach the FK as a 500; a customer
        # with a concrete other org was bound without a check. Same trust split as
        # create_workflow: a NULL-org customer is the caller's quarantine job (the web route
        # does it with access.same_org), so only a concrete different org is refused here.
        from app.models import Customer

        cust = session.get(Customer, cid)
        if cust is None:
            raise WorkflowForeignCustomerError(cid)
        cust_org = getattr(cust, "org_id", None)
        from app.services import access as _access

        scope = _access.org_scope_value(org_id)  # None / UNSCOPED: no org to narrow to
        if scope is not None and cust_org is not None and cust_org != scope:
            raise WorkflowForeignCustomerError(cid)
    grp = WorkflowGroup(
        name=name,
        customer_id=cid,
        description=(description or "").strip(),
        org_id=org_id,
        created_by=created_by or "",
    )
    session.add(grp)
    session.commit()
    session.refresh(grp)
    return grp


def group_reference_count(session: Session, group_id: int) -> dict:
    """How many workflow variants still belong to this group (Workflow.group_id).
    A group delete FAILS CLOSED while it has members — ungrouping a workflow is the
    user's explicit action, never a delete side effect. Never mutates."""
    members = len(session.exec(select(Workflow.id).where(Workflow.group_id == group_id)).all())
    return {"members": members, "blockers": members, "total": members}


def list_groups(session: Session, org_id=None) -> list[WorkflowGroup]:
    """All workflow groups, newest first. `org_id` (an int) scopes to one org for the
    SaaS/multi-org path; None returns every group (single-tenant, byte-identical)."""
    stmt = select(WorkflowGroup).order_by(WorkflowGroup.created_at.desc())
    if org_id is not None:
        stmt = (
            select(WorkflowGroup)
            .where(WorkflowGroup.org_id == org_id)
            .order_by(WorkflowGroup.created_at.desc())
        )
    return list(session.exec(stmt).all())


def group_variants(session: Session, group_id: int) -> list[Workflow]:
    """The Workflow variants belonging to a group, ordered by pair then id."""
    rows = session.exec(select(Workflow).where(Workflow.group_id == group_id)).all()
    return sorted(rows, key=lambda w: (w.source_locale or "", w.target_locale or "", w.id or 0))


def set_default_variant(session: Session, workflow_id: int) -> Workflow | None:
    """Mark a grouped workflow as its group's default (clearing the flag on the
    group's other variants). Returns the updated Workflow, or None if it is missing
    or ungrouped."""
    wf = session.get(Workflow, workflow_id)
    if wf is None or wf.group_id is None:
        return None
    for sib in session.exec(select(Workflow).where(Workflow.group_id == wf.group_id)).all():
        if sib.id != wf.id and sib.is_default:
            sib.is_default = False
            session.add(sib)
    wf.is_default = True
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return wf


_ADAPT_PLACEHOLDERS = (
    "{text}",
    "{prior}",
    "{source}",
    "{source_locale}",
    "{target_locale}",
    "{glossary}",
)


# --- LLM adaptation of a step's cross-language instruction --------------------
# The differentiator (research_gapfill GAP 1): rewrite a step's free-text
# instruction from the source language pair to the target pair, keeping structure +
# placeholders intact. ONE run_completion call per step, using the step's OWN
# provider spec (so a step's engine choice carries over; mock in tests). Placeholders
# ({text}/{source}/{glossary}/…) must survive verbatim so the adapted prompt still
# renders in the orchestrator.
def _adaptation_prompt(instruction: str, src_pair: str, tgt_pair: str) -> str:
    """The tight rewrite prompt. Names BOTH language pairs explicitly, orders the
    model to keep the structure + every {placeholder} intact, and to return ONLY the
    rewritten instruction (no preamble). Kept deterministic-friendly so the mock's
    generic echo path is harmless (the failure mode falls back to the original)."""
    return (
        "You are adapting one step of a localization workflow from one language "
        f"pair to another. The ORIGINAL instruction targets the language pair "
        f"{src_pair}. Rewrite it so it targets {tgt_pair} instead: translate any "
        "language-specific directives to the new language's conventions (for "
        "example a Spanish 'use the formal usted' becomes a French 'use the formal "
        "vous'), and adjust culture/locale-specific guidance to make sense for the "
        "new target. Keep the overall structure and intent. CRITICAL: preserve every "
        "placeholder token exactly as written and in place — {text}, {source}, "
        "{source_locale}, {target_locale}, {glossary}, {prior} — do NOT translate, "
        "rename, add, or drop them. Return ONLY the rewritten instruction text, with "
        "no preamble, quotes, or explanation.\n\n"
        f"ORIGINAL INSTRUCTION ({src_pair}):\n{instruction}"
    )


class _RacedDuplicatePair(Exception):
    """Internal signal: the in-SAVEPOINT re-check found a rival variant for this pair.
    Raised only to unwind `session.begin_nested()` (which rolls the SAVEPOINT back on any
    exception), caught immediately by copy_to_pairs, and never seen by a caller."""


# --- dispatch: pick the variant for a run's target locale --------------------
class VariantMatch(NamedTuple):
    """The outcome of matching a target locale against a group's variants — the SINGLE
    source of truth shared by `resolve_variant` (which returns `.workflow_id`) and the
    group page's coverage readout (which reads `.rung`/`.kind`/`.variant`). Deriving both
    from this one helper keeps the coverage display faithful to dispatch: the readout must
    feed the SAME (target, source) a real run passes (coverage does — it classifies each
    project's real (source, target) pair, see group_locale_coverage), since a rung-2 match
    is source-sensitive and a coverage row simulated at the wrong source WOULD mislead."""

    rung: int  # 2 exact | 3 default/universal | 4 fallback-to-requested
    kind: str  # "exact" | "default" | "universal" | "requested"
    workflow_id: int  # the id that would actually run
    variant: Workflow | None  # the matched sibling (None on rung 4)


# --- coverage: which of a scope's (source, target) pairs this group covers ---
def group_scope_pairs(
    session: Session, grp: WorkflowGroup, viewer_customer_id: int | None = None
) -> list[tuple[str, str]]:
    """Distinct (project source_locale, target_locale) PAIRS across the projects in a
    group's SCOPE — exactly the (source, target) inputs a real run of those projects
    hands to `resolve_variant`. De-duplicated by (normalized source, normalized target),
    first-seen order preserved. Pure read; no schema.

    SCOPE:
      - a customer-owned group looks at that customer's projects;
      - a GLOBAL group (customer_id NULL) looks at its org's projects (all projects when
        single-tenant / org_id NULL) — EXCEPT when the VIEWER is a customer-bound account
        (`viewer_customer_id` given): a bound viewer only ever sees their OWN customer's
        projects, so a global group's Coverage panel never aggregates other customers'
        locales for them (adversarial-review finding 2 — cross-customer exposure). An
        unbound viewer / internal staff (`viewer_customer_id` None) keeps the org-wide view.

    Pairs (not bare target locales) because dispatch is source-sensitive: the SAME target
    can resolve to different variants depending on the project's source, so the coverage
    readout must classify each real (source, target) a scope run would actually dispatch."""
    from app.services import languages_catalog

    q = select(Project.source_locale, TargetLocale.locale).join(
        Project, TargetLocale.project_id == Project.id
    )
    if grp.customer_id is not None:
        q = q.where(Project.customer_id == grp.customer_id)
    elif viewer_customer_id is not None:
        # GLOBAL group viewed by a customer-bound account: confine to their projects.
        q = q.where(Project.customer_id == viewer_customer_id)
    elif getattr(grp, "org_id", None) is not None:
        q = q.where(Project.org_id == grp.org_id)
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for src, tgt in session.exec(q).all():
        key = (languages_catalog.normalize(src), languages_catalog.normalize(tgt))
        if key[1] and key not in seen:
            seen.add(key)
            out.append((src, tgt))
    return out
