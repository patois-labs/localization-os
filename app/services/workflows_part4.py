"""workflows: part 4 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

import math

from sqlmodel import Session, select

from app.models import (
    Scope,
    StepType,
    Workflow,
    WorkflowGroup,
)
from app.services import ids
from app.services.workflows_common import (
    _BRANCH_SLOTS,
    _RC_OPS,
    _RC_REFS,
    _RC_SCORE_OPS,
    _RC_STEP_PREFIX,
    _RULE_GROUP_MEMBER_TYPE,
    _RULE_GROUP_MULTI_BRANCH,
    _RULE_GROUP_REENTRY,
    _RULE_GROUP_SPLIT,
    DEAD_FIELDS_BY_TYPE,
    WorkflowError,
    _is_step_ref,
    _step_attr,
    slugify_key,
)
from app.services.workflows_part2 import (
    _PAIR_SUFFIX_ONE_RE,
    PARALLEL_PHASE1_TYPES,
    WorkflowArchivedError,
    WorkflowArchiveError,
    WorkflowCustomerMismatchError,
    WorkflowForeignCustomerError,
    _ordered,
    _pair,
    _pgroup,
    _rc_ref_qualifies,
    _same_pair,
    _stable_ref,
    _step_label,
    _step_ref_key,
    group_variants,
    parse_run_condition_str,
)
from app.services.workflows_part3 import (
    _DEAD_FIELD_HINT,
    _DEAD_FIELD_LABELS,
    _dead_field_is_meaningful,
    _group_runs,
    _group_span,
    _is_pair_suffix_body,
    _ref_resolves,
    _reject_unknown_locales,
    _variant_match,
)


def _ambiguous_short_ref(ref: str, prior_steps):
    """SPEC-parallel-routing §5.5, the save-time ambiguity refusal. Returns
    (group_slug, [candidate steps]) when a BARE prev_lqa/prev_qe would be ambiguous --
    i.e. the NEAREST upstream step that produces this outcome belongs to a parallel
    group in which 2+ members produce it -- else (None, []).

    `_latest_outcome_steprun` WOULD resolve this deterministically (highest StepRun id
    = the highest order_index member, since the join persists members in order). That is
    exactly why this is refused rather than silently resolved: the determinism is an
    implementation detail of the persistence order, not something an author can read off
    their workflow. Fail closed at save; never guess at run (the condwf house style)."""
    ordered = _ordered(prior_steps)
    nearest = None
    for i in range(len(ordered) - 1, -1, -1):
        if _rc_ref_qualifies(ordered[i], ref):
            nearest = i
            break
    if nearest is None:
        return None, []
    slug = _pgroup(ordered[nearest])
    if not slug:
        return None, []
    first, last = _group_span(ordered, nearest)
    cands = [ordered[k] for k in range(first, last + 1) if _rc_ref_qualifies(ordered[k], ref)]
    return (slug, cands) if len(cands) >= 2 else (None, [])


def validate_run_condition(cond, prior_steps):
    """Validate + sanitize a step's run_condition against the fixed allowlists and the
    workflow's EARLIER steps (SPEC §5). Returns a clean {ref, op, value} dict, or None
    for an absent/blank condition (= always run). Raises WorkflowError on anything
    malformed so the caller (web/CLI/promptable) surfaces a clean error and creates
    nothing. NEVER eval'd — data only.

    `cond` may be a dict, the compact CLI string ('prev_lqa:failed'), None, or "".
    `prior_steps` are the steps that will precede this one (add_step appends, so this
    is every existing step of the workflow)."""
    if cond is None:
        return None
    if isinstance(cond, str):
        s = cond.strip()
        if not s:
            return None
        cond = parse_run_condition_str(s)
    if not isinstance(cond, dict):
        raise WorkflowError("run condition must be an object {ref, op, value}.")
    # Reject extra keys (injection-safety: an unknown key never rides along).
    extra = set(cond) - {"ref", "op", "value"}
    if extra:
        raise WorkflowError(
            f"run condition has unrecognized keys: {sorted(extra)} (only ref/op/value are allowed)."
        )
    ref = cond.get("ref")
    op = cond.get("op")
    value = cond.get("value")
    if _is_step_ref(ref):
        # SPEC-parallel-routing §5.5: normalise `step:<key>` through the SAME slug
        # rules `key` itself uses, so 'step:LQA Main' and 'step:lqa_main' are the same
        # ref and a typo'd/blank key is surfaced instead of silently never matching.
        _k = _step_ref_key(ref)
        if not _k:
            raise WorkflowError(
                f"run condition ref {ref!r} names no step key -- use "
                "'step:<key>' with the key of an earlier gate-producing step."
            )
        ref = _RC_STEP_PREFIX + _k
    elif ref not in _RC_REFS:
        raise WorkflowError(
            f"run condition ref {ref!r} is not allowed (use one of {list(_RC_REFS)}, "
            "or 'step:<key>' to name an earlier gate-producing step)."
        )
    if op not in _RC_OPS:
        raise WorkflowError(f"run condition op {op!r} is not allowed (use one of {list(_RC_OPS)}).")
    if op in _RC_SCORE_OPS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkflowError(f"run condition op {op!r} requires a numeric value (e.g. 90).")
        # GitHub #502: convert under guard. A Python int wider than float range (a 401-digit
        # JSON int passed raw by the add/update-step API) makes float() itself raise
        # OverflowError, which the routes do not catch and would 500; refuse it here instead.
        # NaN/inf survive float() but compare false against every score (the guard would fail
        # open) and break a Postgres JSON bind, so refuse those too via isfinite.
        try:
            value = float(value)
        except OverflowError:
            raise WorkflowError(
                f"run condition op {op!r} requires a finite numeric value (e.g. 90)."
            ) from None
        if not math.isfinite(value):
            raise WorkflowError(
                f"run condition op {op!r} requires a finite numeric value (e.g. 90)."
            )
    else:
        value = None  # passed/failed ignore value
    if not _ref_resolves(ref, prior_steps):
        if _is_step_ref(ref):
            raise WorkflowError(
                f"run condition ref {ref!r} does not resolve: no EARLIER step in this "
                "workflow carries that key and produces a gate (an LQA step, or an "
                "llm_agent step with the QE gate ON). A step in the SAME parallel "
                "group is not 'earlier' -- lanes cannot read each other."
            )
        kind = "LQA" if ref == "prev_lqa" else "QE-gated"
        raise WorkflowError(
            f"run condition references a prior {kind} step (ref {ref!r}) but no such "
            "step exists before this one."
        )
    # SPEC-parallel-routing §5.5: a bare prev_lqa/prev_qe whose nearest producer sits in
    # a parallel group with 2+ producers is AMBIGUOUS to the author -> refuse and demand
    # `step:<key>`. Unambiguous workflows never reach this (zero migration).
    _amb_slug, _amb = _ambiguous_short_ref(ref, prior_steps)
    if _amb_slug:
        _named = [f"step:{_step_attr(s, 'key')}" for s in _amb if _step_attr(s, "key")]
        _hint = (
            f" (e.g. {_named[0]}:{op})"
            if _named
            else " -- give those steps a key first, then reference one of them"
        )
        raise WorkflowError(
            f"run condition ref {ref!r} is ambiguous: the nearest matching steps are "
            f"{len(_amb)} members of the parallel group {_amb_slug!r}, which run "
            f"together. Name the one you mean with 'step:<key>'{_hint}."
        )
    return {"ref": ref, "op": op, "value": value}


def _group_errors(ordered) -> list:
    """Every PARALLEL-GROUP membership error over an ordered step list, as
    (step_id, label, message, identity) 4-tuples (the shape _graph_edit_errors uses).
    Never raises. See the ERROR IDENTITY note above for what the 4th element is for.

    Implements SPEC-parallel-routing §5.1, fail-closed:
      1. members must be CONSECUTIVE (a slug appearing in two separate runs = split);
      2. Phase 1 members are CHECK-ONLY (`lqa`) -- an llm_agent/human_review member is
         refused with an explicit Phase-1 message;
      3. at most ONE member carries `branch` (the routing member, §5.3);
      5. an OUTSIDE branch may target the group's FIRST member only (re-entering the
         whole group); targeting a non-first member is refused.

    (§5.1.4's "a member may not reference another member" is enforced upstream by
    `_pre_group_prior`, which trims the member's own group off its `prior` list, so the
    ordinary run_condition resolution refuses it with its own message. §5.1.6/§5.1.7 need
    no validation: max_visits already applies per member, and the TM-first trigger is
    translate-only, hence unreachable under rule 2.)

    DELIBERATELY NOT ENFORCED: "a group has >= 2 members". See the note on
    `_SINGLETON_GROUP_IS_INERT` below."""
    errors = []
    runs = _group_runs(ordered)
    by_slug: dict = {}
    for g, a, b in runs:
        by_slug.setdefault(g, []).append((a, b))
    for slug, spans in by_slug.items():
        if len(spans) > 1:
            for a, b in spans[1:]:
                for k in range(a, b + 1):
                    s = ordered[k]
                    errors.append(
                        (
                            _step_attr(s, "id"),
                            _step_label(s),
                            f"parallel group {slug!r} is split by other steps -- the steps "
                            "of a group must be consecutive (move them next to each other, "
                            "or use a different group name)",
                            # SYMMETRIC identity: which members land in the "second run" is
                            # a pure function of position, so attributing the split to a step
                            # id makes it flip under any shuffle. The slug IS the error.
                            (_RULE_GROUP_SPLIT, slug),
                        )
                    )
    for slug, a, b in runs:
        members = ordered[a : b + 1]
        if len(members) < 2:
            continue  # a lone member is inert (see _SINGLETON_GROUP_IS_INERT)
        branched = 0
        for s in members:
            st = _step_attr(s, "step_type")
            if st not in PARALLEL_PHASE1_TYPES:
                errors.append(
                    (
                        _step_attr(s, "id"),
                        _step_label(s),
                        f"parallel group {slug!r}: Phase 1 supports parallel CHECKS only -- "
                        f"a {st} step cannot be a member (only lqa steps can run in a "
                        "parallel group). Remove it from the group, or change its type.",
                        (_step_attr(s, "id"), _RULE_GROUP_MEMBER_TYPE, slug),
                    )
                )
            if _step_attr(s, "branch"):
                branched += 1
        if branched > 1:
            for s in members:
                if _step_attr(s, "branch"):
                    errors.append(
                        (
                            _step_attr(s, "id"),
                            _step_label(s),
                            f"parallel group {slug!r}: at most ONE member may carry a branch "
                            f"(the routing member) -- {branched} do. Routing happens once, "
                            "at the join; drop the branch from the others.",
                            # The COUNT is in the message but not the identity: adding a third
                            # branching member is new breakage for THAT member, not for the
                            # two that were already wrong.
                            (_step_attr(s, "id"), _RULE_GROUP_MULTI_BRANCH, slug),
                        )
                    )
    # Rule 5: an OUTSIDE branch may only re-enter a group at its FIRST member.
    if runs:
        idx_by_key = {}
        for i, s in enumerate(ordered):
            k = _step_attr(s, "key")
            if k and k not in idx_by_key:
                idx_by_key[k] = i
        for i, s in enumerate(ordered):  # noqa: B007
            br = _step_attr(s, "branch")
            if not isinstance(br, dict):
                continue
            for slot in _BRANCH_SLOTS:
                tgt = br.get(slot)
                if not isinstance(tgt, dict) or "step" not in tgt:
                    continue
                ti = idx_by_key.get(slugify_key(tgt.get("step")))
                if ti is None:
                    continue  # dangling: the branch rules own it
                t_first, t_last = _group_span(ordered, ti)
                if t_first == t_last:
                    continue  # target is not in a group
                # NO SELF-TARGET EXEMPTION (adversarial review, MEDIUM-3). This used to
                # skip the check when the SOURCE was itself inside the target's group
                # ("a member targeting its own group"), on the assumption that a member
                # can only be looping back to its own group's start. It cannot: the
                # routing member's own key is a legal branch target (validate_branch
                # accepts `self_key`), so `on_fail -> <a NON-FIRST member of my own
                # group>` slipped through -- and at run time `_group_at` returns None
                # for a mid-group cursor, so that lane RE-RAN ALONE while its siblings'
                # visit counts never bumped. The legitimate case (a member looping back
                # to its group's FIRST step, re-running the whole group) still passes,
                # because it satisfies `ti == t_first` on the very next line.
                if ti != t_first:
                    errors.append(
                        (
                            _step_attr(s, "id"),
                            _step_label(s),
                            f"branch {slot} targets {_stable_ref(ordered[ti])!r}, which is a "
                            f"NON-FIRST member of parallel group "
                            f"{_pgroup(ordered[ti])!r}. A group is entered at its first step "
                            "only (the whole group re-runs) -- target "
                            f"{_stable_ref(ordered[t_first])!r} instead.",
                            # The identity names the SOURCE step, the slot and the TARGET KEY
                            # -- all untouched by a move. The MESSAGE additionally names which
                            # member is currently first, which a lane reorder legitimately
                            # changes; keeping that out of the key is the whole point.
                            (
                                _step_attr(s, "id"),
                                _RULE_GROUP_REENTRY,
                                slot,
                                slugify_key(tgt.get("step")),
                            ),
                        )
                    )
    return errors


def refuse_dead_fields(step_type: str, values: dict, current=None) -> None:
    """Fail-closed refusal (SPEC-lqa-step-honest-options) for a field the caller
    ACTIVELY supplied that has no effect on `step_type`. `values` maps field name ->
    the caller-supplied (already-normalised) candidate value for every field
    relevant to THIS call. PUBLIC: shared by add_step/update_step AND
    setup_author.apply_workflow_draft's pre-write per-step validation (the AI-
    drafted-workflow apply path also creates steps through add_step, so a drafted
    lqa step with e.g. qe_gate=true must be caught with the SAME friendly per-step
    error there, not surfaced as a generic "internal error" from add_step raising
    mid-write).

    `current` (the pre-edit WorkflowStep, update_step only) makes this a DIFF check:
    a dead field already carrying a meaningful value that the caller did NOT change
    this time is left alone -- an existing stored step with dead config (they exist
    in prod: a live LQA step with qe_gate/qe_provider already set) keeps loading and
    editing fine; only a freshly-set/changed dead field is refused. Without `current`
    (add_step -- a brand-new step has no prior state) any meaningful value is a fresh
    explicit choice and is refused outright."""
    dead = DEAD_FIELDS_BY_TYPE.get(step_type)
    if not dead:
        return
    offenders = []
    for field in dead:
        if field not in values:
            continue
        new_val = values[field]
        if not _dead_field_is_meaningful(field, new_val):
            continue
        if current is not None and getattr(current, field, None) == new_val:
            continue  # unchanged legacy value -- not a new explicit choice
        offenders.append(_DEAD_FIELD_LABELS.get(field, field))
    if offenders:
        kind = (
            "an LQA"
            if step_type == StepType.LQA
            else "a human_review"
            if step_type == StepType.HUMAN_REVIEW
            else "a tm_match"
            if step_type == StepType.TM_MATCH
            else f"an {step_type}"
        )
        plural = len(offenders) > 1
        raise WorkflowError(
            f"{', '.join(offenders)} {'have' if plural else 'has'} no effect on "
            f"{kind} step and must be cleared (or left at its default) -- "
            f"{_DEAD_FIELD_HINT.get(step_type, '')}"
        )


_DEAD_FIELD_DEFAULT = {
    "provider": None,
    "model": None,
    "fallback_provider": None,
    "fallback_model": None,
    "prompt_text": "",
    "prompt_template_id": None,
    "chunk_policy": "auto",
    "redact_pii": False,
    "glossary_id": None,
    "adaptive_feedback": False,
    "doc_context_prepass": False,
    "tm_examples": True,
    "use_image_context": True,
    "concept_glossary_render": False,
    "glossary_concept_cap": 10,
    "glossary_token_cap": 0,
    "qe_gate": False,
    "qe_threshold": 70,
    "qe_provider": None,
    "qe_autofix": False,
    "qe_autofix_provider": None,
    "qe_autofix_max_rounds": 3,
    "qe_autofix_cross_check": False,
    "tm_protect": None,
    "over_translation_guard": None,
    "memory_mode": None,
    "lqa_threshold": None,
    "parallel_group": None,
    "vendor_id": None,
    "external_wait_hours": None,
    "tm_fill_threshold": None,
    "tm_fuzzy_lock_mode": None,
    "tm_lock_ice": None,
    "tm_hundreds_enter_lqa": None,
    "tm_context_floor": None,
    "tm_pool_selection": None,
}


# The safe/"no-op" value each dead-able field resets to when FORCE-CLEARED (never
# refused: see _force_clear_dead_fields). Matches the class defaults exactly (the
# same values _dead_field_is_meaningful treats as "not meaningful").
# Batch 14 T2 (SPEC §25.5-A): NOT NULL default TRUE — a type switch / propagation
# force-clear MUST write True (never None, which would IntegrityError the NOT NULL
# column). Paired with the _dead_field_is_meaningful carve-out (True never reads
# "meaningful", so the force-clear never fires spuriously anyway).
# Terminology T3 (SPEC §9-T3): NOT NULL defaults — a type switch / propagation
# force-clear MUST write these (never None, which would IntegrityError the NOT NULL
# columns). Default OFF/10/0 = the toggle-OFF, glossary_line-verbatim state.
# run-190 Item D / M5: NULLABLE (None = inherit the global OVER_TRANSLATION_MAX_RATIO
# knob, ON) — a type switch off llm_agent force-clears to None (safe, no IntegrityError).
# feat/lqa-configurable-threshold: a TYPE SWITCH off lqa force-clears the pass bar
# back to None (a step that is no longer an lqa step has no gate to configure). A
# DIRECT set on a non-lqa step is still refused (refuse_dead_fields).
# SPEC-parallel-routing §5.6: a TYPE SWITCH off lqa force-clears the group slug
# (and REPORTS it via type_switch_cleared) rather than refusing -- the builder hides
# the field the instant the type changes, so a refusal would have nothing for the
# user to act on. A DIRECT set on a non-lqa step is still refused (refuse_dead_fields).
# External/vendor dispatch (Wave A): a TYPE SWITCH off external force-clears the
# vendor selection + wait window back to None (a step that is no longer external has
# no vendor to dispatch to).
# Batch 15 T1 (SPEC-tm-match-step-config §8.A): all six are NULLABLE, so a TYPE SWITCH
# off tm_match force-clears them to None (inherit) -- SAFE (no IntegrityError as a
# NOT-NULL column would raise; contrast use_image_context). A DIRECT set on a
# non-tm_match step is still refused (refuse_dead_fields).
def _force_clear_dead_fields(step, effective_type: str) -> list:
    """Reset, IN PLACE on `step`, every field dead for `effective_type` that
    currently holds a MEANINGFUL (non-default) value back to its safe default.
    Returns the list of field names actually cleared (empty when none needed it).

    NEVER refuses -- this is the force-clear counterpart to refuse_dead_fields,
    used exactly where a refusal would have nothing for the user to act on:
    update_step's own TYPE SWITCH (the builder hides these fields the moment the
    type changes, so a hidden checkbox's old value has no UI path to un-set) and
    propagate_step_edit's per-variant sweep (a variant owner who never touched
    this step at all cannot act on anything either). Bypasses _resolve_provider_
    edit's engine-pin-retention quirk on purpose: THAT quirk exists to protect an
    unrelated edit from silently clearing an unrepresentable engine: pin, but a
    force-clear is a deliberate reset, not an accidental blank -- an engine: pin
    on a field that is dead for the new/effective type must actually clear."""
    cleared = []
    for f in DEAD_FIELDS_BY_TYPE.get(effective_type, ()):
        if _dead_field_is_meaningful(f, getattr(step, f, None)):
            setattr(step, f, _DEAD_FIELD_DEFAULT.get(f))
            cleared.append(f)
    return cleared


# --- workflows ---------------------------------------------------------------
def create_workflow(
    session: Session,
    name: str,
    scope: str = Scope.GLOBAL,
    customer_id: str = "",
    org_id=None,
    created_via: str = "manual",
    bound_customer_id=None,
) -> Workflow | None:
    """Create a workflow from a (stripped) name. Returns the new Workflow, or
    None when the name is blank (the route redirects back without creating).

    `org_id`, when given (the creating user's org in multi-org mode — leak vector
    12: a GLOBAL-scope workflow is org-owned now, not app-global), stamps the new
    row; None (the gate off) leaves org_id NULL, byte-identical to today.

    `bound_customer_id` (chip task_56b8b2bf) is the caller's OWN customer binding
    when the caller is a customer-bound account (None = unbound staff / trusted CLI).
    THE SHARED TENANCY GUARD, hoisted here so both the web POST /workflows route and
    the /api route inherit it: when a `customer_id` is named it must be one the caller
    may actually scope to —
      * a BOUND caller may only ever name its OWN customer (any other id ->
        WorkflowForeignCustomerError); and
      * in MULTI-ORG mode (org_id given) a customer that EXISTS but lives in another
        org is refused too (the cross-org plant).
    A nonexistent id and an archived customer are left to the existing paths (FK /
    the routes' _archived_customer_block) so this adds no new behaviour for them — it
    refuses ONLY a real customer the caller has no business scoping to, which is the
    pre-existing cross-tenant hole this chip closes.

    `created_via` (SPEC-workflows-redesign Phase 3 origin stamp) records how the row
    was created: the default "manual" for the direct blank-workflow / `workflow create`
    callers; apply_workflow_draft passes "draft". Never NULL through this seam (only
    pre-migration legacy rows read NULL)."""
    name = ids.clean_name(name, field="workflow name")
    if not name:
        return None
    cid = ids.coerce_id(customer_id)
    bound = ids.coerce_id(bound_customer_id)
    if bound is not None:
        # #468: a customer-bound caller never creates a GLOBAL (shared) workflow; a
        # blank customer becomes its own, and a global scope label becomes customer.
        if cid is None:
            cid = bound
        if scope == Scope.GLOBAL:
            scope = Scope.CUSTOMER
    if cid is not None:
        if bound is not None and cid != bound:
            raise WorkflowForeignCustomerError(cid)
        if org_id is not None:
            from app.models import Customer

            cust = session.get(Customer, cid)
            # THE TRUST SPLIT (review I2, so a future caller knows the contract): this
            # SEAM owns two refusals -- (1) a bound caller's non-own customer, and (2) a
            # customer with a CONCRETE org that differs (a genuine cross-org plant). It
            # does NOT reject a NULL-org customer (unstamped / single-tenant-seeded /
            # legacy): comparing a NULL column to a concrete org gives restart-dependent
            # answers (access.py's NULL-org normalization note). Quarantining a NULL-org
            # customer from a scoped caller is therefore the CALLER's job -- the web route
            # does it via access.same_org (_wf_create_customer_block, pinned by
            # verify_workflow_create_tenancy's NULL-org-plant case). So the seam is NOT
            # the single source of truth for EVERY cross-tenant refusal: a new caller that
            # passes org_id but forgets the NULL-org same_org pre-check could bind a
            # NULL-org customer. No exploit today (the CLI local arm acts as trusted
            # staff; the two wire surfaces both pre-check), but a new wire caller MUST add
            # the same_org quarantine, not assume this seam covers it.
            cust_org = getattr(cust, "org_id", None)
            if cust is not None and cust_org is not None and cust_org != org_id:
                raise WorkflowForeignCustomerError(cid)
    wf = Workflow(name=name, scope=scope, customer_id=cid, org_id=org_id, created_via=created_via)
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return wf


def archive_workflow(
    session: Session, workflow_id: int, *, actor: str = "system", ip: str = "", org_id=None
) -> Workflow:
    """Archive (retire) a workflow: stamp archived_at=now so it drops out of the default
    workflow listings + pickers, is EXCLUDED from dispatch/variant resolution, and is
    BLOCKED from launching new runs. Its steps + run history are fully preserved (NO
    cascade, NO delete) and it is reversible. Org-guarded (a cross-org id raises so the
    caller's 404 gate can translate it). Idempotent-safe: archiving an already-archived
    workflow raises a clean WorkflowArchiveError. Audits workflow.archive HERE (unlike
    rename_workflow, where the caller logs) so every surface records it identically.
    Returns the Workflow."""
    from datetime import datetime as _dt

    wf = session.get(Workflow, workflow_id)
    if wf is None:
        raise WorkflowError(f"workflow {workflow_id} not found.")
    if org_id is not None and getattr(wf, "org_id", None) != org_id:
        raise WorkflowError(f"workflow {workflow_id} not found.")
    if wf.archived_at is not None:
        raise WorkflowArchiveError(f"workflow {workflow_id} ({wf.name}) is already archived.")
    wf.archived_at = _dt.utcnow()
    session.add(wf)
    session.commit()
    session.refresh(wf)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="workflow.archive",
        target=f"workflow {wf.id} ({wf.name})",
        ip=ip,
        customer_id=wf.customer_id,
    )
    return wf


def unarchive_workflow(
    session: Session, workflow_id: int, *, actor: str = "system", ip: str = "", org_id=None
) -> Workflow:
    """Unarchive (reactivate) a workflow: clear archived_at so it is listed, offered in
    pickers, dispatch-eligible, and runnable again. Org-guarded. Idempotent-safe:
    unarchiving an active workflow raises WorkflowArchiveError. Audits
    workflow.unarchive. Returns the Workflow."""
    wf = session.get(Workflow, workflow_id)
    if wf is None:
        raise WorkflowError(f"workflow {workflow_id} not found.")
    if org_id is not None and getattr(wf, "org_id", None) != org_id:
        raise WorkflowError(f"workflow {workflow_id} not found.")
    if wf.archived_at is None:
        raise WorkflowArchiveError(f"workflow {workflow_id} ({wf.name}) is not archived.")
    wf.archived_at = None
    session.add(wf)
    session.commit()
    session.refresh(wf)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="workflow.unarchive",
        target=f"workflow {wf.id} ({wf.name})",
        ip=ip,
        customer_id=wf.customer_id,
    )
    return wf


def refuse_if_workflow_archived(session: Session, workflow_id) -> None:
    """Raise WorkflowArchivedError if `workflow_id` names an ARCHIVED workflow. The
    service guard every NEW-run launcher calls so an archived workflow can never be
    re-run via a forged POST / stray CLI id. A missing workflow / None is left to the
    caller's own resolution (this only refuses a real archived one)."""
    if workflow_id is None:
        return
    wf = session.get(Workflow, workflow_id)
    if wf is not None and getattr(wf, "archived_at", None) is not None:
        raise WorkflowArchivedError(
            f"workflow {wf.id} ({wf.name}) is archived — unarchive it before running "
            f"new translations."
        )


def refuse_if_workflow_customer_mismatch(
    session: Session, workflow_id, *, doc=None, project=None
) -> None:
    """#542 item 3: refuse a NEW run whose workflow belongs to a different customer than
    the document (its project's customer). Allowed: a global workflow (customer_id NULL) or
    a workflow of the document's own customer. FAIL CLOSED: a customer workflow on a
    document with no resolvable project / no customer is refused too. The one service seam
    every launcher shares (start_run, run_workflow, batch, plan, keys); a missing workflow
    is left to the caller's own resolution."""
    if workflow_id is None:
        return
    wf = session.get(Workflow, workflow_id)
    if wf is None or getattr(wf, "customer_id", None) is None:
        return
    if project is None and doc is not None:
        from app.models import Project

        pid = getattr(doc, "project_id", None)
        project = session.get(Project, pid) if pid is not None else None
    doc_customer = getattr(project, "customer_id", None) if project is not None else None
    if doc_customer is None or wf.customer_id != doc_customer:
        raise WorkflowCustomerMismatchError(
            f"workflow {wf.id} ({wf.name}) belongs to a different customer than this "
            f"document; use a global workflow or one of the document's own customer."
        )


def pair_name(base: str, src: str | None, tgt: str | None) -> str:
    """Name a pair variant: the base name's STEM plus exactly ONE `[src->tgt]` suffix.

    Issue #46(6): naming a clone off a workflow that is ITSELF a pair variant used to
    compound suffixes (`ACME [en-US->fr-FR] [en-US->de-DE]`) because the clone name was
    built from the source's full name. Every naming site goes through here, so the stem
    is stripped first and a name carries at most one pair disclosure.

    R7 (adversarial review): a name that is ENTIRELY a suffix (`[en-US->fr-FR]` — legal
    if an operator typed it) strips to an EMPTY stem, and must then render as the bare
    new suffix rather than re-compounding onto the old one."""
    stem = strip_pair_suffix(base)
    suffix = f"[{_pair(src, tgt)}]"
    return f"{stem} {suffix}" if stem else suffix


def strip_pair_suffix(name: str) -> str:
    """`name` without its trailing `[src->tgt]` / `[universal]` suffix(es).

    R5 (adversarial review): strips REPEATEDLY, to a fixed point. Names already
    compounded by the pre-fix code (`X [en-US->fr-FR] [en-US->es-419]` — such rows exist
    in prod) kept one suffix under a single-strip and re-compounded on the next copy.

    Returns "" for a name that is nothing BUT a suffix (R7) — callers that need a
    non-blank fallback say so explicitly (`strip_pair_suffix(n) or n`)."""
    out = (name or "").strip()
    while out:
        m = _PAIR_SUFFIX_ONE_RE.search(out)
        if m is None or not _is_pair_suffix_body(m.group(1)):
            break
        out = out[: m.start()].strip()
    return out


def add_to_group(
    session: Session,
    workflow_id: int,
    group_id: int,
    source_locale: str | None = None,
    target_locale: str | None = None,
    make_default: bool = False,
    force: bool = False,
) -> Workflow | None:
    """Attach an EXISTING workflow to a group as the variant for one language pair.
    Returns the updated Workflow, or None when either id is missing.

    Both locales blank on a FRESH add => a UNIVERSAL variant (a group default
    candidate — matches any pair inside the group). Re-adding an already-grouped
    member without an explicit pair PRESERVES its existing pair (so flagging it default
    never silently universalizes it — Phase 4a bonus fix). `make_default` flags this
    variant as the group's fallback default, clearing the flag on the group's other
    variants (a group has at most ONE default).

    ISOLATION (adversarial-review): refuse to attach a workflow whose org differs
    from the group's — a group must never span orgs (the web add path org-gates both
    ends, but the CLI add path org-gates only the workflow, leaving the group id
    unchecked; this closes that at the source). No-op single-tenant (both org_ids
    NULL). Returns None on a cross-org attempt, so the CLI/route reports "no such
    workflow or group" rather than silently building a cross-org group.

    Raises WorkflowError (issue #46) when an explicit pair on an ALREADY-grouped member
    would REPLACE its current pair -- the only path that ever silently dropped a
    language pair from a group. Two adversarial-review corrections to that refusal (R4):

      * a member with NO pair (universal) may always be given an explicit pair -- there
        is nothing to replace, so refusing it removed a legitimate move and the message
        ("would leave that pair uncovered") named a pair that did not exist;
      * `force=True` PERMITS the replace (the refusal removed the only correction path
        for a mistyped pair). The caller is expected to report old->new; `pair_key`
        spells the old pair the same way this module does.

    Also raises WorkflowError (N3, re-review) when either locale is not in the language
    catalog. This was the LAST unvalidated locale entry point: `workflow group add --pair
    zz-NOT-A-LOCALE:qq-JUNK` exited 0 and stored the junk verbatim, and the web add route
    took a 5000-char target_locale — a stored pair nothing can ever dispatch, which then
    feeds names, coverage keys and pickers. Validated BEFORE any mutation, through the
    same `_reject_unknown_locales` seam both copy surfaces use."""
    wf = session.get(Workflow, workflow_id)
    grp = session.get(WorkflowGroup, group_id)
    if wf is None or grp is None:
        return None
    # N3: refuse junk locales before touching the row (blank sides = universal, skipped).
    _reject_unknown_locales(
        [((source_locale or "").strip() or None, (target_locale or "").strip() or None)]
    )
    if getattr(wf, "org_id", None) != getattr(grp, "org_id", None):
        return None
    # Footgun fix (SPEC-workflows-redesign Phase 4a bonus): only (re)set the pair when
    # the workflow is NEWLY joining this group, OR the caller supplied an explicit,
    # non-blank pair. An ALREADY-grouped member re-added to flag it default (e.g.
    # `workflow group add <wf> <grp> --default` with no --pair) used to have its pair
    # silently reset to universal — the real behavioral gap the spec calls out. Fresh
    # adds keep today's behavior (blank pair => universal candidate); an explicit --pair
    # still updates the pair. Both surfaces stay correct: the web add form only offers
    # UNGROUPED workflows (always newly joining), and a fresh CLI add is byte-identical.
    newly_joining = wf.group_id != group_id
    explicit_pair = bool((source_locale or "").strip() or (target_locale or "").strip())
    # R4 (adversarial review): "already HAS a pair" is what makes an explicit pair a
    # REPLACE. A universal member (both locales NULL) covers no pair, so giving it one
    # replaces nothing and is always allowed.
    has_pair = bool(wf.source_locale or wf.target_locale)
    same_pair = _same_pair(source_locale, target_locale, wf.source_locale, wf.target_locale)
    # Issue #46: the ONE genuine replace path. An explicit --pair on an ALREADY-grouped
    # member silently REWROTE that member's pair -- the CLI equivalent of "I added German
    # and it looks like it overrode French". Refuse and name the current pair unless the
    # caller asked for it explicitly (`force`); the operator can also create a new
    # variant (workflow copy --to-pairs) or drop this one.
    replacing = (not newly_joining) and explicit_pair and has_pair and not same_pair
    if replacing and not force:
        raise WorkflowError(
            f"workflow {wf.id} is already the "
            f"{_pair(wf.source_locale, wf.target_locale)} variant of group {group_id}; "
            f"changing it to {_pair((source_locale or '').strip() or None, (target_locale or '').strip() or None)} "  # noqa: E501
            f"would REPLACE that pair, leaving it uncovered. Add a separate variant for "
            f"the new pair instead (workflow copy --to-pairs), or pass --force to "
            f"replace it deliberately."
        )
    # Issue #512(3): (group, source, target) uniqueness, the same rule copy_to_pairs
    # enforces (#46). Two variants for one pair means `_variant_match` always picks the
    # lower id and the newer workflow silently never runs. Judged on the pair the member
    # will HOLD after this call (a same-pair re-state or a bare default re-add keeps its
    # stored pair and is never a duplicate of itself); a universal variant is not a pair.
    if newly_joining or (explicit_pair and not same_pair):
        final_src = (source_locale or "").strip() or None
        final_tgt = (target_locale or "").strip() or None
        if final_src or final_tgt:
            for sib in session.exec(select(Workflow).where(Workflow.group_id == group_id)).all():
                # Only a sibling that could ever be dispatched ALONGSIDE this member competes
                # for the pair (the same filter `_variant_match._compatible` applies): an
                # ARCHIVED variant is retired, a variant of another org never runs for this
                # one, and two variants scoped to DIFFERENT customers are each dispatched
                # only inside their own customer's ring. Refusing those would block a
                # replacement for a retired variant, and would name another customer's
                # workflow id to a customer-bound caller.
                if getattr(sib, "archived_at", None) is not None:
                    continue
                if getattr(sib, "org_id", None) != getattr(wf, "org_id", None):
                    continue
                if (
                    sib.customer_id is not None
                    and wf.customer_id is not None
                    and sib.customer_id != wf.customer_id
                ):
                    continue
                if sib.id != wf.id and _same_pair(
                    final_src, final_tgt, sib.source_locale, sib.target_locale
                ):
                    raise WorkflowError(
                        f"group {group_id} already has a {_pair(final_src, final_tgt)} variant "
                        f"(workflow {sib.id}); a second one would never run, because dispatch "
                        f"always picks the lower id. Edit workflow {sib.id}, or remove it from "
                        f"the group first."
                    )
    if newly_joining and not make_default and wf.is_default:
        # #365 gate fix: a workflow that was ANOTHER group's default must not carry that flag
        # into this group (it left a second default here, and the lowest id then won both
        # dispatch rung 3 and the base a variant's system prompt inherits from).
        wf.is_default = False
    wf.group_id = group_id
    # R3 (adversarial review): a same-pair RE-STATE must not touch the stored locales.
    # `--pair EN_us:DE_de` on the member that already IS en-US/de-DE fell through into the
    # assignment and rewrote the stored SPELLING -- which then poisons every derived
    # string (the variant's name suffix, coverage keys, the group page's hidden pair
    # field). Nothing about the member changed, so nothing about the member is written;
    # only `make_default` (below) still applies.
    restating_same_pair = (not newly_joining) and explicit_pair and same_pair
    if (newly_joining or explicit_pair) and not restating_same_pair:
        wf.source_locale = (source_locale or "").strip() or None
        wf.target_locale = (target_locale or "").strip() or None
    if make_default:
        # exactly one default per group — clear the flag on siblings first
        for sib in session.exec(select(Workflow).where(Workflow.group_id == group_id)).all():
            if sib.id != wf.id and sib.is_default:
                sib.is_default = False
                session.add(sib)
        wf.is_default = True
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return wf


def resolve_variant(
    session: Session,
    workflow_id: int,
    target_locale: str,
    source_locale: str | None = None,
    doc_customer_id: int | None = None,
) -> int:
    """Resolve which workflow ID should actually RUN for a given target locale.

    Resolution order (MASTER_PLAN P5 (b) / research_gapfill GAP 1):
      1. LEGACY / UNGROUPED — if the requested workflow is not part of a group, run
         it AS-IS. This is the sacred byte-identical path: an ungrouped workflow's
         dispatch is exactly today's behavior (no group involved).
      2. EXACT PAIR VARIANT — the group member whose (source_locale, target_locale)
         matches the run's pair. (source match is optional: if `source_locale` is
         given, a variant's source must match OR be universal; target must match.)
      3. GROUP DEFAULT — a variant flagged is_default, or a UNIVERSAL member (both
         locales unset), else the originally-requested workflow if it is itself a
         group member (it may be the family's default).
      4. FALLBACK — the requested workflow id unchanged (never returns something that
         doesn't exist).

    Returns a workflow id (always resolvable). The orchestrator signature is
    UNCHANGED — the caller (web run route / CLI run) resolves BEFORE calling run.

    ISOLATION (adversarial-review finding): dispatch swaps in a SIBLING variant
    server-side (never through the offer-time picker), so it must never cross the
    scope boundary the requested workflow already sits inside. A group can hold
    mixed-scope members (add_to_group does not force org/customer parity, and even
    the web picker only org-gates the addition), so we filter candidate variants to
    those that share the requested workflow's org AND are customer-compatible (a
    variant CUSTOMER-scoped to a DIFFERENT customer than the requested workflow is
    never dispatched — that would run customer B's private config on customer A's
    run). Rung 1 (ungrouped) and rung 4 (the already-validated requested id) are
    untouched, so single-tenant / non-grouped behavior is byte-identical.

    `doc_customer_id` (#542): the customer that owns the run's document. A variant
    stamped with that customer is dispatchable for its documents even when the requested
    workflow is the shared group's global default (see `_variant_match`). Callers that
    start a run pass the document's project customer; None keeps the old behavior."""
    wf = session.get(Workflow, workflow_id)
    if wf is None or wf.group_id is None:
        return workflow_id  # rung 1: legacy/ungrouped — as-is (sacred)
    # rungs 2-4 come from the shared matcher (the coverage view reads the SAME helper).
    return _variant_match(
        session, wf, target_locale, source_locale, doc_customer_id=doc_customer_id
    ).workflow_id


def group_locale_coverage(session: Session, group_id: int, pairs, variants=None) -> list[dict]:
    """For each (source, target) PAIR a scope run would dispatch, how this group resolves
    it — derived from the SAME `_variant_match` ladder `resolve_variant` uses AND classified
    WITH THE PAIR'S OWN SOURCE (the value a real run passes), so the readout reflects real
    dispatch instead of a single family-source assumption. Returns one dict per pair:
    {source, locale, kind, variant_id, variant_name, variant_pair} where kind is:
      - "exact"     rung 2 — an exact-pair variant matches this (source, target)
      - "default"   rung 3 — no exact match; falls back to the group's is_default
      - "universal" rung 3 — no exact match/default; falls back to a universal member
      - "none"      rung 4 — no exact/default/universal; a run would use the requested
                    workflow unchanged (this pair has no group coverage)

    `pairs` is an iterable of (source_locale, target_locale) — typically from
    `group_scope_pairs`. Dispatch is evaluated as if the group's DEFAULT variant (else its
    first member) were the requested workflow — the same representative `resolve_variant`
    would resolve a family run against — but each pair is matched at its OWN source, so a
    project whose source differs from the family's is classified truthfully (an exact
    variant sourced FROM a different language is not falsely reported as covering a run
    that would reject that source, and vice-versa). An empty group yields every pair as
    "none".

    N1 (re-review, merge-blocking): `variants` overrides the member list — the web page
    passes the REQUESTER-VISIBLE members. Reading the raw list made this readout lie to a
    customer-bound viewer in two ways at once. The REPRESENTATIVE (default-else-first) was
    chosen from EVERY member, so a GLOBAL group holding customer B's default resolved
    every row against B: the table NAMED and LINKED B's workflows (a tenancy disclosure on
    the very page the members-table fix cleaned), and it claimed "exact variant <B's
    workflow>" for pairs where a real run BY THAT VIEWER would dispatch their own workflow
    instead — `_variant_match._compatible` is relative to the representative, so a foreign
    representative poisons every rung. Passing the visible members fixes both at once: the
    representative is one the viewer can actually act on, and `_compatible` then keeps
    every reachable row inside that same ring. Omitted -> the whole group, unchanged."""
    pool = group_variants(session, group_id) if variants is None else variants
    if not pool:
        return [
            {
                "source": src,
                "locale": tgt,
                "kind": "none",
                "variant_id": None,
                "variant_name": None,
                "variant_pair": None,
            }
            for (src, tgt) in pairs
        ]
    rep = next((v for v in pool if v.is_default), pool[0])
    rows = []
    for src, tgt in pairs:
        m = _variant_match(session, rep, tgt, src, candidates=pool)
        # rung 4 ("requested") means no exact/default/universal member matched — i.e.
        # this pair has no group coverage; surface it plainly as "none".
        kind = "none" if m.kind == "requested" else m.kind
        rows.append(
            {
                "source": src,
                "locale": tgt,
                "kind": kind,
                "variant_id": m.variant.id if m.variant else None,
                "variant_name": m.variant.name if m.variant else None,
                # Issue #46(2): a fallback row must DISCLOSE the pair the fallback variant is
                # written for -- "de-DE will run <name> (default, en-US->fr-FR)". Without it
                # the reader cannot tell that their German content runs a French-pair config.
                "variant_pair": _pair(m.variant.source_locale, m.variant.target_locale)
                if m.variant
                else None,
            }
        )
    return rows
