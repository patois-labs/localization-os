"""Reflective per-step field-parity regression between the workflow's TWO deep-copy
sites: `workflows.clone_workflow` (whole-workflow "(copy)") and `workflows.copy_to_pairs`
(per-language-pair variant fan-out). Both build a fresh WorkflowStep from the SAME source
step's columns via ~30 named constructor kwargs, and the two call sites have drifted
before: `copy_to_pairs` already carried `prompt_template_id`, `glossary_id`, and
`needs_review` while `clone_workflow` silently dropped each in turn (prompt_template_id:
fixed, SPEC-prompt-template-runtime; glossary_id and needs_review: fixed here) -- a cloned
workflow lost its pinned glossary AND its "flag for linguist review" checkbox, both with no
error, no warning.

THIS SUITE (introspection, not enumeration): reads WorkflowStep's own column list at
import time, subtracts a small, individually-commented exclusion set of identity/runtime
fields that are NOT copyable step config, and asserts every REMAINING field survives both
copy sites unchanged from a source step that has every field populated with a distinctive
non-default value. Because the field list comes from introspection rather than a hardcoded
tuple, a FUTURE column added to WorkflowStep (e.g. the parallel-routing `parallel_group`
column the branching spec is about to add) automatically joins the comparison -- if the
column is added to the model but only ONE of the two copy sites, this suite fails loudly
instead of silently drifting again like glossary_id and needs_review did.

copy_to_pairs is exercised on BOTH its branches: adapt=False (a straight verbatim copy,
including needs_review) and adapt=True, where prompt_text is the ONE field intentionally
rewritten (an LLM-adapted instruction) and needs_review is intentionally forced True
regardless of the source value -- every OTHER comparable field must still survive
unchanged on the adapt=True path too.

fail-pre-fix evidence for both bugs this suite pins (glossary_id, needs_review) was
captured manually per-commit by reverting app/services/workflows.py to its pre-fix state
via `git checkout -- <file>` (worktree-local reset, never `git stash` -- shared stash stack
across worktrees) and observing this suite fail with the expected mismatch, then restoring
the fix. That is not re-encoded as an in-suite HEAD-source check here: a check against
`git show HEAD:...` can never fail once the fix is merged (HEAD then already carries it),
so it would provide no live protection going forward -- the parity test below is the real,
permanently-live protection.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no live provider
is ever reachable; the adapt=True path drives MockProvider only). ASCII-only.
Final line: "CLONE FIELD PARITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cloneparity_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import StepType, Workflow, WorkflowStep
from app.services import glossary_module
from app.services import workflows as wf

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# ============================================================================
# Field taxonomy: introspect WorkflowStep's own columns, then exclude ONLY the
# identity/runtime fields that are not "copyable step config" -- each with a reason.
# ============================================================================
_ALL_FIELDS = set(WorkflowStep.model_fields.keys())

_EXCLUDED = {
    # Primary key -- every copy gets its OWN new id; comparing it would be meaningless.
    "id": "primary key, always a fresh row",
    # Both copy sites deliberately point at a DIFFERENT target workflow by design
    # (clone_workflow's new `new.id`, copy_to_pairs' new `clone.id`) -- it is not
    # "step config" carried FROM the source, it is where the copy lands.
    "workflow_id": "target-workflow identity, not source step config",
    # Both sites already copy this verbatim (`order_index=st.order_index`); it is a
    # structural echo of the source's own position, not a config value that could
    # plausibly diverge between the two copy sites, so it adds no signal here.
    "order_index": "copied verbatim by both sites; positional echo, not config",
    # Cross-language shared-base provenance (SPEC-workflows-redesign 4b). The TWO
    # copy sites intentionally give this DIFFERENT semantics: copy_to_pairs stamps
    # adapted_from_id = the source step's id (a language-pair VARIANT's live link
    # that base-edit propagation walks), while clone_workflow deliberately leaves it
    # NULL -- a whole-workflow "(copy)" is an independent workflow, not a variant of
    # its source, so it must never be a propagation target. Comparing this field for
    # equality across the two sites would be asserting a bug, not a fix.
    "adapted_from_id": "intentionally divergent semantics (variant link vs. NULL)",
    # A PIN is a deliberate per-variant divergence marker. A freshly made copy (either
    # site) is a brand-new row that has not yet been hand-edited, so it always starts
    # False on both sites regardless of the source step's own overridden state
    # (pinning the SOURCE says nothing about whether the COPY should start pinned).
    "overridden": "fresh-copy pin state always starts False, not source-derived",
}

# needs_review is DELIBERATELY comparable (not excluded): clone_workflow and
# copy_to_pairs' adapt=False branch both carry it VERBATIM from the source step (this
# is the second bug this suite pins -- clone_workflow used to always reset it False).
# The adapt=True branch of copy_to_pairs is a separate, narrower exception -- see
# test_copy_to_pairs_adapt_true_preserves_all_but_prompt_text, which excludes ONLY
# prompt_text (never needs_review) from that one comparison.

_COMPARABLE_FIELDS = sorted(_ALL_FIELDS - set(_EXCLUDED))

# Guard the guard: if WorkflowStep ever loses one of the fields we claim to be
# excluding (a rename/removal), fail loudly here instead of quietly comparing fewer
# fields than intended.
for _f in _EXCLUDED:
    assert _f in _ALL_FIELDS, f"exclusion list names a field WorkflowStep no longer has: {_f}"


# ============================================================================
# The reflective parity test: a step with EVERY comparable field populated with a
# distinctive non-default value must survive BOTH clone_workflow and copy_to_pairs
# unchanged.
# ============================================================================
def _mk_workflow(name="cfp"):
    with Session(engine) as s:
        w = Workflow(
            name=f"{name}-{_seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _mk_template():
    with Session(engine) as s:
        t = wf.create_prompt_template(s, f"CFP-Template-{_seq()}", body="Translate: {text}")
        return t.id


def _mk_glossary():
    with Session(engine) as s:
        g = glossary_module.create(s, f"CFP-Glossary-{_seq()}")
        return g.id


def _mk_vendor():
    from app.services import vendors

    with Session(engine) as s:
        return vendors.create(s, f"CFP-Vendor-{_seq()}").id


# One distinctive, non-default value per COMPARABLE field. Deliberately bypasses
# add_step's per-step-type validation (direct WorkflowStep construction, exactly
# like clone_workflow/copy_to_pairs themselves do) so every field can be populated
# on one step regardless of step_type-specific dead-field rules -- this suite is
# about copy FIDELITY, not save-time validation.
def _fully_populated_kwargs(template_id: int, glossary_id: int, vendor_id: int) -> dict:
    values = {
        "name": "CFP full step",
        "step_type": StepType.LLM_AGENT,
        "provider": "mock",
        "model": "cfp-model-x",
        "fallback_provider": "mock-fallback",
        "fallback_model": "cfp-fallback-model",
        "prompt_template_id": template_id,
        "prompt_text": "CFP SOURCE PROMPT {text}",
        "retry_max": 7,
        "backoff_base": 3.5,
        "chunk_policy": "none",
        "redact_pii": True,
        "assignee_email": "cfp-reviewer@example.com",
        "glossary_id": glossary_id,
        "adaptive_feedback": True,
        "adaptive_feedback_window": 9,
        "doc_context_prepass": True,
        "qe_gate": True,
        "qe_threshold": 55,
        "qe_provider": "mock-qe",
        # QE auto-fix (Train B): distinctive non-defaults (default OFF / NULL / 3 / OFF) so
        # a copy site that forgets one fails loudly here.
        "qe_autofix": True,
        "qe_autofix_provider": "mock-fixer",
        "qe_autofix_max_rounds": 5,
        "qe_autofix_cross_check": True,
        # feat/lqa-configurable-threshold: an lqa step's configurable pass bar. A
        # distinctive non-default value so a copy site that forgets it fails loudly here
        # rather than silently resetting a cloned lqa step's gate back to the default 90.
        "lqa_threshold": 94,
        "tm_examples": False,
        "tm_examples_window": 4,
        # Batch 14 T2 (SPEC §25.7): default is TRUE, so False is the distinctive value —
        # a copy site that forgets use_image_context resets it and fails loudly here.
        "use_image_context": False,
        # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle + caps.
        # Distinctive non-default values (default OFF/10/0) so a copy site that forgets one
        # fails loudly here rather than silently resetting a cloned step's render config.
        "concept_glossary_render": True,
        "glossary_concept_cap": 7,
        "glossary_token_cap": 900,
        "tm_protect": True,
        # run-190 Item D / M5: a distinctive non-default (False = this step opts out of the
        # page-dump guard) so a copy site that forgets it fails loudly here.
        "over_translation_guard": False,
        "run_condition": {"ref": "prev_lqa", "op": "passed", "value": None},
        "key": "cfp-key-1",
        "branch": {"on_pass": "next", "on_fail": "end"},
        "max_visits": 3,
        "memory_mode": "off",
        # SPEC-parallel-routing §5.6: the column this suite's header predicted. A
        # distinctive slug so a copy site that forgets it fails loudly here rather
        # than silently flattening a cloned workflow's parallel lanes.
        "parallel_group": "cfp-checks",
        # PROPOSAL-external-processing-step Wave A: the external step's vendor selection +
        # per-step wait override. Distinctive non-default values so a copy site that
        # forgets either fails loudly here rather than silently dropping a cloned
        # workflow's vendor dispatch config. vendor_id is a real FK (a created vendor).
        "vendor_id": vendor_id,
        "external_wait_hours": 96,
        # Batch 15 T1 (SPEC-tm-match-step-config §5.1): the six tm_match per-step config
        # columns. Default is None (inherit), so a distinctive non-None value on each makes
        # a copy site that forgets one fail loudly here rather than silently resetting a
        # cloned tm_match step's TM policy to inherit. (WorkflowRun.used_tm_match is on
        # WorkflowRun, NOT WorkflowStep, so it does NOT enroll in this introspection.)
        "tm_fill_threshold": 85,
        "tm_fuzzy_lock_mode": "policy",
        "tm_lock_ice": False,
        "tm_hundreds_enter_lqa": False,
        "tm_context_floor": 88,
        "tm_pool_selection": [1, 2],
        # Distinctive (non-default False): the "flag for linguist review" checkbox
        # (workflow_author_review.html -> setup_author.py) -- the second bug this
        # suite pins (clone_workflow used to silently reset this to False).
        "needs_review": True,
    }
    missing = set(_COMPARABLE_FIELDS) - set(values)
    assert not missing, (
        "a WorkflowStep field is comparable but this fixture forgot to give it a "
        "distinctive value -- add one above (this is exactly the guard that would "
        "have caught a newly-added column like the upcoming parallel_group): "
        f"{sorted(missing)}"
    )
    return values


def _get_only_step(session: Session, workflow_id: int) -> WorkflowStep:
    steps = session.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)).all()
    assert len(steps) == 1, (workflow_id, len(steps))
    return steps[0]


def _mk_populated_source():
    """A fresh source workflow with one step carrying every comparable field set to a
    distinctive non-default value. Returns (src_wid, src_values)."""
    tid = _mk_template()
    gid = _mk_glossary()
    vid = _mk_vendor()
    src_wid = _mk_workflow()
    with Session(engine) as s:
        s.add(
            WorkflowStep(
                workflow_id=src_wid, order_index=0, **_fully_populated_kwargs(tid, gid, vid)
            )
        )
        s.commit()
        src_step = _get_only_step(s, src_wid)
        src_values = {f: getattr(src_step, f) for f in _COMPARABLE_FIELDS}
    return src_wid, src_values


def test_clone_and_copy_preserve_all_comparable_fields():
    src_wid, src_values = _mk_populated_source()

    # --- clone_workflow: whole-workflow "(copy)" ---
    with Session(engine) as s:
        clone = wf.clone_workflow(s, src_wid)
        clone_wid = clone.id
    with Session(engine) as s:
        clone_step = _get_only_step(s, clone_wid)
        clone_mismatches = [
            f for f in _COMPARABLE_FIELDS if getattr(clone_step, f) != src_values[f]
        ]
    assert not clone_mismatches, (
        "clone_workflow dropped or altered field(s) that must survive a deep copy: "
        f"{clone_mismatches} (src={ {f: src_values[f] for f in clone_mismatches} }, "
        f"clone={ {f: getattr(clone_step, f) for f in clone_mismatches} })"
    )
    print(
        f"  1. clone_workflow preserves all {len(_COMPARABLE_FIELDS)} comparable "
        "fields (glossary_id + needs_review included) OK"
    )

    # --- copy_to_pairs: per-language-pair variant fan-out (adapt=False path) ---
    with Session(engine) as s:
        result = wf.copy_to_pairs(s, src_wid, [("en-US", "fr-FR")], adapt=False)
    copy_wid = result["created"][0]["workflow_id"]
    with Session(engine) as s:
        copy_step = _get_only_step(s, copy_wid)
        copy_mismatches = [f for f in _COMPARABLE_FIELDS if getattr(copy_step, f) != src_values[f]]
    assert not copy_mismatches, (
        "copy_to_pairs (adapt=False) dropped or altered field(s) that must survive a "
        f"deep copy: {copy_mismatches}"
    )
    print(
        f"  2. copy_to_pairs(adapt=False) preserves all {len(_COMPARABLE_FIELDS)} "
        "comparable fields too (the site that was already correct) OK"
    )

    print(
        f"  3. {len(_COMPARABLE_FIELDS)} comparable fields checked; "
        f"{len(_EXCLUDED)} excluded by name with a reason each: "
        f"{sorted(_EXCLUDED)} OK"
    )


def test_copy_to_pairs_adapt_true_preserves_all_but_prompt_text():
    """copy_to_pairs' adapt=True branch has TWO fields that intentionally diverge from
    the source, both excluded from the bulk diff and checked individually instead of by
    equality-to-source: prompt_text gets rewritten by adapt_instruction for the new
    language pair, and needs_review is unconditionally FORCED True (workflows.py:2032)
    regardless of the source value -- asserted directly rather than diffed against the
    source, so a fixture whose source value happens to already be True can never make a
    regression that removes the forcing look like a coincidental pass. Every OTHER
    comparable field must still survive unchanged. MockProvider (provider="mock" on the
    fixture step) makes the adaptation call offline; a provider failure would just keep
    the original prompt_text (still a pass for this test, since prompt_text is excluded
    from the comparison either way)."""
    src_wid, src_values = _mk_populated_source()
    with Session(engine) as s:
        result = wf.copy_to_pairs(s, src_wid, [("en-US", "de-DE")], adapt=True)
    copy_wid = result["created"][0]["workflow_id"]
    with Session(engine) as s:
        copy_step = _get_only_step(s, copy_wid)
        # prompt_text: the intentional LLM-rewrite divergence (excluded, checked above).
        # needs_review: the adapt=True branch unconditionally FORCES this True
        # (workflows.py:2032), independent of the source value -- also excluded from
        # this diff and checked separately below with an explicit assert, so the fixture's
        # source value of True can never make that forcing coincidentally look like a
        # pass-through copy (a mutation probe confirmed: removing the forcing line must
        # fail this suite, and it only does with the dedicated assert below).
        fields_to_diff = [f for f in _COMPARABLE_FIELDS if f not in ("prompt_text", "needs_review")]
        mismatches = [f for f in fields_to_diff if getattr(copy_step, f) != src_values[f]]
        assert copy_step.needs_review is True, (
            "copy_to_pairs (adapt=True) must force needs_review True, independent of "
            f"the source value -- got {copy_step.needs_review!r}"
        )
    assert not mismatches, (
        "copy_to_pairs (adapt=True) dropped or altered field(s) OTHER than the "
        f"intentionally-rewritten prompt_text: {mismatches}"
    )
    print(
        f"  4. copy_to_pairs(adapt=True) preserves all {len(fields_to_diff)} "
        "comparable fields except prompt_text (the sole intentional divergence -- "
        "an LLM-adapted instruction); needs_review is asserted forced True "
        "independent of source value OK"
    )


def main():
    test_clone_and_copy_preserve_all_comparable_fields()
    test_copy_to_pairs_adapt_true_preserves_all_but_prompt_text()
    print("CLONE FIELD PARITY OK")


if __name__ == "__main__":
    main()
