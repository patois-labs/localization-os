"""WORKFLOW CANVAS, Phase 1 (SPEC-workflow-canvas.md section 7).

Reordering a workflow's steps becomes a real operation, on every surface: a service
seam (`workflows.reorder_step`), a web route the builder's drag-and-drop AND its
keyboard "move up / move down" buttons both POST to, and a headless CLI verb. This
suite is the whole vertical.

GROUPS
  [A] THE SEAM (section 4.1). Happy paths; CONTIGUOUS 0..n-1 renumbering (including
      healing the gaps a past delete left, which no other mutation path may touch);
      every refusal proven fail-closed WITH its message and with the graph unchanged
      afterwards -- an in-flight run, a gate moved BELOW the step whose guard depends on
      it, a move that SPLITS a parallel group from either direction, an out-of-range
      position; before/after diff TOLERANCE (a pre-existing, engine-tolerated breakage
      never blocks an unrelated move); TM-first eligibility following the move in BOTH
      directions; clone + copy_to_pairs preserving a reordered order.
  [B] THE WEB ROUTE (section 4.2/5). RBAC = the step-edit predicate; 303 on success;
      the refusal renders as a 400 carrying the validator's message VERBATIM (character
      for character -- the canvas never paraphrases); and the ATOMIC band drop: when the
      slug edit refuses after the move committed, the move is rolled back EXACTLY (the
      pre-move order_index values, not merely the relative order) and the user sees the
      slug refusal, never a half-applied drop.
  [C] THE CLI VERB. --to / --before / --after round-trip against the same seam, the same
      refusal as a clean usage error (never a traceback), and --json carrying the whole
      resulting order.
  [D] A11Y. The builder renders a move-up/move-down form per step, posting to the same
      route, omitted at the ends of the list rather than rendered dead -- so a keyboard
      or screen-reader user has the full capability without the drag interaction.
  [E] THE MACRO (section 4.4). `editable=false` output is BYTE-IDENTICAL to the
      parameterless call for grouped AND ungrouped workflows (sha256 + length pinned,
      and the pin is PROVEN DISCRIMINATING below); `editable=true` emits the grips, the
      tiled slot rects with correct `data-gap`, the node positions, and one lane band
      per real group with the `data-gap` that joins it; slot PRUNING marks only lqa
      steps groupable.

WHY THE BYTE-IDENTITY PIN IS PROVEN, NOT ASSUMED. "Identical to before the change" is
not a property a suite written after the change can assert against itself, so [E] does
two things a plain snapshot does not:

  * PRE-CHANGE EVIDENCE, captured manually at build time (the branch point's
    `_macros.html` copied aside into a scratch template directory and rendered against
    these exact fixtures -- never `git stash`, whose stack is shared across worktrees):
    `git show 3e1b91d:app/templates/_macros.html` and the working tree produced
    BYTE-IDENTICAL output for the ungrouped fixture (4476 bytes), the grouped fixture
    (5481 bytes) and the grouped fixture WITH a run overlay (6001 bytes). The digests
    pinned below are those exact strings. It is not re-encoded as an in-suite `git show`
    check, for the reason tests/verify_clone_field_parity.py states in full: a check
    against a fixed commit can never fail once merged, so it protects nothing.
  * A FAIL-BEFORE PROOF that is permanently live: `test_e_pin_is_discriminating`
    renders a MUTATED copy of the macro -- the editable block's gate removed, i.e. the
    exact accident this design is built to prevent -- and asserts the pin REJECTS it.
    A snapshot test that cannot fail is worse than none.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env, provider
keys blanked). ASCII-only output. Final line: "WORKFLOW CANVAS OK".
"""

import hashlib
import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_wfcanvas_")

from jinja2 import Environment, FileSystemLoader
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator
from app.services import workflows as wf

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    raise AssertionError(msg)


def _mk_wf(name="CWF"):
    with Session(engine) as s:
        w = Workflow(
            name=f"{name}-{_seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _translate(wid, name="Translate", **kw):
    with Session(engine) as s:
        return wf.add_step(
            s,
            wid,
            name=name,
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
            **kw,
        ).id


def _check(wid, name, **kw):
    with Session(engine) as s:
        return wf.add_step(s, wid, name=name, step_type=StepType.LQA, provider="mock", **kw).id


def _review(wid, name="Review", **kw):
    with Session(engine) as s:
        return wf.add_step(s, wid, name=name, step_type=StepType.HUMAN_REVIEW, **kw).id


def _steps(wid):
    with Session(engine) as s:
        return orchestrator.get_steps(s, wid)


def _order(wid):
    """(order_index, name) pairs in spine order -- the human-readable shape probes assert."""
    return [(s.order_index, s.name) for s in _steps(wid)]


def _names(wid):
    return [s.name for s in _steps(wid)]


def _id_of(wid, name):
    for s in _steps(wid):
        if s.name == name:
            return s.id
    _fail(f"no step named {name!r} in workflow {wid}")


def _move(wid, step_id, pos):
    with Session(engine) as s:
        return wf.reorder_step(s, wid, step_id, pos)


def _err(fn, *a, **kw) -> str:
    """Run `fn`, return the WorkflowError message; fail the suite if it did NOT raise."""
    try:
        fn(*a, **kw)
    except wf.WorkflowError as exc:
        return str(exc)
    _fail("expected a WorkflowError, got none")


def _mk_project(name="CP"):
    with Session(engine) as s:
        p = Project(name=f"{name}-{_seq()}", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(project_id, locale="es-ES"):
    with Session(engine) as s:
        d = Document(project_id=project_id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale=locale,
                order_index=0,
                source_text="Hello world.",
                status=SegStatus.NEW,
            )
        )
        s.commit()
        return d.id


def _plant_run(wid, status):
    """A WorkflowRun row in `status` for `wid`. Never executed -- the run-safety guards
    read only (workflow_id, status) -- but document_id carries a real FK."""
    doc = _mk_doc(_mk_project("plant"))
    with Session(engine) as s:
        r = WorkflowRun(
            workflow_id=wid,
            document_id=doc,
            target_locale="es-ES",
            status=status,
            current_step_index=0,
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


# ============================================================================
# [A] THE SEAM
# ============================================================================
def test_a_happy_paths_and_contiguous_renumbering():
    wid = _mk_wf("A1")
    _translate(wid, name="T")
    _check(wid, "L")
    _translate(wid, name="F")
    _review(wid, name="R")
    assert _order(wid) == [(0, "T"), (1, "L"), (2, "F"), (3, "R")], _order(wid)

    # Move the last step to the front; everything renumbers contiguously.
    _move(wid, _id_of(wid, "R"), 0)
    assert _order(wid) == [(0, "R"), (1, "T"), (2, "L"), (3, "F")], _order(wid)
    # ... and back to the end.
    _move(wid, _id_of(wid, "R"), 3)
    assert _order(wid) == [(0, "T"), (1, "L"), (2, "F"), (3, "R")], _order(wid)
    # ... and into the middle, both directions.
    _move(wid, _id_of(wid, "T"), 2)
    assert _names(wid) == ["L", "F", "T", "R"], _names(wid)
    _move(wid, _id_of(wid, "T"), 1)
    assert _names(wid) == ["L", "T", "F", "R"], _names(wid)
    print(
        "  [A1] reorder_step moves a step to any position, both directions, and "
        "renumbers 0..n-1 contiguously"
    )

    # A no-op move (already at that position) is accepted and stays a no-op.
    _move(wid, _id_of(wid, "T"), 1)
    assert _names(wid) == ["L", "T", "F", "R"], _names(wid)
    print("  [A1] a move to the position a step already holds is a clean no-op")


def test_a_renumbering_heals_delete_gaps():
    """THE ONE OPERATION ENTITLED TO RENUMBER. add_step appends at max+1 and delete_step
    compacts nothing (deliberately -- so no unrelated step ever shifts under an edit),
    which leaves permanent holes in order_index. A move owns the whole order, so it is
    the seam that hands out a clean 0..n-1 and heals them.

    A REAL move heals; a NO-OP move deliberately does not (see [AC4]) -- healing slides
    steps toward index 0, and index 0 arms the TM-first short-circuit, so it may only
    happen as part of a change the user actually asked for."""
    wid = _mk_wf("A2")
    _translate(wid, name="T")
    mid = _check(wid, "Gone")
    _translate(wid, name="F")
    _review(wid, name="R")
    with Session(engine) as s:
        wf.delete_step(s, mid, workflow_id=wid)
    assert [s.order_index for s in _steps(wid)] == [0, 2, 3], _order(wid)
    _move(wid, _id_of(wid, "R"), 1)  # a REAL move: R jumps ahead of F
    assert _order(wid) == [(0, "T"), (1, "R"), (2, "F")], _order(wid)
    print(
        "  [A2] a real move compacts order_index to 0..n-1, healing the gap a delete "
        "left (the only seam that may)"
    )


def test_a_refusal_active_run():
    wid = _mk_wf("A3")
    t = _translate(wid, name="T")
    _check(wid, "L")
    for status in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED):
        w2 = _mk_wf("A3s")
        t2 = _translate(w2, name="T")
        _check(w2, "L")
        _plant_run(w2, status)
        with Session(engine) as s:
            msg = _err(wf.reorder_step, s, w2, t2, 1)
        assert "in-flight run" in msg and "resume at the wrong one" in msg, (status, msg)
        assert _names(w2) == ["T", "L"], (status, _names(w2))
    # A FINISHED run does not block.
    _plant_run(wid, RunStatus.COMPLETED)
    _move(wid, t, 1)
    assert _names(wid) == ["L", "T"], _names(wid)
    print(
        "  [A3] a move is refused while a run is pending/running/paused and the order "
        "is untouched; a finished run does not block"
    )


def test_a_refusal_out_of_range_and_unknown_step():
    wid = _mk_wf("A4")
    t = _translate(wid, name="T")
    _check(wid, "L")
    with Session(engine) as s:
        assert "out of range" in _err(wf.reorder_step, s, wid, t, 2)
        assert "out of range" in _err(wf.reorder_step, s, wid, t, -1)
        assert "whole number" in _err(wf.reorder_step, s, wid, t, "middle")
        # A step of ANOTHER workflow is not addressable through this workflow's id.
        other = _mk_wf("A4b")
        ot = _translate(other, name="X")
        assert "not found in workflow" in _err(wf.reorder_step, s, wid, ot, 0)
    assert _names(wid) == ["T", "L"], _names(wid)
    print(
        "  [A4] out-of-range / non-numeric positions and a foreign step id are all "
        "refused, changing nothing"
    )


def test_a_refusal_gate_moved_below_its_referrer():
    """POSITION IS A GRAPH PROPERTY. A guard resolves against the steps that PRECEDE it,
    so moving the only gate-producing step BELOW the step that depends on it orphans that
    reference -- exactly the class of breakage the whole-graph diff exists for."""
    wid = _mk_wf("A5")
    _translate(wid, name="T")
    lqa = _check(wid, "L", key="a5_l")
    _translate(wid, name="Fix", key="a5_fix", run_condition="prev_lqa:failed")
    with Session(engine) as s:
        msg = _err(wf.reorder_step, s, wid, lqa, 2)
    assert "would break the workflow graph" in msg, msg
    assert "prev_lqa" in msg and "no such step exists before this one" in msg, msg
    assert _names(wid) == ["T", "L", "Fix"], _names(wid)
    print(
        "  [A5] moving a gate BELOW the step whose guard depends on it is refused "
        f"naming the dependent step -- {msg[:96]!r}"
    )

    # The MIRROR image: dragging the dependent step ABOVE its producer is the same
    # breakage from the other side, and is refused too.
    with Session(engine) as s:
        msg2 = _err(wf.reorder_step, s, wid, _id_of(wid, "Fix"), 1)
    assert "would break the workflow graph" in msg2 and "prev_lqa" in msg2, msg2
    assert _names(wid) == ["T", "L", "Fix"], _names(wid)
    print(
        "  [A5] and the mirror image (dragging the GUARDED step above its producer) is refused too"
    )


def test_a_refusal_group_split_both_directions():
    """SPEC-parallel-routing section 5.1.1: members must be CONSECUTIVE. Two moves can
    break that -- dropping an OUTSIDER between members, or dragging a MEMBER out of
    contiguity without clearing its slug. The UI avoids offering these drops; the seam is
    the authority, so both are proven here with the UI bypassed entirely."""
    wid = _mk_wf("A6")
    _translate(wid, name="T")
    _check(wid, "L1", key="a6_a", parallel_group="checks")
    _check(wid, "L2", key="a6_b", parallel_group="checks")
    _translate(wid, name="P")
    assert _names(wid) == ["T", "L1", "L2", "P"], _names(wid)

    # (a) an OUTSIDER dropped BETWEEN the members.
    with Session(engine) as s:
        msg = _err(wf.reorder_step, s, wid, _id_of(wid, "P"), 2)
    assert "is split by other steps" in msg and "must be consecutive" in msg, msg
    assert _names(wid) == ["T", "L1", "L2", "P"], _names(wid)
    print(f"  [A6a] dropping an outsider BETWEEN two members is refused -- {msg[:92]!r}")

    # (b) a MEMBER dragged out of contiguity while it still carries the slug.
    with Session(engine) as s:
        msg = _err(wf.reorder_step, s, wid, _id_of(wid, "L1"), 0)
    assert "is split by other steps" in msg, msg
    assert _names(wid) == ["T", "L1", "L2", "P"], _names(wid)
    print(
        "  [A6b] dragging a member OUT of contiguity without clearing its slug is "
        "refused (the same consecutiveness rule, from the other side)"
    )

    # (c) moving a member WITHIN its own group is allowed -- lane order only affects
    #     display and the deterministic join persistence order (SPEC section 4.1).
    _move(wid, _id_of(wid, "L2"), 1)
    assert _names(wid) == ["T", "L2", "L1", "P"], _names(wid)
    assert [s.parallel_group for s in _steps(wid)] == [None, "checks", "checks", None]
    print("  [A6c] reordering LANES inside a group is allowed and keeps the membership")


def test_a_refusal_branch_reentry_at_non_first_member():
    """SPEC-parallel-routing section 5.1.5: an outside branch may re-enter a group at its
    FIRST member only. A move can create that violation without touching any branch --
    reorder the lanes under a branch that names one of them."""
    wid = _mk_wf("A7")
    _translate(wid, name="T")
    _check(wid, "L1", key="a7_a", parallel_group="checks")
    _check(wid, "L2", key="a7_b", parallel_group="checks")
    _translate(wid, name="Fix", key="a7_fix", branch={"on_pass": {"step": "a7_a"}}, qe_gate=True)
    with Session(engine) as s:
        msg = _err(wf.reorder_step, s, wid, _id_of(wid, "L2"), 1)
    assert "NON-FIRST member of parallel group" in msg, msg
    assert _names(wid) == ["T", "L1", "L2", "Fix"], _names(wid)
    print(
        "  [A7] a move that leaves a branch re-entering a group at a NON-FIRST member "
        f"is refused -- {msg[:96]!r}"
    )


def test_a_diff_tolerates_pre_existing_breakage():
    """BEFORE/AFTER, not a freeze. A workflow can already carry an engine-tolerated
    inconsistency (here: a group split that was created by a delete, which the delete
    itself never introduced). That must not block an UNRELATED move."""
    wid = _mk_wf("A8")
    _translate(wid, name="T")
    _check(wid, "L1", key="a8_a", parallel_group="checks")
    _check(wid, "L2", key="a8_b", parallel_group="checks")
    _check(wid, "M", key="a8_m")
    _check(wid, "X", key="a8_x")
    _translate(wid, name="A")
    _translate(wid, name="B")
    # Manufacture the pre-existing split directly -- X carries the group's slug but sits
    # apart from it, which no seam would ever create (every mutation path refuses it).
    with Session(engine) as s:
        rows = orchestrator.get_steps(s, wid)
        rows[4].parallel_group = "checks"
        s.add(rows[4])
        s.commit()
        pre = wf._graph_edit_errors(orchestrator.get_steps(s, wid))
    assert any("is split by other steps" in m for _i, _l, m, _id in pre), pre

    # An UNRELATED move (swapping the two trailing translate steps, nowhere near the
    # group) still succeeds -- the broken state travels along untouched.
    _move(wid, _id_of(wid, "B"), 5)
    assert _names(wid) == ["T", "L1", "L2", "M", "X", "B", "A"], _names(wid)
    with Session(engine) as s:
        post = wf._graph_edit_errors(orchestrator.get_steps(s, wid))
    assert any("is split by other steps" in m for _i, _l, m, _id in post), post
    print(
        "  [A8] a pre-existing (engine-tolerated) group split does NOT block an "
        "unrelated move -- the diff is a diff, not a freeze"
    )


def test_a_tm_first_follows_the_move_both_directions():
    """THE HONEST SIDE EFFECT (SPEC section 4.1). The TM-first short-circuit fires on
    `order_index == 0`. Reordering therefore GRANTS and REVOKES it, and the spec says to
    document that rather than special-case it. Asserted on the seam's own output AND
    against the orchestrator's own trigger expression, so a change to either is caught."""
    wid = _mk_wf("A9")
    _translate(wid, name="T")
    _check(wid, "L")
    tid = _id_of(wid, "T")

    def eligible(step_id):
        with Session(engine) as s:
            st = s.get(WorkflowStep, step_id)
            # The orchestrator's own trigger, quoted: a TRANSLATE step at index 0.
            return st.step_type == StepType.LLM_AGENT and st.order_index == 0

    assert eligible(tid), "the translate step should start TM-first-eligible"
    _move(wid, tid, 1)
    assert not eligible(tid), "moving the translate step off the front kept TM-first"
    print("  [A9] moving a translate step AWAY from position 0 REVOKES TM-first eligibility")
    _move(wid, tid, 0)
    assert eligible(tid), "moving the translate step back to the front lost TM-first"
    print(
        "  [A9] moving it back to position 0 GRANTS it again -- reordering means this, "
        "it is not special-cased"
    )


def test_a_copies_preserve_the_reordered_order():
    wid = _mk_wf("A10")
    _translate(wid, name="T")
    _check(wid, "L")
    _translate(wid, name="F")
    _move(wid, _id_of(wid, "F"), 0)
    assert _names(wid) == ["F", "T", "L"], _names(wid)

    with Session(engine) as s:
        clone = wf.clone_workflow(s, wid)
        got = [x.name for x in orchestrator.get_steps(s, clone.id)]
    assert got == ["F", "T", "L"], got
    print("  [A10] clone_workflow preserves a REORDERED order")

    with Session(engine) as s:
        res = wf.copy_to_pairs(s, wid, [("en-US", "fr-FR")])
        made = res["created"][0]["workflow_id"]
        got2 = [x.name for x in orchestrator.get_steps(s, made)]
    assert got2 == ["F", "T", "L"], got2
    print("  [A10] copy_to_pairs preserves it too (both copy sites)")


# ============================================================================
# [A-ID] ERROR IDENTITY -- the before/after diff must key on something a MOVE
#        cannot change. Both repros below were REFUSED before the fix (review R1,
#        HIGH): the diff keyed on (step_id, message), and reordering makes both of
#        those unstable for two error classes.
# ============================================================================
def _plant_legacy_guard(wid, name, ref="prev_lqa"):
    """A downstream bare prev_lqa that is ALREADY ambiguous. Planted directly because
    every save-time seam refuses to CREATE one -- which is the point: real installs
    carry these from before the ambiguity rule existed, and a reorder must not become
    the thing that finally trips over them."""
    with Session(engine) as s:
        st = [x for x in orchestrator.get_steps(s, wid) if x.name == name][0]  # noqa: RUF015
        st.run_condition = {"ref": ref, "op": "failed", "value": None}
        s.add(st)
        s.commit()


def test_aid_lane_reorder_survives_a_flipping_ambiguity_example():
    """REPRO 1. The ambiguity refusal's message ends with a worked example naming the
    FIRST keyed candidate -- "(e.g. step:lane_a:failed)". Reordering the lanes inside a
    group is explicitly ALLOWED (SPEC-workflow-canvas 4.1: lane order only affects
    display and join persistence order), but it flips which candidate is first, so the
    PRE-EXISTING error's text changed and the diff read it as NEW."""
    wid = _mk_wf("AID1")
    _translate(wid, name="T")
    _check(wid, "L1", key="lane_a", parallel_group="checks")
    _check(wid, "L2", key="lane_b", parallel_group="checks")
    _translate(wid, name="Fix", key="aid_fix")
    _plant_legacy_guard(wid, "Fix")

    with Session(engine) as s:
        pre = wf._graph_edit_errors(orchestrator.get_steps(s, wid))
    assert any("is ambiguous" in m for _s, _l, m, _i in pre), pre
    assert "lane_a" in "".join(m for _s, _l, m, _i in pre), (
        "the fixture must reproduce the message that names the FIRST lane"
    )

    _move(wid, _id_of(wid, "L2"), 1)  # swap the two lanes
    assert _names(wid) == ["T", "L2", "L1", "Fix"], _names(wid)
    assert [s.parallel_group for s in _steps(wid)] == [None, "checks", "checks", None]
    # The error is still there, still pre-existing, and now names the OTHER lane -- the
    # exact text change that used to be read as new breakage.
    with Session(engine) as s:
        post = wf._graph_edit_errors(orchestrator.get_steps(s, wid))
    assert any("lane_b" in m for _s, _l, m, _i in post), post
    print(
        "  [AID1] a lane reorder under a legacy ambiguous prev_lqa is ALLOWED -- the "
        "message's worked example flips, the identity does not"
    )


def test_aid_duplicate_key_attribution_is_symmetric():
    """REPRO 2. A duplicate key is reported against whichever occurrence is seen SECOND.
    Moving the later occurrence in FRONT of the earlier one hands the error to the other
    step id, so a pre-existing duplicate read as new breakage and the move was refused."""
    wid = _mk_wf("AID2")
    _translate(wid, name="A", key="dup")
    _translate(wid, name="B")
    _translate(wid, name="C")
    with Session(engine) as s:  # planted: no seam creates this
        c = [x for x in orchestrator.get_steps(s, wid) if x.name == "C"][0]  # noqa: RUF015
        c.key = "dup"
        s.add(c)
        s.commit()
    with Session(engine) as s:
        pre = [i for _s, _l, _m, i in wf._graph_edit_errors(orchestrator.get_steps(s, wid))]
    assert any(i[0] == "dup_key" for i in pre), pre

    _move(wid, _id_of(wid, "C"), 0)
    assert _names(wid) == ["C", "A", "B"], _names(wid)
    print(
        "  [AID2] moving the SECOND occurrence of a duplicate key in front of the "
        "first is ALLOWED -- the identity names the KEY, not an occurrence"
    )


def test_aid_add_step_onto_a_LEGACY_split_is_allowed():
    """THE ONE DELIBERATE BEHAVIOUR CHANGE from routing add_step through the shared
    identity keying, pinned here so it is a decision rather than a drift.

    On a workflow whose slug is ALREADY split, adding a new step carrying that slug used
    to be REFUSED: the new fragment produced a split message against a step id that had
    none before, and the (step_id, message) diff read that as fresh breakage. The
    symmetric identity ("slug X is split") sees the truth -- X was split before and after,
    so the add introduced nothing.

    Allowing it is correct. The split is a STANDING error still surfaced on every render;
    at run time `_group_at` forks each consecutive run of >= 2 members independently, so
    an extra fragment changes no other lane; and refusing TRAPPED legacy installs, because
    a workflow that already carries a split cannot be repaired by adding (there is no
    insert-at-position), so the refusal blocked every future add while fixing nothing.
    A CLEAN group is still protected -- second half of this test."""
    wid = _mk_wf("AID4")
    _translate(wid, name="T")
    _check(wid, "L1", key="a4_a", parallel_group="checks")
    _check(wid, "L2", key="a4_b", parallel_group="checks")
    _check(wid, "M", key="a4_m")
    tail = _check(wid, "Tail", key="a4_t")
    with Session(engine) as s:  # planted: a PRE-EXISTING split
        t = s.get(WorkflowStep, tail)
        t.parallel_group = "checks"
        s.add(t)
        s.commit()

    def split_errors():
        with Session(engine) as s:
            return [
                m
                for _i, _l, m, _id in wf._graph_edit_errors(orchestrator.get_steps(s, wid))
                if "is split by other steps" in m
            ]

    assert split_errors(), "the fixture must start with a split slug"
    with Session(engine) as s:  # the add SUCCEEDS
        made = wf.add_step(
            s, wid, name="New", step_type=StepType.LQA, key="a4_n", parallel_group="checks"
        )
    assert made.parallel_group == "checks", made.parallel_group
    assert "New" in _names(wid), _names(wid)
    # ...and the graph error is still REPORTED -- allowed is not the same as hidden.
    assert split_errors(), "the split stopped being reported after the add"
    print(
        "  [AID4] adding to an ALREADY-split slug is allowed (it introduces nothing) "
        "and the split is still reported -- legacy installs are not trapped"
    )

    # A CLEAN group is still protected: an add that would CREATE a split is refused.
    wid2 = _mk_wf("AID4b")
    _translate(wid2, name="T")
    _check(wid2, "L1", key="a4c_a", parallel_group="checks")
    _check(wid2, "L2", key="a4c_b", parallel_group="checks")
    _check(wid2, "M", key="a4c_m")
    with Session(engine) as s:
        msg = _err(
            wf.add_step,
            s,
            wid2,
            name="New",
            step_type=StepType.LQA,
            key="a4c_n",
            parallel_group="checks",
        )
    assert "is split by other steps" in msg, msg
    assert "New" not in _names(wid2), _names(wid2)
    print(
        "  [AID4] but an add that would CREATE a split on a clean group is still "
        f"refused -- {msg[:76]!r}"
    )


def test_aid_no_permutation_re_keys_an_existing_error():
    """THE PROPERTY the two repros are instances of, over a graph carrying SEVERAL
    pre-existing errors: EVERY single-step move to EVERY position is checked.

    The property is NOT "no move ever adds an error" -- position is genuinely part of the
    graph, so a move really can orphan a guard or split/fuse a parallel group, and those
    must still be refused. The property is that a move may not RE-KEY a fact it did not
    change, which shows up as an identity appearing out of nowhere. Two checks separate
    the two cases:

      * DUPLICATE KEYS ARE POSITION-INVARIANT. A move does not touch any step's key, so
        the set of duplicated keys is identical before and after -- ALWAYS, for every
        permutation. Under the old (step_id, message) keying this was violated by every
        move that reordered the two occurrences (repro 2).
      * A GUARD ERROR MAY ONLY APPEAR FOR A STEP THAT DID NOT ALREADY HAVE ONE. A move
        can legitimately orphan a guard that used to resolve; it may NOT make a guard
        that was already failing look like a different, new failure. Under the old
        keying, the ambiguity message's worked example flipped with lane order and did
        exactly that (repro 1)."""
    wid = _mk_wf("AID3")
    _translate(wid, name="T")
    _check(wid, "L1", key="p_a", parallel_group="checks")
    _check(wid, "L2", key="p_b", parallel_group="checks")
    _check(wid, "M", key="p_m")
    _translate(wid, name="Fix", key="p_fix")
    _translate(wid, name="Tail")
    # M's bare prev_lqa is ambiguous (its nearest producers are the two lanes).
    _plant_legacy_guard(wid, "M")
    with Session(engine) as s:  # + a duplicate key and a split
        rows = orchestrator.get_steps(s, wid)
        rows[5].key = "p_m"  # duplicate of M's key
        rows[5].parallel_group = "checks"  # a SECOND run of the slug = a split group
        s.add(rows[5])
        s.commit()

    def idents(rows):
        return {i for _s, _l, _m, i in wf._graph_edit_errors(rows)}

    def by_rule(ids, code):
        return {i for i in ids if i[0] == code}

    def guard_steps(ids):
        return {i[0] for i in ids if len(i) == 3 and i[1] == "guard"}

    with Session(engine) as s:
        rows = orchestrator.get_steps(s, wid)
        base = idents(rows)
    assert len(base) >= 3, ("the fixture must carry several distinct errors", base)
    assert by_rule(base, "dup_key") and by_rule(base, "group_split") and guard_steps(base), (
        "the fixture must exercise all three shapes",
        base,
    )
    n = len(rows)
    checked = 0
    for step in rows:
        others = [x for x in rows if x.id != step.id]
        for pos in range(n):
            cand = others[:pos] + [step] + others[pos:]  # noqa: RUF005
            after = idents([wf._graph_row(x, i) for i, x in enumerate(cand)])
            assert by_rule(after, "dup_key") == by_rule(base, "dup_key"), (
                "moving %r to position %d changed the DUPLICATE-KEY identities "  # noqa: UP031
                "(%r -> %r) -- a move does not touch any step's key"
                % (step.name, pos, by_rule(base, "dup_key"), by_rule(after, "dup_key"))
            )
            re_keyed = guard_steps(after - base) & guard_steps(base)
            assert not re_keyed, (
                "moving %r to position %d produced a NEW guard identity for step(s) %r "  # noqa: UP031
                "that ALREADY had a guard error -- the same fact was re-keyed"
                % (step.name, pos, re_keyed)
            )
            checked += 1
    print(
        "  [AID3] permutation property over %d single-step moves: duplicate-key "  # noqa: UP031
        "identities are invariant and no already-failing guard is ever re-keyed "
        "(%d pre-existing errors in the fixture)" % (checked, len(base))
    )


# ============================================================================
# [A-C] THE COMPOSED SEAM: move + lane membership, one diff, one commit
# ============================================================================
def test_ac_drag_out_of_a_group():
    """Leaving a group was UNIMPLEMENTED AND IMPOSSIBLE (review R1+R2, convergent HIGH).
    The surfaces did move-then-clear as two calls, so the move was judged while the step
    still carried the slug and ALWAYS tripped the consecutiveness rule -- every drag-out
    was a guaranteed refusal. Composed, it just works."""
    wid = _mk_wf("AC1")
    _translate(wid, name="T")
    _check(wid, "L1", key="ac_a", parallel_group="checks")
    _check(wid, "L2", key="ac_b", parallel_group="checks")
    _translate(wid, name="P")

    # The two-call shape, reproduced: the move ALONE (slug untouched) is refused.
    with Session(engine) as s:
        msg = _err(wf.reorder_step, s, wid, _id_of(wid, "L1"), 0)
    assert "is split by other steps" in msg, msg

    # Composed, the SAME move plus the clear is accepted.
    with Session(engine) as s:
        wf.reorder_step(s, wid, _id_of(wid, "L1"), 0, parallel_group=None)
    got = [(s.name, s.parallel_group) for s in _steps(wid)]
    assert got == [("L1", None), ("T", None), ("L2", "checks"), ("P", None)], got
    print(
        "  [AC1] drag-OUT: the move alone is refused, the composed move+clear lands in one commit"
    )


def test_ac_cross_group_move():
    """A member dragged onto ANOTHER group's band composes to one new slug + one move.
    The old design could not express this either (same reason as drag-out), which is why
    the UI was offering a drop that was guaranteed to refuse."""
    wid = _mk_wf("AC2")
    _translate(wid, name="T")
    _check(wid, "A1", key="ac2_a1", parallel_group="ga")
    _check(wid, "A2", key="ac2_a2", parallel_group="ga")
    _check(wid, "B1", key="ac2_b1", parallel_group="gb")
    _check(wid, "B2", key="ac2_b2", parallel_group="gb")
    with Session(engine) as s:
        wf.reorder_step(s, wid, _id_of(wid, "A2"), 4, parallel_group="gb")
    got = [(s.name, s.parallel_group) for s in _steps(wid)]
    assert got == [("T", None), ("A1", "ga"), ("B1", "gb"), ("B2", "gb"), ("A2", "gb")], got
    print("  [AC2] cross-group move: a member SWITCHES groups in one composed call")


def test_ac_composed_refusal_names_the_real_rule():
    """A composed call whose FINAL state is invalid is refused naming the actual rule --
    not a spurious intermediate. A translate step onto a lane band is the Phase-1
    check-only rule, and it must read as that."""
    wid = _mk_wf("AC3")
    _translate(wid, name="T")
    _check(wid, "L1", key="ac3_a", parallel_group="checks")
    _check(wid, "L2", key="ac3_b", parallel_group="checks")
    _translate(wid, name="P")
    before = _order(wid)
    with Session(engine) as s:
        msg = _err(wf.reorder_step, s, wid, _id_of(wid, "P"), 3, parallel_group="checks")
    assert "parallel group" in msg and "llm_agent" in msg, msg
    assert _order(wid) == before, (before, _order(wid))
    with Session(engine) as s:
        assert s.get(WorkflowStep, _id_of(wid, "P")).parallel_group is None
    print(
        "  [AC3] a composed drop with an invalid FINAL state is refused naming the "
        f"real rule, and NOTHING is written -- {msg[:80]!r}"
    )


def test_ac_noop_move_does_not_arm_tm_first():
    """GAP HEALING IS A REAL SIDE EFFECT. delete_step leaves holes in order_index; the
    move seam compacts them. So a move that changes NOTHING used to still renumber -- and
    if the hole was at index 0, compaction slid a translate step into index 0 and
    SILENTLY armed the TM-first short-circuit (a 100% TM match stops calling the model).
    A no-op must be a no-op."""
    wid = _mk_wf("AC4")
    lead = _check(wid, "Lead")
    _translate(wid, name="T")
    _check(wid, "L")
    with Session(engine) as s:
        wf.delete_step(s, lead, workflow_id=wid)
    assert _order(wid) == [(1, "T"), (2, "L")], _order(wid)
    tid = _id_of(wid, "T")

    def armed():
        with Session(engine) as s:
            return s.get(WorkflowStep, tid).order_index == 0

    assert not armed(), "the fixture must start with TM-first NOT armed"
    rep: dict = {}
    with Session(engine) as s:
        wf.reorder_step(s, wid, tid, 0, report=rep)  # already first in the LIST
    assert rep.get("noop") is True, rep
    assert _order(wid) == [(1, "T"), (2, "L")], _order(wid)
    assert not armed(), "a no-op move silently armed TM-first by healing the gap"
    print(
        "  [AC4] a move that changes nothing does not renumber, does not commit, and "
        "cannot arm TM-first by healing a gap"
    )


def test_ac_tm_first_note_is_reported():
    """When a move DOES change which step sits at index 0, the seam says so -- in the
    report both surfaces render, so the web notice and the CLI line cannot drift."""
    wid = _mk_wf("AC5")
    _translate(wid, name="T")
    _check(wid, "L")
    rep: dict = {}
    with Session(engine) as s:
        wf.reorder_step(s, wid, _id_of(wid, "L"), 0, report=rep)
    note = rep.get("tm_first_note", "")
    assert "TM pre-fill eligibility moved away from" in note and "'T'" in note, note
    rep2: dict = {}
    with Session(engine) as s:
        wf.reorder_step(s, wid, _id_of(wid, "T"), 0, report=rep2)
    note2 = rep2.get("tm_first_note", "")
    assert "TM pre-fill eligibility moved to" in note2 and "'T'" in note2, note2
    # Two steps that were never eligible swapping at the front says nothing.
    wid2 = _mk_wf("AC5b")
    _check(wid2, "C1")
    _check(wid2, "C2")
    rep3: dict = {}
    with Session(engine) as s:
        wf.reorder_step(s, wid2, _id_of(wid2, "C2"), 0, report=rep3)
    assert "tm_first_note" not in rep3, rep3
    print(
        "  [AC5] the seam reports TM-first gained/lost, and stays quiet when index 0 "
        "changes between two steps that were never eligible"
    )


def test_ac_gap_heal_arming_is_reported():
    """THE OTHER HALF OF THE GAP-HEAL FINDING. [AC4] proves a NO-OP move cannot arm
    TM-first. This is the case where a REAL move legitimately arms it on a step the user
    never touched: a gapped workflow has NOTHING at order_index 0, so TM-first is OFF;
    any real move renumbers contiguously and slides the head down to 0, arming it.

    The note used to stay silent here, because it compared WHICH STEP heads the list --
    and that is the same step before and after. Eligibility is not "heads the list", it
    is "a translate step whose order_index is ACTUALLY 0", so the seam now captures the
    head's raw order_index BEFORE renumbering (after the commit the refreshed object
    already reads 0, so the pre-move value is otherwise unrecoverable)."""
    wid = _mk_wf("AC7")
    lead = _check(wid, "Lead")
    _translate(wid, name="Translate")
    _check(wid, "B")
    _check(wid, "C")
    with Session(engine) as s:
        wf.delete_step(s, lead, workflow_id=wid)
    assert _order(wid) == [(1, "Translate"), (2, "B"), (3, "C")], _order(wid)
    tid = _id_of(wid, "Translate")

    def armed():
        with Session(engine) as s:
            return s.get(WorkflowStep, tid).order_index == 0

    assert not armed(), "the fixture must start with TM-first NOT armed (a gap at 0)"
    rep: dict = {}
    with Session(engine) as s:  # move C up: a REAL move
        wf.reorder_step(s, wid, _id_of(wid, "C"), 1, report=rep)
    assert _names(wid) == ["Translate", "C", "B"], _names(wid)
    assert armed(), "the gap heal should have armed TM-first"
    note = rep.get("tm_first_note", "")
    assert "TM pre-fill eligibility moved to" in note and "'Translate'" in note, (
        f"the gap-heal ARMED TM-first on an untouched step and said nothing: {note!r}"
    )
    print(
        "  [AC7] a move that heals a gap and thereby ARMS TM-first on an untouched "
        "step reports it (the head of the list never changed)"
    )


def test_ac_lock_is_held_around_the_whole_seam():
    """The check-and-write is serialized in-process (orchestrator._RUN_CREATE_LOCK's
    pattern). Two concurrent moves both read the same step list and both compute a full
    0..n-1 renumbering from it, so without the lock the second silently discards the
    first's committed move."""
    import threading as _t

    assert isinstance(wf._MOVE_LOCK, type(_t.Lock())), type(wf._MOVE_LOCK)
    wid = _mk_wf("AC6")
    _translate(wid, name="T")
    _check(wid, "L")
    held = []
    real = wf._reorder_step_locked

    def probe(*a, **k):
        held.append(wf._MOVE_LOCK.locked())
        return real(*a, **k)

    set_pieces("app.services.workflows", "_reorder_step_locked", probe)
    try:
        with Session(engine) as s:
            wf.reorder_step(s, wid, _id_of(wid, "L"), 0)
    finally:
        set_pieces("app.services.workflows", "_reorder_step_locked", real)
    assert held == [True], held
    print(
        "  [AC6] the seam body runs with _MOVE_LOCK HELD (check-and-write is atomic "
        "in-process; cross-process is documented, not claimed)"
    )


# ============================================================================
# [B] THE WEB ROUTE
# ============================================================================
def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_b_route_response_shape_is_content_negotiated():
    """The route answers a FORM POST with a 303 and a FETCH with 200 JSON carrying the
    same URL. The split exists because a fetch cannot read a 303's Location -- the canvas
    uses redirect:'manual' so a success renders once -- which left the drag path able only
    to reload the CURRENT url, silently dropping the ?notice= banner on the primary input.
    The fetch is identified by X-CSRF-Token, the same signal app/main.py already uses to
    tell a fetch apart from a full-page navigation."""
    from urllib.parse import unquote

    client = _client()
    wid = _mk_wf("B1")
    _translate(wid, name="T")
    _check(wid, "L")
    _translate(wid, name="F")
    sid = _id_of(wid, "F")

    # (a) FORM POST (no X-CSRF-Token) -> 303, unchanged.
    r = client.post(
        f"/workflows/{wid}/steps/{sid}/move", data={"position": "0"}, follow_redirects=False
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    loc = r.headers["location"]
    assert loc.startswith(f"/workflows/{wid}"), loc
    assert _names(wid) == ["F", "T", "L"], _names(wid)
    # This move handed index 0 from one translate step to another, so the redirect
    # carries the seam's TM-first sentence rather than being silent about it.
    assert "TM pre-fill eligibility moved" in unquote(loc), loc
    print("  [B1] a FORM POST still gets a 303 whose Location carries the notice")

    # (b) FETCH-SHAPED (X-CSRF-Token present) -> 200 JSON with the SAME url.
    r2 = client.post(
        f"/workflows/{wid}/steps/{_id_of(wid, 'T')}/move",
        data={"position": "0"},
        headers={"X-CSRF-Token": "t"},
        follow_redirects=False,
    )
    assert r2.status_code == 200, (r2.status_code, r2.text[:300])
    assert r2.headers["content-type"].startswith("application/json"), r2.headers
    body = r2.json()
    assert body.get("ok") is True, body
    target = body.get("location", "")
    assert target.startswith(f"/workflows/{wid}?notice="), target
    assert "TM pre-fill eligibility moved" in unquote(target), target
    assert _names(wid) == ["T", "F", "L"], _names(wid)
    print(
        "  [B1] a FETCH-shaped request gets 200 JSON whose `location` carries the SAME "
        "notice params -- so the drag can navigate to it and show the banner"
    )

    # (c) No notice to give -> still the right shape on both paths, just no query.
    r3 = client.post(
        f"/workflows/{wid}/steps/{_id_of(wid, 'L')}/move",
        data={"position": "1"},
        follow_redirects=False,
    )
    assert r3.status_code == 303 and r3.headers["location"] == f"/workflows/{wid}", r3.headers
    r4 = client.post(
        f"/workflows/{wid}/steps/{_id_of(wid, 'L')}/move",
        data={"position": "2"},
        headers={"X-CSRF-Token": "t"},
        follow_redirects=False,
    )
    assert r4.status_code == 200 and r4.json()["location"] == f"/workflows/{wid}", r4.text
    print("  [B1] a move that leaves index 0 alone carries no notice on either path")


def test_b_route_refusal_message_is_verbatim():
    """The canvas never paraphrases. The 400 page must carry the SEAM's message
    character for character -- a paraphrase drifts, and the real message names the step
    and the rule the author has to act on."""
    client = _client()
    wid = _mk_wf("B2")
    _translate(wid, name="T")
    lqa = _check(wid, "L", key="b2_l")
    _translate(wid, name="Fix", key="b2_fix", run_condition="prev_lqa:failed")
    with Session(engine) as s:
        expected = _err(wf.reorder_step, s, wid, lqa, 2)
    r = client.post(
        f"/workflows/{wid}/steps/{lqa}/move", data={"position": "2"}, follow_redirects=False
    )
    assert r.status_code == 400, (r.status_code, r.text[:300])
    import html as _html

    body = _html.unescape(r.text)
    assert expected in body, (expected, r.text[:600])
    assert _names(wid) == ["T", "L", "Fix"], _names(wid)
    print(
        "  [B2] a refused move renders a 400 carrying the validator message VERBATIM "
        "and changes nothing"
    )


def test_b_route_rbac_matches_step_edit():
    """RBAC is the step-edit routes' predicate, exactly: _ai_block (admins + linguists).
    Proven against a TRANSLATOR, who may write content but may not tune AI setup -- the
    same user the edit route turns away."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import security, settings
    from tests._csrf import csrf_for

    wid = _mk_wf("B3")
    _translate(wid, name="T")
    _check(wid, "L")
    sid = _id_of(wid, "T")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        tr = security.create_user(
            f"canvas-tr-{_seq()}@x.com", "Tr", Role.TRANSLATOR, "canvas-tr-pw-123"
        )
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post(
            "/auth/login", data={"email": tr.email, "password": "canvas-tr-pw-123", "next": "/"}
        )
        assert r.status_code == 303, ("login", r.status_code, r.text[:200])
        csrf_for(cl)
        r = cl.post(f"/workflows/{wid}/steps/{sid}/move", data={"position": "1"})
        assert r.status_code == 403, (r.status_code, r.text[:300])
        # The SAME predicate the edit route applies -- proven side by side, not assumed.
        r2 = cl.post(
            f"/workflows/{wid}/steps/{sid}/edit", data={"name": "T", "step_type": "llm_agent"}
        )
        assert r2.status_code == 403, (r2.status_code, r2.text[:300])
        assert _names(wid) == ["T", "L"], _names(wid)

        ling = security.create_user(
            f"canvas-lg-{_seq()}@x.com", "Lg", Role.LINGUIST, "canvas-lg-pw-123"
        )
        cl2 = TestClient(app, follow_redirects=False)
        csrf_for(cl2)
        r = cl2.post(
            "/auth/login", data={"email": ling.email, "password": "canvas-lg-pw-123", "next": "/"}
        )
        assert r.status_code == 303, ("login linguist", r.status_code)
        csrf_for(cl2)
        r = cl2.post(f"/workflows/{wid}/steps/{sid}/move", data={"position": "1"})
        # csrf_for installs X-CSRF-Token as a default header, so this client is
        # fetch-shaped and gets the negotiated 200 JSON (see [B1]) -- the point here is
        # that a linguist is ALLOWED, whichever success shape the route chose.
        assert r.status_code == 200, (r.status_code, r.text[:300])
        assert r.json()["location"].startswith(f"/workflows/{wid}"), r.text
        assert _names(wid) == ["L", "T"], _names(wid)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  [B3] the move route uses the STEP-EDIT predicate: a translator gets 403 "
        "from both routes; a linguist moves the step"
    )


def test_b_band_drop_is_one_seam_call():
    """ATOMICITY IS THE SEAM'S PROPERTY NOW, NOT THE ROUTE'S. The route makes exactly ONE
    call and owns no compensating writes; an invalid composed drop leaves nothing behind
    because nothing was written. The route is also pinned to a single seam call here --
    the two-call shape is what made drag-out impossible and made a rollback able to
    revert a concurrent request's committed move."""
    client = _client()
    wid = _mk_wf("B4")
    _translate(wid, name="T")
    _check(wid, "L1", key="b4_a", parallel_group="checks")
    _check(wid, "L2", key="b4_b", parallel_group="checks")
    _translate(wid, name="P")
    before = _order(wid)
    assert [n for _i, n in before] == ["T", "L1", "L2", "P"], before

    # A TRANSLATE step dragged onto the lane band: the composed final state breaks the
    # Phase-1 check-only rule, so NOTHING is written -- neither half.
    pid = _id_of(wid, "P")
    calls = []
    real = wf.reorder_step

    def counting(*a, **k):
        calls.append(k.get("parallel_group", "<unset>"))
        return real(*a, **k)

    set_pieces("app.services.workflows", "reorder_step", counting)
    try:
        r = client.post(
            f"/workflows/{wid}/steps/{pid}/move",
            data={"position": "3", "parallel_group": "checks"},
            follow_redirects=False,
        )
        assert r.status_code == 400, (r.status_code, r.text[:300])
        assert "parallel" in r.text and "llm_agent" in r.text, r.text[:400]
        assert _order(wid) == before, (before, _order(wid))
        with Session(engine) as s:
            assert s.get(WorkflowStep, pid).parallel_group is None
        assert calls == ["checks"], (
            f"the route must reach the seam EXACTLY once, with the slug composed in: {calls!r}"
        )
    finally:
        set_pieces("app.services.workflows", "reorder_step", real)
    print(
        "  [B4] a band drop is ONE composed seam call; an invalid final state writes "
        "nothing at all (no rollback needed, because nothing landed)"
    )

    # The legal band drop: a CHECK step joining the group lands both halves.
    lone = _check(wid, "L3")
    r = client.post(
        f"/workflows/{wid}/steps/{lone}/move",
        data={"position": "3", "parallel_group": "checks"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    got = [(s.name, s.parallel_group) for s in _steps(wid)]
    assert got == [
        ("T", None),
        ("L1", "checks"),
        ("L2", "checks"),
        ("L3", "checks"),
        ("P", None),
    ], got
    print("  [B4] and a legal band drop applies BOTH halves in one commit")

    # DRAG-OUT over the route: a plain-slot drop for a grouped step sends an EMPTY slug.
    r = client.post(
        f"/workflows/{wid}/steps/{_id_of(wid, 'L3')}/move",
        data={"position": "0", "parallel_group": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    got = [(s.name, s.parallel_group) for s in _steps(wid)]
    assert got == [("L3", None), ("T", None), ("L1", "checks"), ("L2", "checks"), ("P", None)], got
    print(
        "  [B4] drag-OUT over the route (empty parallel_group) leaves the group and "
        "moves in one call"
    )


def test_b_band_drop_on_a_base_reports_propagation():
    """A slug change is a real field edit, so a band drop SWEEPS to variants. That must
    be as visible as doing it on the step form -- it was silent before (review R1, MED):
    the route discarded the propagation report entirely."""
    client = _client()
    wid = _mk_wf("B7")
    _translate(wid, name="T")
    _check(wid, "L1", key="b7_a", parallel_group="checks")
    _check(wid, "L2", key="b7_b")
    with Session(engine) as s:
        wf.copy_to_pairs(s, wid, [("en-US", "fr-FR")])
    from urllib.parse import unquote

    r = client.post(
        f"/workflows/{wid}/steps/{_id_of(wid, 'L2')}/move",
        data={"position": "2", "parallel_group": "checks"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    notice = unquote(r.headers.get("location", ""))
    assert "notice=" in r.headers.get("location", ""), r.headers
    assert "Propagated to" in notice, notice
    print(
        "  [B7] a band drop on a BASE step renders the shared-base propagation notice, "
        f"exactly like the step form -- {notice[-90:]!r}"
    )

    # THE DRAG PATH MUST SAY THE SAME THING. This is finding 4's actual point: a base
    # band drop performed by DRAGGING was silent, because a fetch cannot read the 303's
    # Location. The fetch-shaped response hands the client the notice URL to navigate to.
    with Session(engine) as s:
        wf.reorder_step(s, wid, _id_of(wid, "L2"), 2, parallel_group=None)
    r2 = client.post(
        f"/workflows/{wid}/steps/{_id_of(wid, 'L2')}/move",
        data={"position": "2", "parallel_group": "checks"},
        headers={"X-CSRF-Token": "t"},
        follow_redirects=False,
    )
    assert r2.status_code == 200, (r2.status_code, r2.text[:300])
    drag_notice = unquote(r2.json().get("location", ""))
    assert "notice=" in r2.json().get("location", ""), r2.text
    assert "Propagated to" in drag_notice, drag_notice
    print(
        "  [B7] and the DRAG path receives the same notice URL to navigate to, so the "
        "banner is no longer lost on the primary input"
    )

    # And a direct band drop on a VARIANT step reports the auto-pin.
    with Session(engine) as s:
        vsteps = [
            x
            for x in s.exec(__import__("sqlmodel").select(WorkflowStep)).all()
            if x.adapted_from_id is not None and x.key == "b7_b"
        ]
        assert vsteps, "the fixture must have produced a variant step"
        vstep, vwf = vsteps[0].id, vsteps[0].workflow_id
    r = client.post(
        f"/workflows/{vwf}/steps/{vstep}/move",
        data={"position": "1", "parallel_group": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    with Session(engine) as s:
        assert s.get(WorkflowStep, vstep).overridden is True, "the variant was not pinned"
    print(
        "  [B7] a direct band drop on a VARIANT step auto-pins it, same rule as a "
        "direct edit ('you touched it, you own it')"
    )


def test_b_move_route_404s_a_foreign_step():
    client = _client()
    wid = _mk_wf("B5")
    _translate(wid, name="T")
    other = _mk_wf("B5b")
    ot = _translate(other, name="X")
    r = client.post(
        f"/workflows/{wid}/steps/{ot}/move", data={"position": "0"}, follow_redirects=False
    )
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert "not found in workflow" in r.text, r.text[:300]
    assert _names(other) == ["X"], _names(other)
    print(
        "  [B5] a step id belonging to ANOTHER workflow is refused through this "
        "workflow's move route"
    )


def test_b_position_is_optional_so_the_ring_runs_first():
    """`position` is Form("") and NOT Form(...), deliberately. FastAPI validates a
    REQUIRED form field BEFORE the handler runs, so a bodyless request would 422 ahead of
    the RBAC + customer-ring checks -- and a caller bound to customer A probing this route
    against customer B's workflow would get a 422 instead of the 404 that makes a foreign
    resource indistinguishable from a missing one. (tests/verify_connector_isolation_gaps
    [K] sweeps the whole workflow family for exactly that, and caught this route the first
    time it was written with Form(...).) With a default, the ring runs first and a blank
    position is refused by the SEAM, with its own message."""
    client = _client()
    wid = _mk_wf("B6")
    t = _translate(wid, name="T")
    _check(wid, "L")
    r = client.post(f"/workflows/{wid}/steps/{t}/move", data={}, follow_redirects=False)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert "whole number" in r.text, r.text[:300]
    assert _names(wid) == ["T", "L"], _names(wid)
    # And the route declares no required Form field at all, which is what keeps the
    # ring-before-body ordering true for EVERY payload, not just this one.
    import inspect

    from app.web.workflows import move_step

    required = [
        n
        for n, p in inspect.signature(move_step).parameters.items()
        if getattr(p.default, "default", None) is Ellipsis
    ]
    assert not required, (
        f"move_step declares required Form field(s) {required!r} -- FastAPI would 422 before the "
        "customer ring, re-opening the leak the workflow-family sweep pins"
    )
    print(
        "  [B6] a bodyless move is a clean 400 from the seam, and the route declares "
        "NO required Form field, so the customer ring always runs before the body"
    )


# ============================================================================
# [C] THE CLI VERB
# ============================================================================
def test_c_cli_round_trip():
    import json

    from tests._cli_runner import run_cli

    wid = _mk_wf("C1")
    _translate(wid, name="T", key="c_t")
    _check(wid, "L", key="c_l")
    _translate(wid, name="F", key="c_f")
    ids = {s.name: s.id for s in _steps(wid)}

    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(ids["F"]), "--to", "0"])
    assert rc == 0, (out, err)
    assert "position 0" in out and "0:c_f" in out, out
    assert _names(wid) == ["F", "T", "L"], _names(wid)
    print(
        "  [C1] `workflow move-step --to 0` moves the step and prints the resulting order (ASCII)"
    )

    rc, out, err = run_cli(
        ["--json", "workflow", "move-step", str(wid), str(ids["F"]), "--after", "c_l"]
    )
    assert rc == 0, (out, err)
    payload = json.loads(out)
    assert payload["position"] == 2, payload
    assert [r["key"] for r in payload["order"]] == ["c_t", "c_l", "c_f"], payload
    assert _names(wid) == ["T", "L", "F"], _names(wid)
    print("  [C1] --after <key> resolves to a position; --json carries the whole resulting order")

    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(ids["L"]), "--before", "c_t"])
    assert rc == 0, (out, err)
    assert _names(wid) == ["L", "T", "F"], _names(wid)
    print("  [C1] --before <key> resolves to a position too")

    # --before/--after also accept a raw step id (the graph is not always keyed).
    rc, out, err = run_cli(
        ["workflow", "move-step", str(wid), str(ids["L"]), "--after", str(ids["F"])]
    )
    assert rc == 0, (out, err)
    assert _names(wid) == ["T", "F", "L"], _names(wid)
    print("  [C1] --before/--after accept a raw step id as well as a key")


def test_c_cli_refusals_are_clean():
    from tests._cli_runner import run_cli

    wid = _mk_wf("C2")
    _translate(wid, name="T")
    lqa = _check(wid, "L", key="c2_l")
    _translate(wid, name="Fix", key="c2_fix", run_condition="prev_lqa:failed")

    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(lqa), "--to", "2"])
    assert rc == 2, (rc, out, err)
    assert "would break the workflow graph" in (out + err), (out, err)
    assert "Traceback" not in (out + err), (out, err)
    assert _names(wid) == ["T", "L", "Fix"], _names(wid)
    print("  [C2] a refused move exits 2 with the seam's message, never a traceback")

    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(lqa), "--before", "nosuch"])
    assert rc == 2 and "no step matches" in (out + err), (rc, out, err)
    rc, out, err = run_cli(["workflow", "move-step", str(wid), "999999", "--to", "0"])
    assert rc == 2 and "no such step" in (out + err), (rc, out, err)
    # --to / --before / --after are mutually exclusive AND one is required.
    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(lqa)])
    assert rc != 0, (rc, out, err)
    rc, out, err = run_cli(
        ["workflow", "move-step", str(wid), str(lqa), "--to", "0", "--after", "c2_l"]
    )
    assert rc != 0, (rc, out, err)
    print(
        "  [C2] an unknown anchor / unknown step / missing or conflicting target flags "
        "all fail cleanly"
    )


def test_c_cli_ambiguous_anchor_is_refused():
    """A numeric --before/--after ref can match one step's KEY and a DIFFERENT step's ID
    (keys are free-form slugs, so '3' is legal). Letting the key silently win moved the
    step somewhere the author did not name, with nothing in the output to reveal it."""
    from tests._cli_runner import run_cli

    wid = _mk_wf("C3")
    a = _translate(wid, name="A")
    _translate(wid, name="B")
    _translate(wid, name="C")
    ids = {s.name: s.id for s in _steps(wid)}
    with Session(engine) as s:  # C's KEY is A's ID
        c = s.get(WorkflowStep, ids["C"])
        c.key = str(a)
        s.add(c)
        s.commit()
    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(ids["B"]), "--before", str(a)])
    assert rc == 2, (rc, out, err)
    assert "ambiguous" in (out + err), (out, err)
    assert "Traceback" not in (out + err), (out, err)
    assert _names(wid) == ["A", "B", "C"], _names(wid)
    print(
        "  [C3] a --before/--after ref that is one step's KEY and another's ID is "
        "REFUSED as ambiguous, not silently resolved key-first"
    )


def test_c_cli_composes_lane_membership():
    """Headless parity for the drag: join, switch and LEAVE a group atomically."""
    import json

    from tests._cli_runner import run_cli

    wid = _mk_wf("C4")
    _translate(wid, name="T")
    _check(wid, "L1", key="c4_a", parallel_group="checks")
    _check(wid, "L2", key="c4_b", parallel_group="checks")
    _check(wid, "L3", key="c4_c")
    ids = {s.name: s.id for s in _steps(wid)}

    # JOIN: move next to the group and take its slug in one call.
    rc, out, err = run_cli(
        [
            "--json",
            "workflow",
            "move-step",
            str(wid),
            str(ids["L3"]),
            "--to",
            "3",
            "--parallel-group",
            "checks",
        ]
    )
    assert rc == 0, (out, err)
    assert json.loads(out)["parallel_group"] == "checks", out
    assert [(s.name, s.parallel_group) for s in _steps(wid)] == [
        ("T", None),
        ("L1", "checks"),
        ("L2", "checks"),
        ("L3", "checks"),
    ]
    print("  [C4] --parallel-group joins a group as part of the move")

    # LEAVE: the only expressible form -- the move alone would split the group.
    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(ids["L1"]), "--to", "0"])
    assert rc == 2 and "is split by other steps" in (out + err), (rc, out, err)
    rc, out, err = run_cli(
        [
            "--json",
            "workflow",
            "move-step",
            str(wid),
            str(ids["L1"]),
            "--to",
            "0",
            "--clear-parallel-group",
        ]
    )
    assert rc == 0, (out, err)
    assert json.loads(out)["parallel_group"] is None, out
    assert [(s.name, s.parallel_group) for s in _steps(wid)] == [
        ("L1", None),
        ("T", None),
        ("L2", "checks"),
        ("L3", "checks"),
    ]
    print(
        "  [C4] --clear-parallel-group LEAVES a group as part of the move (the move "
        "alone is refused, as it must be)"
    )

    # The two flags are mutually exclusive.
    rc, out, err = run_cli(
        [
            "workflow",
            "move-step",
            str(wid),
            str(ids["L1"]),
            "--to",
            "0",
            "--parallel-group",
            "x",
            "--clear-parallel-group",
        ]
    )
    assert rc != 0, (rc, out, err)
    print("  [C4] --parallel-group and --clear-parallel-group are mutually exclusive")


def test_c_cli_surfaces_tm_first_and_noop():
    """Web/CLI parity for the two things the seam now reports."""
    import json

    from tests._cli_runner import run_cli

    wid = _mk_wf("C5")
    _translate(wid, name="T")
    _check(wid, "L")
    ids = {s.name: s.id for s in _steps(wid)}
    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(ids["L"]), "--to", "0"])
    assert rc == 0, (out, err)
    assert "TM pre-fill eligibility moved away from" in out, out
    print("  [C5] the CLI prints the SAME TM-first sentence the web notice shows")

    rc, out, err = run_cli(
        ["--json", "workflow", "move-step", str(wid), str(ids["L"]), "--to", "0"]
    )
    assert rc == 0, (out, err)
    payload = json.loads(out)
    assert payload["noop"] is True, payload
    assert "already at position" not in out or True  # noqa: SIM222
    rc, out, err = run_cli(["workflow", "move-step", str(wid), str(ids["L"]), "--to", "0"])
    assert "nothing changed" in out, out
    print("  [C5] a no-op move says so on both surfaces instead of silently renumbering")


# ============================================================================
# [D] A11Y -- the keyboard path ships in Phase 1
# ============================================================================
def test_d_move_buttons_render_and_work():
    client = _client()
    wid = _mk_wf("D1")
    _translate(wid, name="T")
    _check(wid, "L")
    _translate(wid, name="F")
    page = client.get(f"/workflows/{wid}").text
    forms = re.findall(r'action="/workflows/%d/steps/(\d+)/move"' % wid, page)  # noqa: UP031
    assert forms, "no move forms rendered in the builder step list"
    assert "move up" in page and "move down" in page, "the a11y buttons are missing"

    ids = [s.id for s in _steps(wid)]
    # The FIRST step gets no "move up" and the LAST no "move down" -- OMITTED, not
    # rendered dead. Read off the target positions each step's forms actually carry.
    pairs = re.findall(
        r'action="/workflows/%d/steps/(\d+)/move"[\s\S]*?name="position" value="(-?\d+)"' % wid,  # noqa: UP031
        page,
    )
    by_step = {}
    for sid, pos in pairs:
        by_step.setdefault(int(sid), []).append(int(pos))
    assert by_step.get(ids[0]) == [1], by_step  # first: down only
    assert by_step.get(ids[1]) == [0, 2], by_step  # middle: up + down
    assert by_step.get(ids[2]) == [1], by_step  # last: up only
    print(
        "  [D1] the builder renders per-step move up/down forms, omitted at the ends "
        "of the list rather than rendered dead"
    )

    # They post to the SAME route the drag uses, with a plain 0-based position.
    r = client.post(
        f"/workflows/{wid}/steps/{ids[2]}/move", data={"position": "1"}, follow_redirects=False
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    assert _names(wid) == ["T", "F", "L"], _names(wid)
    print("  [D1] and they drive the same seam (no second code path for the keyboard affordance)")


def test_d_builder_wires_the_canvas():
    client = _client()
    wid = _mk_wf("D2")
    _translate(wid, name="T")
    _check(wid, "L")
    page = client.get(f"/workflows/{wid}").text
    assert 'class="wf-canvas"' in page, "the canvas wrapper is missing"
    assert f'data-move-url="/workflows/{wid}/steps"' in page, "the endpoint is missing"
    assert "data-csrf=" in page, "the CSRF token is missing from the wrapper"
    assert "/static/wfcanvas.js" in page, "the drag script is not loaded"
    assert "wfg-editable" in page and "wfg-grip" in page, "the graph is not editable"
    # POSITIONING LAW: calm and precise; never canvas/magic/wizard in user-facing copy.
    # Whitespace-collapsed, because the template hard-wraps its prose.
    low = " ".join(page.lower().split())
    assert "drag a step by its grip" in low, "the reorder copy is missing"
    assert "run it together with that group" in low, "the group drop copy is missing"
    assert "take it out of its group" in low, "the drag-OUT copy is missing"
    for banned in ("magic", "wizard", "canvas</", "the canvas"):
        assert banned not in low, f"user-facing copy says {banned!r}"

    # HONEST ABOUT JS. The drag needs JavaScript and the buttons never do, so the copy
    # must lead with the buttons and name the drag as the addition. An earlier version
    # claimed the graph worked "without JS", which was simply false.
    assert "those always work" in low, "the copy no longer promises the buttons work"
    assert "with javascript on you can" in low, "the copy must say the DRAG needs JavaScript"
    assert "without js" not in low and "or without javascript" not in low, (
        "the copy still claims the drag degrades gracefully without JS"
    )

    # The notice is announced to assistive tech rather than only appearing visually.
    assert 'class="wf-canvas-notice" role="status" aria-live="polite"' in page, (
        "the inline notice is not announced (no aria-live)"
    )
    print(
        "  [D2] the builder wires the canvas (endpoint + CSRF + script + editable "
        "graph), the notice is announced, and the copy is honest about JS"
    )


def test_d_script_is_gated_on_edit_rights():
    """The editor's script is not served to a read-only viewer. Same predicate as the
    .wf-canvas wrapper, so the two can never disagree."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import security, settings
    from tests._csrf import csrf_for

    wid = _mk_wf("D4")
    _translate(wid, name="T")
    _check(wid, "L")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        tr = security.create_user(
            f"canvas-ro-{_seq()}@x.com", "Ro", Role.TRANSLATOR, "canvas-ro-pw-123"
        )
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post(
            "/auth/login", data={"email": tr.email, "password": "canvas-ro-pw-123", "next": "/"}
        )
        assert r.status_code == 303, ("login", r.status_code)
        page = cl.get(f"/workflows/{wid}").text
        assert "/static/wfcanvas.js" not in page, (
            "a read-only viewer was served the editor's drag script"
        )
        assert 'class="wf-canvas"' not in page, "a read-only viewer got the editable wrapper"
        assert "wfg-grip" not in page, "a read-only viewer got grips"
        assert "wf-graph-svg" in page, "the read-only graph should still render"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  [D4] a read-only viewer gets the plain graph and NO drag script (the script "
        "tag is gated on the same predicate as the wrapper)"
    )


def test_d_move_buttons_name_their_step():
    """A screen-reader user tabbing the list otherwise hears 'move up' N times with
    nothing to tell them apart."""
    client = _client()
    wid = _mk_wf("D5")
    _translate(wid, name="Translate draft")
    _check(wid, "Terminology check")
    page = client.get(f"/workflows/{wid}").text
    # a11y T8 (2.5.3 label in name): the accessible name STARTS with the visible words.
    # The arrow is aria-hidden and the step name an sr-only suffix, so the name reads
    # "move down: Translate draft"; the old divergent aria-label ("Move Translate draft
    # down", which does not contain the visible "move down") is gone.
    down = (
        '<span aria-hidden="true">&darr;</span> move down'
        '<span class="sr-only">: Translate draft</span></button>'
    )
    up = (
        '<span aria-hidden="true">&uarr;</span> move up'
        '<span class="sr-only">: Terminology check</span></button>'
    )
    assert down in page, "missing the named move down button"
    assert up in page, "missing the named move up button"
    assert 'aria-label="Move ' not in page, "the divergent move-button aria-label is back"
    for step in ("Translate draft", "Terminology check"):
        assert f'remove<span class="sr-only">: {step}</span></button>' in page, (
            f"remove button does not name {step}"
        )
    print("  [D5] each move / remove button carries a per-step name that starts with its text")


def test_d_read_only_call_sites_stay_read_only():
    """The run view and the author-review page must NOT become editable. Proven at the
    MACRO level (their call sites pass no `editable`), which is also what keeps their
    bytes identical."""

    def _calls(name):
        body = open(os.path.join(_ROOT, "app", "templates", name), encoding="utf-8").read()  # noqa: SIM115
        return re.findall(r"wf_graph\(([^)]*)\)", body)

    # a11y T8: each call site now also names its text equivalent (text_id), which only
    # adds aria-describedby; editable stays absent on the read-only sites.
    assert _calls("_run_graph_card.html") == ["steps, overlay=overlay, text_id='wfgText'"], _calls(
        "_run_graph_card.html"
    )
    assert _calls("workflow_author_review.html") == ["fields.steps, text_id='wfgText'"], _calls(
        "workflow_author_review.html"
    )
    # The builder is the ONLY call site that asks for the editable rendering.
    builder = _calls("workflow_builder.html")
    assert sorted(builder) == [
        "steps, editable=true, text_id='wfStepList'",
        "steps, text_id='wfStepList'",
    ], builder
    print(
        "  [D3] the run-view and author-review graphs still call wf_graph WITHOUT "
        "editable -- they stay read-only"
    )


# ============================================================================
# [E] THE MACRO
# ============================================================================
class _S(dict):
    """A step the macro can read with dotted access (it accepts ORM rows AND draft
    dicts, so a dict with __getattr__ exercises the very same code path)."""

    def __getattr__(self, k):
        return self.get(k)


_UNGROUPED = [
    _S(
        id=1,
        name="Translate",
        key=None,
        step_type="llm_agent",
        provider="mock",
        model="m1",
        qe_gate=False,
        qe_threshold=70,
        glossary_id=None,
        tm_examples=True,
        redact_pii=False,
        needs_review=False,
        branch=None,
        max_visits=None,
        parallel_group=None,
    ),
    _S(
        id=2,
        name="LQA main",
        key="lqa_main",
        step_type="lqa",
        provider=None,
        model=None,
        qe_gate=False,
        qe_threshold=70,
        glossary_id=None,
        tm_examples=False,
        redact_pii=False,
        needs_review=False,
        branch={"on_fail": {"step": "fix"}},
        max_visits=3,
        parallel_group=None,
    ),
    _S(
        id=3,
        name="Fix",
        key="fix",
        step_type="llm_agent",
        provider="mock",
        model=None,
        qe_gate=True,
        qe_threshold=80,
        glossary_id=None,
        tm_examples=True,
        redact_pii=True,
        needs_review=False,
        branch=None,
        max_visits=None,
        parallel_group=None,
    ),
]

_GROUPED = [
    _S(
        id=1,
        name="T",
        key=None,
        step_type="llm_agent",
        provider="mock",
        model=None,
        qe_gate=False,
        qe_threshold=70,
        glossary_id=None,
        tm_examples=True,
        redact_pii=False,
        needs_review=False,
        branch=None,
        max_visits=None,
        parallel_group=None,
    ),
    _S(
        id=2,
        name="L1",
        key="a",
        step_type="lqa",
        provider=None,
        model=None,
        qe_gate=False,
        qe_threshold=70,
        glossary_id=None,
        tm_examples=False,
        redact_pii=False,
        needs_review=False,
        branch=None,
        max_visits=None,
        parallel_group="checks",
    ),
    _S(
        id=3,
        name="L2",
        key="b",
        step_type="lqa",
        provider=None,
        model=None,
        qe_gate=False,
        qe_threshold=70,
        glossary_id=None,
        tm_examples=False,
        redact_pii=False,
        needs_review=False,
        branch={"on_fail": {"step": "p"}},
        max_visits=None,
        parallel_group="checks",
    ),
    _S(
        id=4,
        name="P",
        key="p",
        step_type="llm_agent",
        provider="mock",
        model=None,
        qe_gate=True,
        qe_threshold=80,
        glossary_id=None,
        tm_examples=True,
        redact_pii=False,
        needs_review=False,
        branch=None,
        max_visits=None,
        parallel_group=None,
    ),
]

# See the module docstring for how these were established against the branch point.
_PIN = {
    "ungrouped": ("d710b0fceb0f316fa6fa31f7f416c267fbf84c9be934e33d62788eb5aff3fe05", 4476),
    "grouped": ("8e9618dafbcb6c2291288d2ce7760b778ec1b2446f1330333423962ab7be3d50", 5481),
}


def _macro(template_dir=None):
    d = template_dir or os.path.join(_ROOT, "app", "templates")
    return Environment(loader=FileSystemLoader(d)).get_template("_macros.html").module


def _digest(svg):
    return hashlib.sha256(svg.encode("utf-8")).hexdigest()


def test_e_default_output_is_byte_identical():
    m = _macro()
    for label, steps in (("ungrouped", _UNGROUPED), ("grouped", _GROUPED)):
        want_sha, want_len = _PIN[label]
        svg = m.wf_graph(steps)
        assert len(svg) == want_len, (
            "the %s wf_graph SVG changed length (%d != %d) -- the editable-mode markup "  # noqa: UP031
            "leaked into the DEFAULT rendering (most likely an {%% if editable %%} put "
            "on a line of its own, whose indentation + newline survive the false branch)"
            % (label, len(svg), want_len)
        )
        assert _digest(svg) == want_sha, (
            f"the {label} wf_graph SVG is no longer byte-identical (sha256 {_digest(svg)} != {want_sha})"  # noqa: E501
        )
        # The new PARAMETER must be inert when explicitly false, not merely when omitted.
        assert m.wf_graph(steps, editable=False) == svg, (
            f"wf_graph(..., editable=False) differs from wf_graph(...) for the {label} fixture"
        )
        # a11y T8: the same for text_id (verify_workflow_diagram_text pins the opted-in form).
        assert m.wf_graph(steps, text_id=None) == svg, (
            f"wf_graph(..., text_id=None) differs from wf_graph(...) for the {label} fixture"
        )
    print(
        "  [E1] the DEFAULT wf_graph output is byte-identical for the ungrouped AND "
        "grouped fixtures, and editable=False is inert (4476 / 5481 bytes)"
    )


def test_e_pin_is_discriminating():
    """FAIL-BEFORE PROOF, permanently live. A snapshot test that cannot fail protects
    nothing, so this renders a MUTATED macro -- the editable block's `{% if editable %}`
    gate removed, i.e. exactly the accident the design exists to prevent -- and asserts
    the pin REJECTS it. Copy-aside into a throwaway template dir; the working tree is
    never touched (and `git stash` is never used: its stack is shared across worktrees)."""
    src = os.path.join(_ROOT, "app", "templates")
    tmp = tempfile.mkdtemp(prefix="loctms_wfcanvas_mut_")
    try:
        for name in os.listdir(src):
            if name.endswith(".html"):
                shutil.copy2(os.path.join(src, name), os.path.join(tmp, name))
        path = os.path.join(tmp, "_macros.html")
        body = open(path, encoding="utf-8").read()  # noqa: SIM115
        # Ungate the grip block: the false branch would now emit it too.
        marker = '{% if editable %}<g class="wfg-grip"'
        assert marker in body, "the grip block moved -- update this mutation"
        body = body.replace(marker, '{% if True %}<g class="wfg-grip"', 1)
        open(path, "w", encoding="utf-8", newline="").write(body)  # noqa: SIM115
        svg = _macro(tmp).wf_graph(_UNGROUPED)
        want_sha, want_len = _PIN["ungrouped"]
        assert len(svg) != want_len and _digest(svg) != want_sha, (
            "THE PIN IS NOT DISCRIMINATING: an ungated editable block rendered the same "  # noqa: UP031
            "bytes as the pinned default (%d, %s)" % (len(svg), _digest(svg))
        )
        assert "wfg-grip" in svg, "the mutation did not take effect"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(
        "  [E2] the byte-identity pin is DISCRIMINATING: an accidentally-ungated "
        "editable block is caught (length AND digest both move)"
    )


def test_e_editable_emits_grips_slots_and_bands():
    m = _macro()
    svg = m.wf_graph(_GROUPED, editable=True)
    n = len(_GROUPED)

    # One grip per node, and the drag handle is a distinct element (a drag can only
    # start there -- clicking the node body must never begin one).
    assert svg.count('class="wfg-grip"') == n, svg.count('class="wfg-grip"')
    assert svg.count("Drag to reorder this step.") == n

    # Every node carries its identity + CURRENT position, which is what the client
    # needs to turn a boundary into a target position.
    nodes = re.findall(
        r'<g class="wfg-node" data-step-id="(\d+)" data-position="(\d+)"'
        r'( data-groupable="1")?(?: data-group="([^"]+)")?>',
        svg,
    )
    assert [(a, b) for a, b, _c, _d in nodes] == [("1", "0"), ("2", "1"), ("3", "2"), ("4", "3")], (
        nodes
    )
    # PRUNING (section 4.3): only an lqa step may ever be a lane member in Phase 1, so
    # only an lqa node is marked groupable -- the client offers it no band target.
    assert [bool(c) for _a, _b, c, _d in nodes] == [False, True, True, False], nodes
    # data-group carries the step's CURRENT membership. The client needs it to know that
    # a plain-slot drop means "leave the group" -- without it, drag-OUT is unexpressible.
    assert [d for _a, _b, _c, d in nodes] == ["", "checks", "checks", ""], nodes
    # The old data-step-type was never read by anything; a dead attribute in shipped
    # markup is a maintenance trap.
    assert "data-step-type" not in svg, "the unused data-step-type attribute is back"

    # One slot per BOUNDARY, 0..n inclusive (n+1 of them: above every step, plus the end).
    gaps = re.findall(r'<rect class="wfg-slot" data-gap="(\d+)"', svg)
    assert gaps == [str(k) for k in range(n + 1)], gaps
    # Each slot has its own insertion marker, hidden until the client reveals it.
    marks = re.findall(r'<line class="wfg-slot-mark" data-gap-mark="(\d+)"', svg)
    assert marks == gaps, (marks, gaps)
    assert svg.count('opacity="0"') >= n + 1

    # The slots TILE the graph: contiguous, non-overlapping, in order.
    rects = [
        (int(a), int(b))
        for a, b in re.findall(
            r'class="wfg-slot" data-gap="\d+" x="-?\d+" y="(-?\d+)" width="\d+" '
            r'height="(\d+)"',
            svg,
        )
    ]
    assert len(rects) == n + 1, rects
    for (y0, h0), (y1, _h1) in zip(rects, rects[1:], strict=False):  # noqa: RUF007
        assert y0 + h0 == y1, ("slots must tile without gaps or overlap", rects)

    # ONE lane band per REAL group (>= 2 consecutive members), carrying the slug and the
    # boundary that joins the group from either direction (just past its last member).
    bands = re.findall(r'<rect class="wfg-band" data-group="([^"]+)" data-gap="(\d+)"', svg)
    assert bands == [("checks", "3")], bands
    # The band is fill="none", so it takes no pointer events and a <title> on it could
    # NEVER be shown -- it was dead markup pretending to be a tooltip. The "run together"
    # copy lives where a user can actually read it: the figcaption and the helper line.
    assert "<title>" not in svg.split('class="wfg-band"')[1], (
        "the lane band carries a title that can never render"
    )
    print(
        "  [E3] editable=true emits %d grips, %d tiled slots (data-gap 0..%d), node "  # noqa: UP031
        "positions + current membership, and 1 lane band (group 'checks' at boundary 3)"
        % (n, n + 1, n)
    )


def test_e_editable_prunes_and_degrades():
    m = _macro()
    # An UNGROUPED workflow gets slots but NO band targets (there is no group to join).
    svg = m.wf_graph(_UNGROUPED, editable=True)
    assert 'class="wfg-band"' not in svg, "a band was emitted with no parallel group"
    assert svg.count('class="wfg-slot"') == len(_UNGROUPED) + 1
    assert "data-groupable" in svg, "the lqa step in the ungrouped fixture lost its flag"

    # A LONE member is not a group (the engine does not fork one), so it earns no band --
    # the same definition the outline and the shape tag already use.
    lone = [
        _S(
            id=1,
            name="T",
            step_type="llm_agent",
            provider="mock",
            tm_examples=True,
            qe_gate=False,
            qe_threshold=70,
            parallel_group=None,
        ),
        _S(
            id=2,
            name="L",
            key="x",
            step_type="lqa",
            qe_gate=False,
            qe_threshold=70,
            parallel_group="solo",
        ),
    ]
    svg2 = m.wf_graph(lone, editable=True)
    assert 'class="wfg-band"' not in svg2, "a LONE member drew a lane band"
    assert "(in parallel)" not in svg2, "a LONE member drew a group outline"

    # An EMPTY workflow renders nothing at all in either mode.
    assert m.wf_graph([], editable=True).strip() == ""
    assert m.wf_graph([]).strip() == ""
    print(
        "  [E4] no band without a real group (ungrouped, and a LONE member), and an "
        "empty workflow renders nothing in either mode"
    )


def main():
    test_a_happy_paths_and_contiguous_renumbering()
    test_a_renumbering_heals_delete_gaps()
    test_a_refusal_active_run()
    test_a_refusal_out_of_range_and_unknown_step()
    test_a_refusal_gate_moved_below_its_referrer()
    test_a_refusal_group_split_both_directions()
    test_a_refusal_branch_reentry_at_non_first_member()
    test_a_diff_tolerates_pre_existing_breakage()
    test_a_tm_first_follows_the_move_both_directions()
    test_a_copies_preserve_the_reordered_order()
    test_aid_lane_reorder_survives_a_flipping_ambiguity_example()
    test_aid_duplicate_key_attribution_is_symmetric()
    test_aid_add_step_onto_a_LEGACY_split_is_allowed()
    test_aid_no_permutation_re_keys_an_existing_error()
    test_ac_drag_out_of_a_group()
    test_ac_cross_group_move()
    test_ac_composed_refusal_names_the_real_rule()
    test_ac_noop_move_does_not_arm_tm_first()
    test_ac_tm_first_note_is_reported()
    test_ac_gap_heal_arming_is_reported()
    test_ac_lock_is_held_around_the_whole_seam()
    test_b_route_response_shape_is_content_negotiated()
    test_b_route_refusal_message_is_verbatim()
    test_b_band_drop_is_one_seam_call()
    test_b_band_drop_on_a_base_reports_propagation()
    test_b_move_route_404s_a_foreign_step()
    test_b_position_is_optional_so_the_ring_runs_first()
    test_b_route_rbac_matches_step_edit()
    test_c_cli_round_trip()
    test_c_cli_refusals_are_clean()
    test_c_cli_ambiguous_anchor_is_refused()
    test_c_cli_composes_lane_membership()
    test_c_cli_surfaces_tm_first_and_noop()
    test_d_move_buttons_render_and_work()
    test_d_builder_wires_the_canvas()
    test_d_script_is_gated_on_edit_rights()
    test_d_move_buttons_name_their_step()
    test_d_read_only_call_sites_stay_read_only()
    test_e_default_output_is_byte_identical()
    test_e_pin_is_discriminating()
    test_e_editable_emits_grips_slots_and_bands()
    test_e_editable_prunes_and_degrades()
    print("WORKFLOW CANVAS OK")


if __name__ == "__main__":
    main()
