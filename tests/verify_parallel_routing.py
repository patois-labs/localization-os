"""PARALLEL STEP ROUTING, Phase 1 (SPEC-parallel-routing.md section 10).

Two or more CONSECUTIVE `lqa` steps that share a `parallel_group` slug run
CONCURRENTLY inside one run, against the SAME frozen snapshot of the working text,
and JOIN before the next step starts. This suite is the whole vertical: save-time
membership rules, the new `step:<key>` condition ref, the fork/join engine, the
concurrency hygiene the QA-1 post-mortem demands, and the surfaces.

GROUPS
  [A] SAVE-TIME MEMBERSHIP (section 5.1), each rule proven fail-closed with its message:
      consecutive members; check-only (lqa) members; at most one routing member; an
      outside branch may re-enter a group at its FIRST member only; a member's guard
      resolves against PRE-GROUP steps (so a lane cannot reference a sibling lane);
      slug validation; the before/after diff (pre-existing breakage never blocks an
      unrelated edit); dead-field handling per step type; both copy sites.
  [B] CONDITION REFS (section 5.5): a bare prev_lqa whose nearest producers are 2+
      members of one group is REFUSED at save time naming the group and the exact
      `step:` form to use; an unambiguous prev_lqa is untouched (zero migration); a
      `step:<key>` ref round-trips and resolves at run time to the NAMED lane, which is
      how a NON-routing lane's gate stays addressable.
  [C] EXECUTION (section 5.2 / 5.4): CONCURRENCY PROVEN WITH A BARRIER, never timing --
      a threading.Barrier(2) that both lanes must reach for either to return, so a
      sequential implementation deadlocks the barrier and fails instead of merely being
      slow; frozen-snapshot isolation; the ONE-COMMIT join with the StepRun id-order
      invariant asserted; an all-skip group as a clean no-op; a member failure failing
      the run only AFTER every lane persisted; loop-back re-entry bumping every member's
      visits; loop_cap_reached BEFORE any dispatch.
  [D] ROUTING AT THE JOIN (section 5.3): the routing member's branch routes; a
      non-routing member's gate does not; no routing member = fall-through past the
      group.
  [E] CONCURRENCY HYGIENE: the DBAPI in-transaction probe at every dispatch point
      (tests/verify_llmemory_dispatch_lock.py is the post-mortem template -- read its
      header); per-member spend on each member's StepRun; the prompt tap carrying the
      MEMBER's step_key; deterministic observer replay.
  [F] UNGROUPED BYTE-STABILITY, three independent proofs (see the note below).
  [G] SURFACES: CLI add-step/edit-step/--no-parallel-group round-trip + the `par`
      bracket column; the web builder's add + edit form round-trip + the group chip;
      wf_graph rendering a group; the `parallel-checks` shape tag.

HOW BYTE-STABILITY IS PROVEN (group [F]) -- three complementary, PERMANENTLY-LIVE
checks, because "byte-identical to before the change" is not a property a suite written
after the change can assert directly (the clone-parity suite makes the same argument: a
check against `git show HEAD:...` can never fail once merged, so it protects nothing):

  [F1] UNREACHABILITY. On an ungrouped run BOTH seam entry points
       (`_group_at`, `_run_parallel_group`) are replaced with functions that RAISE. The
       run completes normally, so no new code executed at all. This is the strongest of
       the three: unreached code cannot change behaviour.
  [F2] ARMED-VS-DISARMED EQUIVALENCE. The same ungrouped run is executed twice, once
       with `_has_parallel` forced True (so the added per-iteration `_group_at` call IS
       evaluated) and once forced False, and the two event streams, StepRun rows and
       written segment targets are compared field by field. This covers the one line
       [F1] cannot: that evaluating the gate changes nothing.
  [F3] wf_graph SVG SNAPSHOT. The rendered SVG of a fixed ungrouped 3-step workflow --
       including a loop-back branch curve, the geometry most at risk from a macro edit
       -- is pinned by sha256. Live protection for every FUTURE macro change.

  PRE-CHANGE EVIDENCE, captured manually at build time rather than encoded as a
  self-defeating HEAD check (both measurements below were taken by copying the branch
  point's file over the working tree, running, and copying the working tree's file back
  -- never `git stash`, whose stack is shared across worktrees):

    * ENGINE. The 3-step ungrouped workflow (translate -> LQA -> polish) over a 3-segment
      document was run against `git show 630d28d:app/services/orchestrator.py` (the
      branch point) and against the working tree, and the full canonical dump -- every
      event with every key, every StepRun row (status/input/output/error/score/gate/
      outcome_index/cost/tokens), every written Segment (target/status/qa_flags) and the
      run's own (status, current_step_index, context) -- was BYTE-IDENTICAL, 3914 bytes
      of JSON either way.
    * wf_graph. The macro was rendered from `git show
      630d28d:app/templates/_macros.html` and from the working tree for the identical
      ungrouped fixture below, and the two outputs were BYTE-IDENTICAL (4476 bytes each).
      The hash pinned below is that exact string.

  Neither measurement is re-encoded as an in-suite check, for the reason
  tests/verify_clone_field_parity.py states in full: a check against `git show HEAD:...`
  can never fail once merged, so it would provide no live protection. [F1]/[F2]/[F3] are
  the permanently-live protection.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env, provider
keys blanked -> no live provider is reachable; the LQA judge is stubbed at
`lqa.score_text`, the seam whose own docstring certifies it session-free). ASCII-only
output. Final line: "PARALLEL ROUTING OK".
"""

import hashlib
import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_parroute_")

from concurrent.futures import ThreadPoolExecutor

from jinja2 import Environment, FileSystemLoader
from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import lqa, orchestrator
from app.services import workflow_shapes as wshapes
from app.services import workflows as wf

init_db()

SOURCES = ["Hello world.", "The cat sat down.", "Please sign in."]
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# ============================================================================
# Fixtures
# ============================================================================
def _mk_project(name="PR", provider="mock"):
    with Session(engine) as s:
        p = Project(name=f"{name}-{_seq()}", source_locale="en-US", default_provider=provider)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(project_id, sources=None, locale="es-ES"):
    with Session(engine) as s:
        d = Document(project_id=project_id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(sources if sources is not None else SOURCES):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return d.id


def _mk_doc_fulltext(project_id, text="Hello world.", locale="es-ES"):
    """A whole-document (FULL_TEXT) doc: the source lives in the skeleton units the
    FULL_TEXT loop assembles, with one Segment to receive the write-back."""
    with Session(engine) as s:
        d = Document(
            project_id=project_id,
            filename="ft.docx",
            fmt="docx",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": [{"text": text}], "seg_locale": "en-US"},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale=locale,
                order_index=0,
                source_text=text,
                status=SegStatus.NEW,
            )
        )
        s.commit()
        return d.id


def _mk_wf(name="PWF"):
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


def _steps(wid):
    with Session(engine) as s:
        return orchestrator.get_steps(s, wid)


def _run(wid, doc_id, locale="es-ES"):
    """Drive a run on a session THIS suite owns (so probes can interrogate it)."""
    with Session(engine) as s:
        _PROBE_SESSION[0] = s
        try:
            return list(orchestrator.run_workflow_stream(s, wid, doc_id, locale))
        finally:
            _PROBE_SESSION[0] = None


def _run_rows(run_id):
    with Session(engine) as s:
        return s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()


def _last_run(wid):
    with Session(engine) as s:
        return s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == wid)
            .order_by(WorkflowRun.id.desc())
        ).first()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _err(fn, *a, **kw) -> str:
    """Run `fn`, return the WorkflowError message; fail the suite if it did NOT raise."""
    try:
        fn(*a, **kw)
    except wf.WorkflowError as exc:
        return str(exc)
    _fail("expected a WorkflowError, got none")


# ============================================================================
# The stubbed judge. `lqa.score_text` is the seam the orchestrator calls (and the
# seam whose own docstring certifies it session-free) -- replacing it keeps every
# lane offline AND makes each lane's inputs observable.
# ============================================================================
_CALLS: list = []  # one dict per lane call, in COMPLETION order
_CALL_LOCK = threading.Lock()
_BARRIER: list = [None]  # optional threading.Barrier both lanes must reach
_GATE_BY_PROVIDER: dict = {}  # tap step_key -> passed?  (per-lane verdict control)
_RAISE_FOR: set = set()  # tap step_keys whose call raises
_PROBE_SESSION: list = [None]
_real_score_text = lqa.score_text


def _card(passed=True, score=None, errors=None):
    score = (100.0 if passed else 40.0) if score is None else score
    errs = (
        errors
        if errors is not None
        else (
            []
            if passed
            else [
                {
                    "dimension": "Accuracy",
                    "severity": "Major",
                    "note": "stub finding",
                    "span": "cat",
                }
            ]
        )
    )
    return {
        "score": score,
        "passed": passed,
        "errors": errs,
        "gate": "auto_approved" if passed else "needs_review",
        "cost_usd": 0.25,
        "tokens_in": 11,
        "tokens_out": 7,
    }


def _stub_score_text(
    source,
    target,
    source_locale,
    target_locale,
    rulebook="",
    provider_name=None,
    style_guide="",
    tap_meta=None,
    spec=None,
    glossary_terms=None,
    lqa_threshold=None,
):
    # lqa_threshold (feat/lqa-configurable-threshold): this double must mirror the real
    # lqa.score_text signature the orchestrator now calls with a per-member pass bar.
    # Pass/fail here is driven by _GATE_BY_PROVIDER, so the value is accepted-and-ignored.
    key = (tap_meta or {}).get("step_key")
    rec = {
        "step_key": key,
        "source": source,
        "target": target,
        "provider": provider_name,
        "tap_meta": dict(tap_meta or {}),
        "spec_provider": getattr(spec, "provider", None),
    }
    if _BARRIER[0] is not None:
        # THE CONCURRENCY PROOF. Both lanes must be IN FLIGHT AT THE SAME TIME for
        # either to get past this line; a sequential implementation blocks here
        # forever and the barrier's own timeout breaks it (BrokenBarrierError ->
        # a FAILED member StepRun), so this can never pass by being merely fast.
        _BARRIER[0].wait()
    with _CALL_LOCK:
        _CALLS.append(rec)
    if key in _RAISE_FOR:
        raise RuntimeError("stub judge exploded")
    return _card(passed=_GATE_BY_PROVIDER.get(key, True))


def _reset_stub():
    _CALLS.clear()
    _BARRIER[0] = None
    _GATE_BY_PROVIDER.clear()
    _RAISE_FOR.clear()


def _install_stub():
    lqa.score_text = _stub_score_text


def _remove_stub():
    lqa.score_text = _real_score_text


# ============================================================================
# [A] SAVE-TIME MEMBERSHIP RULES (section 5.1)
# ============================================================================
def test_a_membership_rules():
    # --- the happy shape: translate -> [check, check] -> fix ---
    wid = _mk_wf("A-ok")
    _translate(wid)
    _check(wid, "LQA main", key="lqa_main", parallel_group="checks")
    _check(wid, "LQA term", key="lqa_term", parallel_group="checks")
    st = _steps(wid)
    assert [s.parallel_group for s in st] == [None, "checks", "checks"], [
        s.parallel_group for s in st
    ]
    print("  [A] a 2-member group of consecutive lqa steps is accepted")

    # --- rule 1: members must be CONSECUTIVE (a split slug is refused) ---
    _translate(wid, name="Middle")
    msg = _err(_check, wid, "LQA late", key="lqa_late", parallel_group="checks")
    assert "consecutive" in msg and "checks" in msg, msg
    print(f"  [A] rule 1 (consecutive): a split group is refused -- {msg[:72]!r}")

    # --- rule 2: Phase 1 members are CHECK-ONLY ---
    wid2 = _mk_wf("A-type")
    _translate(wid2)
    _check(wid2, "L1", key="k1", parallel_group="g")
    msg = _err(_translate, wid2, name="Bad", parallel_group="g")
    assert "no effect on an llm_agent step" in msg and "CHECKS only" in msg, msg
    with Session(engine) as s:
        msg2 = _err(
            wf.add_step, s, wid2, name="HR", step_type=StepType.HUMAN_REVIEW, parallel_group="g"
        )
    assert "no effect on a human_review step" in msg2, msg2
    print(
        "  [A] rule 2 (check-only): llm_agent + human_review members refused with an "
        "explicit Phase-1 message"
    )

    # A group of TWO llm_agent members is refused by the whole-graph rule too (the
    # dead-field refusal fires first per step; this proves _group_errors itself).
    ordered = [
        {"id": 1, "order_index": 0, "step_type": StepType.LLM_AGENT, "parallel_group": "g"},
        {"id": 2, "order_index": 1, "step_type": StepType.LLM_AGENT, "parallel_group": "g"},
    ]
    ge = [m for _i, _l, m, _id in wf._group_errors(ordered)]
    assert len(ge) == 2 and all("CHECKS only" in m for m in ge), ge
    print("  [A] rule 2: _group_errors itself flags EVERY non-lqa member, not just one")

    # --- rule 3: at most ONE routing member ---
    wid3 = _mk_wf("A-branch")
    _translate(wid3)
    fix = _translate(wid3, name="Fix", key="fix")
    a = _check(wid3, "L1", key="ka", parallel_group="g", branch={"on_fail": "fix"})
    b_id = _check(wid3, "L2", key="kb", parallel_group="g")
    with Session(engine) as s:
        msg = _err(wf.update_step, s, b_id, branch={"on_fail": "fix"})
    assert "at most ONE member may carry a branch" in msg, msg
    print("  [A] rule 3 (one routing member): a second branch in the group is refused")

    # --- rule 5: an OUTSIDE branch may target the FIRST member only ---
    with Session(engine) as s:
        msg = _err(wf.update_step, s, fix, qe_gate=True, branch={"on_fail": "kb"})
    assert "NON-FIRST member" in msg and "'ka'" in msg, msg
    with Session(engine) as s:
        wf.update_step(s, fix, qe_gate=True, branch={"on_fail": "ka"})
    print(
        "  [A] rule 5 (entry point): targeting a non-first member is refused; the "
        "first member is accepted"
    )
    assert a  # the routing member id is used above only through its key

    # --- rule 4: a member's guard resolves against PRE-GROUP steps ---
    wid4 = _mk_wf("A-guard")
    _translate(wid4)
    _check(wid4, "L1", key="m1", parallel_group="g")
    m2 = _check(wid4, "L2", key="m2", parallel_group="g")
    with Session(engine) as s:
        msg = _err(wf.update_step, s, m2, run_condition="step:m1:failed")
    assert "does not resolve" in msg and "SAME parallel group" in msg, msg
    print("  [A] rule 4 (no sibling refs): a lane referencing another lane is refused")

    # --- slug validation ---
    msg = _err(_check, wid4, "L3", parallel_group="!!!")
    assert "not a valid slug" in msg, msg
    with Session(engine) as s:
        cand = wf.add_step(
            s, wid4, name="L4", step_type=StepType.LQA, parallel_group="  Checks Two  "
        )
    assert cand.parallel_group == "checks_two", cand.parallel_group
    print(
        "  [A] slug rules: an unslugifiable value is refused; a human-typed one "
        f"normalises ({cand.parallel_group!r})"
    )


def test_a_diff_dead_fields_and_copies():
    # --- the before/after diff: pre-existing breakage never blocks an unrelated edit ---
    wid = _mk_wf("A-diff")
    t = _translate(wid)
    _check(wid, "L1", key="d1", parallel_group="g")
    _check(wid, "L2", key="d2", parallel_group="g")
    # Manufacture a PRE-EXISTING violation the validators would refuse (a split group),
    # bypassing them exactly as a legacy row would look.
    with Session(engine) as s:
        rows = orchestrator.get_steps(s, wid)
        rows[0].parallel_group = "g"  # translate step, non-adjacent slug + wrong type
        s.add(rows[0])
        s.commit()
    with Session(engine) as s:
        pre = wf._graph_edit_errors(orchestrator.get_steps(s, wid))
    assert pre, "fixture did not produce a pre-existing graph error"
    with Session(engine) as s:
        wf.update_step(s, t, name="Renamed translate")  # unrelated edit -> allowed
    with Session(engine) as s:
        assert s.get(WorkflowStep, t).name == "Renamed translate"
    print(
        "  [A] before/after diff: a PRE-EXISTING group violation (%d error(s)) does "  # noqa: UP031
        "not block an unrelated edit" % len(pre)
    )

    # --- dead-field handling per step type ---
    assert "parallel_group" not in wf.DEAD_FIELDS_BY_TYPE[StepType.LQA]
    assert "parallel_group" in wf.DEAD_FIELDS_BY_TYPE[StepType.HUMAN_REVIEW]
    assert "parallel_group" in wf.DEAD_FIELDS_BY_TYPE[StepType.LLM_AGENT]
    print("  [A] dead-field table: live on lqa, dead on human_review AND llm_agent")

    # A TYPE SWITCH off lqa force-clears (and REPORTS) rather than refusing -- the
    # builder hides the field the moment the type changes, so a refusal would have
    # nothing for the user to act on.
    wid2 = _mk_wf("A-switch")
    _translate(wid2)
    _check(wid2, "L1", key="s1", parallel_group="g")
    s2 = _check(wid2, "L2", key="s2", parallel_group="g")
    report: dict = {}
    with Session(engine) as s:
        wf.update_step(
            s, s2, step_type=StepType.LLM_AGENT, prompt_text="Fix: {text}", report=report
        )
    with Session(engine) as s:
        assert s.get(WorkflowStep, s2).parallel_group is None
    assert "parallel_group" in report.get("type_switch_cleared", []), report
    print(
        "  [A] type switch off lqa force-CLEARS the group and reports it "
        "(type_switch_cleared={!r})".format(report.get("type_switch_cleared"))
    )

    # --- both copy sites (the reflective field-set parity guard is
    #     tests/verify_clone_field_parity.py; this is the behavioural half) ---
    wid3 = _mk_wf("A-copy")
    _translate(wid3)
    _check(wid3, "L1", key="c1", parallel_group="checks")
    _check(wid3, "L2", key="c2", parallel_group="checks")
    with Session(engine) as s:
        clone = wf.clone_workflow(s, wid3)
        clone_id = clone.id
        pairs = wf.copy_to_pairs(s, wid3, [("en-US", "fr-FR")], adapt=False)
    copy_id = pairs["created"][0]["workflow_id"]
    for wid_x, label in ((clone_id, "clone_workflow"), (copy_id, "copy_to_pairs")):
        got = [s.parallel_group for s in _steps(wid_x)]
        assert got == [None, "checks", "checks"], (label, got)
    print(
        "  [A] both copy sites carry parallel_group (a dropped slug would silently "
        "FLATTEN the lanes on every clone / pair variant)"
    )


# ============================================================================
# [B] CONDITION REFS (section 5.5)
# ============================================================================
def test_b_condition_refs():
    wid = _mk_wf("B")
    _translate(wid)
    _check(wid, "LQA main", key="lqa_main", parallel_group="checks")
    _check(wid, "LQA term", key="lqa_term", parallel_group="checks")

    msg = _err(_translate, wid, name="Fix", run_condition="prev_lqa:failed")
    assert "ambiguous" in msg and "'checks'" in msg and "step:lqa_main:failed" in msg, msg
    print(
        "  [B] ambiguous prev_lqa REFUSED at save, naming the group and the exact "
        f"step: form -- {msg[:96]!r}"
    )

    fix = _translate(wid, name="Fix", key="fix", run_condition="step:lqa_term:failed")
    with Session(engine) as s:
        assert s.get(WorkflowStep, fix).run_condition == {
            "ref": "step:lqa_term",
            "op": "failed",
            "value": None,
        }
    print("  [B] step:<key> round-trips through the CLI compact form")

    # A key that exists but produces no gate, and one that does not exist at all.
    msg = _err(_translate, wid, name="F2", run_condition="step:nope:failed")
    assert "does not resolve" in msg, msg
    print("  [B] step:<key> naming no earlier gate step is refused")

    # UNAMBIGUOUS prev_lqa is untouched -> zero migration.
    wid2 = _mk_wf("B-plain")
    _translate(wid2)
    _check(wid2, "L", key="only")
    ok = _translate(wid2, name="Fix", run_condition="prev_lqa:failed")
    with Session(engine) as s:
        assert s.get(WorkflowStep, ok).run_condition["ref"] == "prev_lqa"
    print("  [B] an UNAMBIGUOUS prev_lqa is accepted unchanged (zero migration)")

    # And parse_run_condition_str handles the 4-field score form.
    assert wf.parse_run_condition_str("step:lqa_main:score_lt:90") == {
        "ref": "step:lqa_main",
        "op": "score_lt",
        "value": 90.0,
    }
    print(
        "  [B] step:<key>:score_lt:<value> parses (the colon inside the ref is destructured first)"
    )


def test_b_non_routing_lane_is_addressable():
    """The payoff of section 5.5: the lane that does NOT route is still readable by a
    later guard, which is the whole reason a keyed ref exists."""
    pid = _mk_project("B2")
    doc = _mk_doc(pid)
    wid = _mk_wf("B2")
    _translate(wid)
    _check(wid, "LQA main", key="b_main", parallel_group="checks", branch={"on_fail": "end"})
    _check(wid, "LQA term", key="b_term", parallel_group="checks")
    _translate(wid, name="Term fix", key="b_fix", run_condition="step:b_term:failed")
    _reset_stub()
    _install_stub()
    try:
        # main PASSES (so the routing member falls through), term FAILS.
        _GATE_BY_PROVIDER["b_term"] = False
        events = _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    kinds = [(e["type"], e.get("step_index")) for e in events]
    assert ("step", 3) in kinds, kinds
    skipped = [e for e in events if e["type"] == "step_skipped"]
    assert not skipped, skipped
    assert run.status == RunStatus.COMPLETED, run.status
    print(
        "  [B] the NON-routing lane's gate drove a later guard via step:<key> "
        "(the fix step RAN because b_term failed, while b_main passed)"
    )


# ============================================================================
# [C] EXECUTION (section 5.2 / 5.4)
# ============================================================================
def _grouped_wf(name="C", *, branch=None, guards=None, max_visits=None):
    wid = _mk_wf(name)
    _translate(wid)
    kw1 = {"key": "c_main", "parallel_group": "checks"}
    kw2 = {"key": "c_term", "parallel_group": "checks"}
    if branch:
        kw1["branch"] = branch
    if max_visits:
        kw1["max_visits"] = max_visits
        kw2["max_visits"] = max_visits
    if guards:
        kw1["run_condition"] = guards[0]
        kw2["run_condition"] = guards[1]
    _check(wid, "LQA main", **kw1)
    _check(wid, "LQA term", **kw2)
    return wid


def test_c_concurrency_barrier():
    """CONCURRENCY, PROVEN BY A BARRIER RATHER THAN A CLOCK. Both lanes must be in
    flight simultaneously for either to return; a sequential engine blocks forever and
    the barrier's own timeout breaks it. No wall-clock threshold appears anywhere."""
    pid = _mk_project("C1")
    doc = _mk_doc(pid)
    wid = _grouped_wf("C1")
    _reset_stub()
    _BARRIER[0] = threading.Barrier(2, timeout=20)
    _install_stub()
    try:
        events = _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    assert run.status == RunStatus.COMPLETED, (
        "the two lanes did not overlap -- the barrier broke, meaning they ran "
        f"SEQUENTIALLY (run status {run.status!r}, events {events[-1]!r})"
    )
    assert len(_CALLS) == 2, _CALLS
    assert not _BARRIER[0].broken
    print(
        "  [C] CONCURRENCY: both lanes were in flight at the same instant "
        "(threading.Barrier(2) released; a sequential engine could not pass it)"
    )


def test_c_frozen_snapshot():
    """Every lane sees the SAME frozen snapshot of the working text -- and, because a
    Phase-1 lane writes no text, no lane can observe another lane's work."""
    pid = _mk_project("C2")
    doc = _mk_doc(pid)
    wid = _grouped_wf("C2")
    _reset_stub()
    _install_stub()
    try:
        _run(wid, doc)
    finally:
        _remove_stub()
    assert len(_CALLS) == 2, _CALLS
    targets = {c["target"] for c in _CALLS}
    sources = {c["source"] for c in _CALLS}
    assert len(targets) == 1, f"lanes saw DIFFERENT target text: {targets!r}"
    assert len(sources) == 1, sources
    run = _last_run(wid)
    rows = _run_rows(run.id)
    # The snapshot is the TRANSLATE step's output, unchanged by either lane.
    assert rows[0].output_text == targets.pop(), (rows[0].output_text, targets)
    assert rows[1].output_text == rows[2].output_text == rows[0].output_text
    print(
        "  [C] frozen snapshot: both lanes scored the IDENTICAL text (the translate "
        "step's output), and neither altered it"
    )


def test_c_one_commit_join_and_id_order():
    """The join persists every member in order_index order in ONE commit (section 5.4)
    -- the invariant `_latest_outcome_steprun` and `observe_run` both depend on."""
    pid = _mk_project("C3")
    doc = _mk_doc(pid)
    wid = _grouped_wf("C3")
    member_ids = [s.id for s in _steps(wid)[1:]]
    snapshots: list = []

    _reset_stub()
    _install_stub()
    try:
        with Session(engine) as s:
            _PROBE_SESSION[0] = s
            real_commit = s.commit

            def _recording_commit():
                real_commit()
                run = s.exec(
                    select(WorkflowRun)
                    .where(WorkflowRun.workflow_id == wid)
                    .order_by(WorkflowRun.id.desc())
                ).first()
                if run is None:
                    snapshots.append(set())
                    return
                got = s.exec(select(StepRun.step_id).where(StepRun.workflow_run_id == run.id)).all()
                snapshots.append({g for g in got if g in member_ids})

            s.commit = _recording_commit
            try:
                list(orchestrator.run_workflow_stream(s, wid, doc, "es-ES"))
            finally:
                s.commit = real_commit
                _PROBE_SESSION[0] = None
    finally:
        _remove_stub()

    want = set(member_ids)
    boundary = next(
        (i for i in range(1, len(snapshots)) if not snapshots[i - 1] and snapshots[i] == want), None
    )
    assert boundary is not None, (
        "the group's member StepRuns did not appear together at a single commit "
        f"boundary: {snapshots!r}"
    )
    print(
        "  [C] ONE-COMMIT join: all %d member StepRuns became visible at the SAME "  # noqa: UP031
        "commit boundary (#%d of %d)" % (len(member_ids), boundary, len(snapshots))
    )

    run = _last_run(wid)
    rows = _run_rows(run.id)
    got = [(r.id, r.step_id) for r in rows if r.step_id in member_ids]
    assert [sid for _i, sid in got] == member_ids, (
        f"StepRun id order does not follow order_index: {got!r}"
    )
    assert [i for i, _s in got] == sorted(i for i, _s in got)
    print(
        "  [C] id-order invariant: member StepRun ids ascend in order_index order "
        f"{got!r} -- observer replay stays deterministic"
    )


def test_c_all_skip_group_is_a_noop():
    pid = _mk_project("C4")
    doc = _mk_doc(pid)
    wid = _mk_wf("C4")
    _translate(wid)
    _check(wid, "Gate", key="gate")
    # Both members guarded on the FIRST gate passing... which it does, so guard on
    # `failed` to make both skip.
    _check(wid, "L1", key="s_a", parallel_group="g", run_condition="step:gate:failed")
    _check(wid, "L2", key="s_b", parallel_group="g", run_condition="step:gate:failed")
    _translate(wid, name="After", key="after")
    _reset_stub()
    _install_stub()
    try:
        events = _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    assert run.status == RunStatus.COMPLETED, run.status
    skipped = [e for e in events if e["type"] == "step_skipped"]
    assert [e["step_index"] for e in skipped] == [2, 3], skipped
    assert all(e.get("parallel_group") == "g" for e in skipped), skipped
    assert len(_CALLS) == 1, f"the gate scored once; no lane may have dispatched: {_CALLS!r}"
    after = [e for e in events if e["type"] == "step" and e["step_index"] == 4]
    assert after, events
    rows = _run_rows(run.id)
    assert sum(1 for r in rows if r.status == RunStatus.SKIPPED) == 2
    print(
        "  [C] all-skip group: a clean no-op -- 2 SKIPPED rows, zero judge calls, "
        "the cursor moved past the group to step 4"
    )


def test_c_member_failure_fails_run_after_all_persist():
    pid = _mk_project("C5")
    doc = _mk_doc(pid)
    wid = _grouped_wf("C5")
    member_ids = [s.id for s in _steps(wid)[1:]]
    _reset_stub()
    _install_stub()
    try:
        _RAISE_FOR.add("c_main")  # the FIRST member explodes
        events = _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    assert run.status == RunStatus.FAILED, run.status
    rows = {r.step_id: r for r in _run_rows(run.id)}
    assert set(member_ids) <= set(rows), (
        f"the run failed before the surviving lane persisted: {list(rows)!r}"
    )
    assert rows[member_ids[0]].status == RunStatus.FAILED
    assert rows[member_ids[1]].status == RunStatus.COMPLETED
    assert rows[member_ids[1]].gate == "passed"
    assert events[-1]["type"] == "error"
    print(
        "  [C] member failure: the run FAILED only AFTER every lane persisted "
        "(lane 1 FAILED, lane 2 COMPLETED+gated) -- the operator sees both outcomes"
    )

    # Write-back is success-only, so a failed run leaves NO targets.
    with Session(engine) as s:
        tgts = s.exec(select(Segment.target_text).where(Segment.document_id == doc)).all()
    assert all(not (t or "").strip() for t in tgts), tgts
    print(
        "  [C] a failed parallel run wrote NO segment targets (the success-only "
        "write-back choke point is untouched)"
    )


def test_c_loopback_reenters_the_whole_group():
    pid = _mk_project("C6")
    doc = _mk_doc(pid)
    wid = _mk_wf("C6")
    _translate(wid)
    _check(
        wid,
        "LQA main",
        key="lb_main",
        parallel_group="g",
        branch={"on_fail": "lb_main"},
        max_visits=2,
    )
    _check(wid, "LQA term", key="lb_term", parallel_group="g", max_visits=2)
    member_ids = [s.id for s in _steps(wid)[1:]]
    _reset_stub()
    _install_stub()
    try:
        _GATE_BY_PROVIDER["lb_main"] = False  # always fails -> loop back into itself
        _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    rows = _run_rows(run.id)
    per_member = {mid: [r.outcome_index for r in rows if r.step_id == mid] for mid in member_ids}
    assert per_member[member_ids[0]] == [0, 1], per_member
    assert per_member[member_ids[1]] == [0, 1], (
        "the loop-back re-entered only the routing member; the WHOLE group must "
        f"re-run and every member's visits must bump: {per_member!r}"
    )
    visits = (run.context or {}).get("visits", {})
    assert visits.get("lb_main") == 2 and visits.get("lb_term") == 2, visits
    assert run.status == RunStatus.COMPLETED, run.status
    print(
        "  [C] loop-back re-entry: the WHOLE group re-ran and BOTH members' visit "
        f"counts bumped ({visits!r}); max_visits ended the loop gracefully"
    )


def test_c_loop_cap_trips_before_any_dispatch():
    pid = _mk_project("C7")
    doc = _mk_doc(pid)
    wid = _mk_wf("C7")
    _translate(wid)
    _check(
        wid,
        "LQA main",
        key="cap_main",
        parallel_group="g",
        branch={"on_fail": "cap_main"},
        max_visits=50,
    )
    _check(wid, "LQA term", key="cap_term", parallel_group="g", max_visits=50)
    from app.services import settings

    settings.set_many({"workflow_total_step_cap": "3"})
    _reset_stub()
    _install_stub()
    try:
        _GATE_BY_PROVIDER["cap_main"] = False
        events = _run(wid, doc)
        calls_at_cap = len(_CALLS)
    finally:
        _remove_stub()
        settings.set_many({"workflow_total_step_cap": "50"})
    run = _last_run(wid)
    assert run.status == RunStatus.FAILED, run.status
    assert events[-1]["type"] == "error"
    assert "loop_cap_reached" in events[-1]["error"], events[-1]
    rows = _run_rows(run.id)
    failed = [r for r in rows if r.error == "loop_cap_reached"]
    assert len(failed) == 1, rows
    # The cap tripped on the SECOND member of the second visit (steps 1,2,3 then 4>3),
    # so exactly the lanes dispatched BEFORE the cap ran -- never the capped group.
    assert calls_at_cap == 2, (
        "a lane dispatched after the loop cap tripped: %d call(s)" % calls_at_cap  # noqa: UP031
    )
    print(
        "  [C] loop_cap_reached: the run FAILED at the guard, BEFORE the capped "  # noqa: UP031
        "group dispatched anything (%d judge calls, all from the first visit)" % calls_at_cap
    )


def test_c_full_text_mirror():
    """The FULL_TEXT loop mirrors the segmented one through the SAME shared seam."""
    pid = _mk_project("C8")
    doc = _mk_doc_fulltext(pid)
    wid = _grouped_wf("C8")
    _reset_stub()
    _BARRIER[0] = threading.Barrier(2, timeout=20)
    _install_stub()
    try:
        events = _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    assert run.status == RunStatus.COMPLETED, (run.status, events[-1])
    assert len(_CALLS) == 2 and not _BARRIER[0].broken
    rows = _run_rows(run.id)
    assert [r.status for r in rows] == [RunStatus.COMPLETED] * 3, rows
    print(
        "  [C] FULL_TEXT mirror: the same fork/join seam ran the group concurrently "
        "on the whole-document path too"
    )


# ============================================================================
# [D] ROUTING AT THE JOIN (section 5.3)
# ============================================================================
def test_d_routing_at_the_join():
    pid = _mk_project("D")
    doc = _mk_doc(pid)
    # 0 translate | 1+2 the group | 3 a step the routing jump must SKIP | 4 the fixer.
    # The fixer sits two steps past the group so an on_fail jump is distinguishable
    # from plain fall-through (which would run step 3).
    wid = _mk_wf("D")
    _translate(wid)
    _check(wid, "LQA main", key="d_main", parallel_group="g")
    _check(wid, "LQA term", key="d_term", parallel_group="g")
    _translate(wid, name="Skipped by the jump", key="d_skip")
    fixer = _translate(wid, name="Fixer", key="d_fix")
    with Session(engine) as s:
        wf.update_step(s, _steps(wid)[1].id, branch={"on_fail": "d_fix", "on_pass": "end"})
    assert fixer

    # (i) the ROUTING member fails -> jump to the fixer, even though the OTHER lane
    #     passed. A non-routing gate must never move the cursor.
    _reset_stub()
    _install_stub()
    try:
        _GATE_BY_PROVIDER["d_main"] = False
        events = _run(wid, doc)
    finally:
        _remove_stub()
    idxs = [e.get("step_index") for e in events if e["type"] == "step"]
    assert idxs == [0, 1, 2, 4], (
        f"routing did not happen at the JOIN off the routing member: {idxs!r}"
    )
    print(
        f"  [D] routing member's on_fail routed at the join (order {idxs!r}): both lanes "
        "ran, THEN the cursor jumped PAST step 3 to the fixer"
    )

    # (ii) the ROUTING member passes -> on_pass 'end' finishes the run; the OTHER
    #      lane's FAILING gate must not override it.
    doc2 = _mk_doc(pid)
    _reset_stub()
    _install_stub()
    try:
        _GATE_BY_PROVIDER["d_term"] = False
        events = _run(wid, doc2)
    finally:
        _remove_stub()
    idxs = [e.get("step_index") for e in events if e["type"] == "step"]
    assert idxs == [0, 1, 2], f"a NON-routing member's gate moved the cursor: {idxs!r}"
    assert _last_run(wid).status == RunStatus.COMPLETED
    print(
        f"  [D] a NON-routing member's failing gate did NOT route (order {idxs!r}); only "
        "the routing member decides"
    )

    # (iii) no routing member at all -> fall through past the LAST member.
    wid2 = _grouped_wf("D2")
    _translate(wid2, name="After", key="d2_after")
    doc3 = _mk_doc(pid)
    _reset_stub()
    _install_stub()
    try:
        _GATE_BY_PROVIDER["c_main"] = False
        _GATE_BY_PROVIDER["c_term"] = False
        events = _run(wid2, doc3)
    finally:
        _remove_stub()
    idxs = [e.get("step_index") for e in events if e["type"] == "step"]
    assert idxs == [0, 1, 2, 3], idxs
    print(
        "  [D] no routing member: the group fell through to the step after the LAST "
        f"member ({idxs!r}), exactly like a branch-less gate today"
    )


# ============================================================================
# [E] CONCURRENCY HYGIENE
# ============================================================================
_FLAGS: list = []


class _ProbePool(ThreadPoolExecutor):
    """Records the DBAPI in-transaction flag of the RUN's session at every dispatch.

    THE ORACLE IS THE DBAPI FLAG, NOT session.new/dirty: autoflush empties those
    collections while the rows stay uncommitted and SQLite's writer lock stays HELD --
    they read empty in exactly the state this probe exists to detect (see
    db.dbapi_in_transaction's docstring, and the QA-1 post-mortem in
    tests/verify_orchestrator_spend_lock.py)."""

    def submit(self, fn, *a, **kw):
        sess = _PROBE_SESSION[0]
        if sess is not None:
            _FLAGS.append(("submit", _db.dbapi_in_transaction(sess)))
        return super().submit(fn, *a, **kw)


def test_e_no_open_write_transaction_at_dispatch():
    pid = _mk_project("E1")
    doc = _mk_doc(pid)
    # A branch + max_visits make RunGuard ACTIVE, so the run really does stage
    # WorkflowRun.context writes around the fork -- the staging this probe is about.
    wid = _mk_wf("E1")
    _translate(wid)
    _check(
        wid, "LQA main", key="e_main", parallel_group="g", branch={"on_pass": "next"}, max_visits=3
    )
    _check(wid, "LQA term", key="e_term", parallel_group="g", max_visits=3)
    _FLAGS.clear()
    _reset_stub()
    _install_stub()
    set_pieces("app.services.orchestrator", "ThreadPoolExecutor", _ProbePool)
    try:
        _run(wid, doc)
    finally:
        set_pieces("app.services.orchestrator", "ThreadPoolExecutor", ThreadPoolExecutor)
        _remove_stub()
    assert _FLAGS, "no dispatch point was probed"
    held = [f for f in _FLAGS if f[1]]
    assert not held, (
        "a write transaction was OPEN at %d of %d dispatch points -- QA-1's exact "  # noqa: UP031
        "circular wait: %r" % (len(held), len(_FLAGS), _FLAGS)
    )
    print(
        "  [E] %d dispatch points probed, DBAPI in-transaction FALSE at every one "  # noqa: UP031
        "(the writer lock is handed back before any lane is submitted)" % len(_FLAGS)
    )


def test_e_spend_tap_and_replay():
    pid = _mk_project("E2")
    doc = _mk_doc(pid)
    wid = _grouped_wf("E2")
    member_ids = [s.id for s in _steps(wid)[1:]]
    _reset_stub()
    _install_stub()
    try:
        _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    rows = {r.step_id: r for r in _run_rows(run.id)}
    for mid in member_ids:
        assert rows[mid].cost_usd == 0.25 and rows[mid].tokens_in == 11, rows[mid]
    print(
        "  [E] per-member spend: each lane's own cost/tokens landed on its OWN "
        "StepRun (thread-local, never summed onto one row)"
    )

    keys = sorted(c["tap_meta"].get("step_key") for c in _CALLS)
    assert keys == ["c_main", "c_term"], keys
    assert all(c["tap_meta"].get("workflow_run_id") == run.id for c in _CALLS)
    print(
        f"  [E] prompt tap: every lane's tap_meta carried its OWN step_key {keys!r} plus "
        "the run id -- the lane is identifiable in the tap"
    )

    # Deterministic observer replay: two independent replays of the SAME finished run
    # must emit identical event sequences.
    def _replay():
        out = []
        for _run_row, ev in orchestrator.observe_run(run.id, poll_interval=0.01, max_seconds=10.0):
            out.append((ev["type"], ev.get("step_index"), ev.get("step_name")))
            if ev["type"] in ("done", "error", "closed"):
                break
        return out

    a, b = _replay(), _replay()
    assert a == b, (a, b)
    step_idx = [i for t, i, _n in a if t == "step"]
    assert step_idx == sorted(step_idx), step_idx
    print(
        "  [E] observer replay is deterministic and in order across independent "
        f"passes ({step_idx!r})"
    )


# ============================================================================
# [F] UNGROUPED BYTE-STABILITY (three independent proofs -- see the header)
# ============================================================================
def _plain_wf(name):
    wid = _mk_wf(name)
    _translate(wid)
    _check(wid, "LQA", key="p_lqa")
    _translate(wid, name="Polish", key="p_polish")
    return wid


def _boom(*a, **kw):
    raise AssertionError(
        "the parallel seam was ENTERED on a workflow with no groups -- byte-stability "
        "is broken by construction"
    )


def test_f1_seam_unreachable_when_no_groups():
    pid = _mk_project("F1")
    doc = _mk_doc(pid)
    wid = _plain_wf("F1")
    _reset_stub()
    _install_stub()
    real_at, real_run = orchestrator._group_at, orchestrator._run_parallel_group
    set_pieces("app.services.orchestrator", "_group_at", _boom)
    set_pieces("app.services.orchestrator", "_run_parallel_group", _boom)
    try:
        events = _run(wid, doc)
    finally:
        set_pieces("app.services.orchestrator", "_group_at", real_at)
        set_pieces("app.services.orchestrator", "_run_parallel_group", real_run)
        _remove_stub()
    assert _last_run(wid).status == RunStatus.COMPLETED
    assert events[-1]["type"] == "done", events[-1]
    print(
        "  [F1] UNREACHABILITY: an ungrouped run completed with BOTH seam entry "
        "points replaced by functions that raise -- no new code executed at all"
    )


def _canonical(wid, doc):
    """Everything a run externally produces, as comparable plain data."""
    events = _run(wid, doc)
    run = _last_run(wid)
    rows = [
        (
            r.step_id,
            r.status,
            r.input_text,
            r.output_text,
            r.error,
            r.score,
            r.gate,
            r.outcome_index,
            r.cost_usd,
            r.tokens_in,
            r.tokens_out,
        )
        for r in _run_rows(run.id)
    ]
    with Session(engine) as s:
        segs = [
            (x.order_index, x.target_text, x.status, x.qa_flags)
            for x in s.exec(
                select(Segment).where(Segment.document_id == doc).order_by(Segment.order_index)
            ).all()
        ]
    ev = [tuple(sorted((k, repr(v)) for k, v in e.items() if k != "run_id")) for e in events]
    return {
        "events": ev,
        "rows": rows,
        "segs": segs,
        "run": (run.status, run.current_step_index, run.context),
    }


def test_f2_armed_vs_disarmed_equivalence():
    pid = _mk_project("F2")
    wid = _plain_wf("F2")
    real = orchestrator._has_parallel
    out = {}
    for label, forced in (("disarmed", False), ("armed", True)):
        doc = _mk_doc(pid)
        _reset_stub()
        _install_stub()
        set_pieces("app.services.orchestrator", "_has_parallel", lambda _steps, _v=forced: _v)
        try:
            out[label] = _canonical(wid, doc)
        finally:
            set_pieces("app.services.orchestrator", "_has_parallel", real)
            _remove_stub()
    a, b = out["disarmed"], out["armed"]
    for field in ("events", "rows", "segs", "run"):
        assert a[field] == b[field], (
            f"ARMED vs DISARMED differ in {field}:\n  disarmed={a[field]!r}\n  armed={b[field]!r}"
        )
    print(
        "  [F2] ARMED-vs-DISARMED: forcing the gate True on an ungrouped run "  # noqa: UP031
        "changed NOTHING -- %d events, %d StepRuns, %d segments identical"
        % (len(a["events"]), len(a["rows"]), len(a["segs"]))
    )


# The wf_graph SVG of a fixed ungrouped 3-step workflow WITH a loop-back branch curve
# (the geometry a macro edit is most likely to disturb). Captured from the working tree
# and independently verified BYTE-IDENTICAL against `git show
# 630d28d:app/templates/_macros.html` -- the branch point, before any parallel-routing
# change -- for this exact fixture (4476 bytes both sides). Pinning the digest rather
# than the blob keeps the suite readable; the length is asserted too so a hash collision
# is not the only thing standing between a regression and a green run.
_UNGROUPED_SVG_SHA256 = "d710b0fceb0f316fa6fa31f7f416c267fbf84c9be934e33d62788eb5aff3fe05"
_UNGROUPED_SVG_LEN = 4476


class _S(dict):
    """A step the macro can read with dotted access (it accepts ORM rows AND draft
    dicts, so a dict with __getattr__ exercises the very same code path)."""

    def __getattr__(self, k):
        return self.get(k)


_SNAPSHOT_STEPS = [
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


def _render_graph(steps):
    env = Environment(loader=FileSystemLoader(os.path.join(_ROOT, "app", "templates")))
    return env.get_template("_macros.html").module.wf_graph(steps)


def test_f3_ungrouped_svg_snapshot():
    svg = _render_graph(_SNAPSHOT_STEPS)
    digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()
    assert len(svg) == _UNGROUPED_SVG_LEN, (
        "the UNGROUPED wf_graph SVG changed length (%d != %d) -- an edit to the macro "  # noqa: UP031
        "disturbed the geometry of workflows that use no parallel groups"
        % (len(svg), _UNGROUPED_SVG_LEN)
    )
    assert digest == _UNGROUPED_SVG_SHA256, (
        f"the UNGROUPED wf_graph SVG is no longer byte-identical (sha256 {digest} != {_UNGROUPED_SVG_SHA256})"  # noqa: E501
    )
    print(
        "  [F3] wf_graph SNAPSHOT: the ungrouped 3-step + loop-back SVG is "  # noqa: UP031
        "byte-identical (%d bytes, sha256 %s...)" % (len(svg), digest[:12])
    )


# The GROUPED counterpart of the pin above, added by SPEC-workflow-canvas Phase 1 (its
# test plan: "extend the sha256 pin to a grouped fixture"). The ungrouped pin cannot see
# the group outline / label at all -- every artifact of it is gated on `ns.has_par` --
# so a macro edit could have broken the group rendering with the ungrouped pin still
# green. This fixture exercises the outline, the label, a member carrying the routing
# branch, and a downstream QE-gated node, i.e. everything the grouped path emits.
#
# PRE-CHANGE EVIDENCE, captured the same way as the ungrouped pin (the branch point's
# _macros.html copied aside into a scratch template dir and rendered against this exact
# fixture -- never `git stash`): `git show 3e1b91d:app/templates/_macros.html` and the
# working tree produced BYTE-IDENTICAL output, 5481 bytes each, for both this fixture and
# its overlay rendering. The digest below is that exact string.
_GROUPED_SVG_SHA256 = "8e9618dafbcb6c2291288d2ce7760b778ec1b2446f1330333423962ab7be3d50"
_GROUPED_SVG_LEN = 5481

_GROUPED_SNAPSHOT_STEPS = [
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


def test_f3b_grouped_svg_snapshot():
    svg = _render_graph(_GROUPED_SNAPSHOT_STEPS)
    digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()
    assert "(in parallel)" in svg, "the grouped fixture no longer renders a group outline"
    assert len(svg) == _GROUPED_SVG_LEN, (
        "the GROUPED wf_graph SVG changed length (%d != %d) -- a macro edit disturbed "  # noqa: UP031
        "the parallel-group outline/label geometry" % (len(svg), _GROUPED_SVG_LEN)
    )
    assert digest == _GROUPED_SVG_SHA256, (
        f"the GROUPED wf_graph SVG is no longer byte-identical (sha256 {digest} != {_GROUPED_SVG_SHA256})"  # noqa: E501
    )
    print(
        "  [F3b] wf_graph SNAPSHOT: the GROUPED 4-step SVG (outline + label + routing "  # noqa: UP031
        "member) is byte-identical (%d bytes, sha256 %s...)" % (len(svg), digest[:12])
    )


# ============================================================================
# [G] SURFACES
# ============================================================================
def test_g_cli_round_trip():
    from tests._cli_runner import run_cli

    rc, out, err = run_cli(["workflow", "create", "--name", "CLI Par"])
    assert rc == 0, (out, err)
    import json

    rc, out, _e = run_cli(["--json", "workflow", "list"])
    wid = max(w["id"] for w in json.loads(out)["workflows"])

    rc, out, err = run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--name",
            "T",
            "--provider",
            "mock",
            "--prompt",
            "Translate {text}",
        ]
    )
    assert rc == 0, (out, err)
    rc, out, err = run_cli(
        [
            "--json",
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "lqa",
            "--name",
            "L1",
            "--key",
            "g_main",
            "--parallel-group",
            "Checks",
        ]
    )
    assert rc == 0, (out, err)
    assert json.loads(out)["parallel_group"] == "checks", out
    rc, out, err = run_cli(
        [
            "--json",
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "lqa",
            "--name",
            "L2",
            "--key",
            "g_term",
            "--parallel-group",
            "checks",
        ]
    )
    assert rc == 0, (out, err)
    step2 = json.loads(out)["id"]
    print("  [G] CLI add-step --parallel-group round-trips and slugifies ('Checks' -> 'checks')")

    # The refusal reaches the operator as a clean usage error, never a traceback.
    rc, out, err = run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--name",
            "X",
            "--prompt",
            "{text}",
            "--parallel-group",
            "checks",
        ]
    )
    assert rc != 0 and "llm_agent" in err, (rc, out, err)
    print(f"  [G] CLI: an llm_agent member is a clean usage error -- {err.strip()[:78]!r}")

    # The ambiguous short ref, from the CLI compact form.
    rc, out, err = run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--name",
            "F",
            "--prompt",
            "{text}",
            "--run-condition",
            "prev_lqa:failed",
        ]
    )
    assert rc != 0 and "ambiguous" in err, (rc, err)
    rc, out, err = run_cli(
        [
            "--json",
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--name",
            "F",
            "--prompt",
            "{text}",
            "--run-condition",
            "step:g_main:failed",
        ]
    )
    assert rc == 0, (out, err)
    print("  [G] CLI: --run-condition step:<key>:<op> accepted where the bare form is refused")

    rc, out, _e = run_cli(["workflow", "show", str(wid)])
    assert "/ checks" in out and "\\ checks" in out, out
    assert "run in PARALLEL" in out, out
    print(
        "  [G] CLI `workflow show`: ASCII group brackets '/ checks' .. '\\ checks' "
        "plus the legend line"
    )

    rc, out, err = run_cli(
        ["--json", "workflow", "edit-step", str(wid), str(step2), "--no-parallel-group"]
    )
    assert rc == 0, (out, err)
    assert json.loads(out)["parallel_group"] is None, out
    rc, out, _e = run_cli(["--json", "workflow", "show", str(wid)])
    slugs = [s["parallel_group"] for s in json.loads(out)["steps"]]
    assert slugs == [None, "checks", None, None], slugs
    print(
        "  [G] CLI edit-step --no-parallel-group leaves the group; show --json "
        f"echoes parallel_group {slugs!r}"
    )


def test_g_web_round_trip():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    wid = _mk_wf("Web")

    base = {
        "name": "T",
        "step_type": "llm_agent",
        "provider": "mock",
        "prompt_text": "Translate {text}",
        "chunk_policy": "none",
    }
    r = client.post(f"/workflows/{wid}/steps", data=base, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    for nm, key in (("L1", "w_main"), ("L2", "w_term")):
        r = client.post(
            f"/workflows/{wid}/steps",
            data={"name": nm, "step_type": "lqa", "step_key": key, "parallel_group": "checks"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (nm, r.status_code, r.text[:300])
    got = [s.parallel_group for s in _steps(wid)]
    assert got == [None, "checks", "checks"], got
    print(f"  [G] web add-step form round-trips parallel_group {got!r}")

    page = client.get(f"/workflows/{wid}").text
    assert "group: checks" in page, "the step-head chip is missing"
    assert 'name="parallel_group"' in page, "the builder form field is missing"
    assert "in parallel" in page, "the wf_graph group outline label is missing"
    print(
        "  [G] builder page: the group chip, the form field, and the wf_graph group "
        "outline all render"
    )

    # The web edit form full-POSTs; a BLANK field is a real "leave the group" edit.
    steps = _steps(wid)
    r = client.post(
        f"/workflows/{wid}/steps/{steps[2].id}/edit",
        data={"name": "L2", "step_type": "lqa", "step_key": "w_term", "parallel_group": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    assert [s.parallel_group for s in _steps(wid)] == [None, "checks", None]
    print("  [G] web edit form: a blank field left the group (a real edit, not 'unchanged')")

    # THE ADD FORM'S HIDDEN-FIELD DEAD END (adversarial review, SURFACES-9). The
    # honest-fields JS only HIDES the field; the form still POSTs it. So a slug typed
    # before switching the type to llm_agent arrives on a field with no widget on
    # screen. The web ADD route force-clears it and SAYS SO, mirroring the edit path's
    # type-switch regime, instead of refusing an error the user cannot act on.
    r = client.post(
        f"/workflows/{wid}/steps",
        data={
            "name": "Bad",
            "step_type": "llm_agent",
            "prompt_text": "{text}",
            "parallel_group": "checks",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    loc = r.headers.get("location", "")
    assert "notice=" in loc and "parallel%20group" in loc.replace("+", "%20"), loc
    made = _steps(wid)[-1]
    assert made.name == "Bad" and made.parallel_group is None, (made.name, made.parallel_group)
    print(
        "  [G] web ADD form: a hidden dead parallel_group is force-CLEARED with a "
        "notice (not an unfixable refusal); the step is still created"
    )


def test_g_shape_tag_and_group_graph():
    wid = _grouped_wf("G3")
    steps = _steps(wid)
    res = wshapes.detect_shapes(steps)
    assert wshapes.PARALLEL_CHECKS in res.tags, res.sorted_tags()
    assert wshapes.PARALLEL_CHECKS in res.additive
    assert res.primary != wshapes.PARALLEL_CHECKS, res.primary
    print(
        f"  [G] workflow_shapes: 'parallel-checks' is emitted, ADDITIVE (primary stays {res.primary!r})"  # noqa: E501
    )

    # A LONE member is not a group -- it does not fork, so it earns no tag either.
    lone = [
        _S(id=1, order_index=0, step_type="llm_agent", parallel_group=None),
        _S(id=2, order_index=1, step_type="lqa", parallel_group="g"),
    ]
    assert wshapes.PARALLEL_CHECKS not in wshapes.detect_shapes(lone).tags
    print(
        "  [G] a LONE member earns no shape tag (it does not fork -- same definition "
        "the engine uses)"
    )

    # The graph renders a group outline, and a LONE member renders none.
    grouped = [
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
            branch=None,
            max_visits=None,
            parallel_group="checks",
        ),
    ]
    svg = _render_graph(grouped)
    assert 'stroke-dasharray="3 3"' in svg and "checks (in parallel)" in svg, svg[:400]
    lone_steps = [grouped[0], grouped[1]]
    assert "(in parallel)" not in _render_graph(lone_steps)
    print(
        "  [G] wf_graph: a 2-member group draws a labelled outline; a lone member "
        "draws none (matching the engine)"
    )


# ============================================================================
# [H] ADVERSARIAL-REVIEW REGRESSIONS. One test per confirmed finding; each names
# the finding and the repro it pins. Every one FAILED against the pre-fix code
# (reconstructed by copying the branch's own earlier file over the working tree and
# back -- never `git stash`, whose stack is shared across worktrees).
# ============================================================================
def _plant_run(wid, status):
    """A WorkflowRun row in `status` for `wid`. It is never executed -- the run-safety
    guards read only (workflow_id, status) -- but document_id carries a real FK, so a
    throwaway document backs it."""
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


def test_h1_delete_step_is_guarded():
    """HIGH-1. delete_step had NEITHER of update_step's protections."""
    # (a) RUN SAFETY. A PAUSED run + a delete of an EARLIER step re-seats every later
    #     step, so the resume lands on the wrong one -- the reviewer resumed straight
    #     into the MIDDLE of a group, where _group_at declines to fork and one lane
    #     silently never ran.
    wid = _mk_wf("H1a")
    t = _translate(wid)
    _check(wid, "L1", key="h1_a", parallel_group="g")
    _check(wid, "L2", key="h1_b", parallel_group="g")
    _plant_run(wid, RunStatus.PAUSED)
    with Session(engine) as s:
        msg = _err(wf.delete_step, s, t, workflow_id=wid)
    assert "in-flight run" in msg and "resume at the wrong one" in msg, msg
    with Session(engine) as s:
        assert s.get(WorkflowStep, t) is not None, "the step was deleted anyway"
    print(f"  [H1a] delete refused while a run is PAUSED -- {msg[:88]!r}")

    # (b) THE REVIEWER'S GRAPH REPRO, verbatim: T -> [L1 || L2] -> M(lqa) ->
    #     FIX(prev_lqa:failed). FIX is UNAMBIGUOUS at save (its nearest producer is the
    #     lone M). Deleting M makes the nearest producers the two group members, so the
    #     bare prev_lqa becomes ambiguous -- and the engine would then answer with the
    #     highest-order lane only, silently.
    wid = _mk_wf("H1b")
    _translate(wid)
    _check(wid, "L1", key="h1b_a", parallel_group="checks")
    _check(wid, "L2", key="h1b_b", parallel_group="checks")
    m = _check(wid, "M", key="h1b_m")
    _translate(wid, name="Fix", key="h1b_fix", run_condition="prev_lqa:failed")
    with Session(engine) as s:
        msg = _err(wf.delete_step, s, m, workflow_id=wid)
    assert "break the workflow graph" in msg and "ambiguous" in msg, msg
    with Session(engine) as s:
        assert s.get(WorkflowStep, m) is not None
    print(
        "  [H1b] delete that would make a downstream prev_lqa AMBIGUOUS is refused "
        f"(the reviewer's repro) -- {msg[:92]!r}"
    )

    # An unrelated delete on the same workflow still works (the diff is a diff, not a
    # freeze): drop the FIX step that carried the guard, then M deletes cleanly.
    with Session(engine) as s:
        fix_id = [x for x in orchestrator.get_steps(s, wid) if x.key == "h1b_fix"][0].id  # noqa: RUF015
        assert wf.delete_step(s, fix_id, workflow_id=wid) is True
        assert wf.delete_step(s, m, workflow_id=wid) is True
    print(
        "  [H1b] ... and once the dependent guard is gone the same delete succeeds "
        "(a BEFORE/AFTER diff, never a blanket freeze)"
    )


def test_h2_add_step_uses_the_full_graph_diff():
    """HIGH-2 + MEDIUM-4 together, as the reviewer's end-to-end repro.

    order_index is NOT compacted on delete, so `len(existing)` (the old append index)
    disagrees with the spine after any delete and lands the new step MID-SPINE, where
    it fused an inert singleton into a real group and made a downstream bare prev_lqa
    ambiguous -- accepted at save. MEDIUM-4 makes the append land last; HIGH-2 makes
    add_step run the SAME whole-graph diff update_step does, so the corruption is
    refused even if a candidate ever lands mid-spine again."""
    wid = _mk_wf("H2")
    t = _translate(wid)  # order_index 0
    _check(wid, "L1", key="h2_a", parallel_group="checks")  # 1  (lone -> inert)
    m = _check(wid, "M", key="h2_m")  # 2
    _translate(
        wid, name="Fix", key="h2_fix", run_condition="prev_lqa:failed"
    )  # 3  (nearest = M: fine)
    with Session(engine) as s:
        assert wf.delete_step(s, t, workflow_id=wid) is True  # leaves 1, 2, 3
        assert wf.delete_step(s, m, workflow_id=wid) is True  # leaves 1, 3  -- len == 2
    with Session(engine) as s:
        remaining = orchestrator.get_steps(s, wid)
    assert [x.order_index for x in remaining] == [1, 3], [x.order_index for x in remaining]
    assert len(remaining) == 2, remaining
    # THE PRE-FIX CALL. `len(existing) == 2` landed this BETWEEN L1(1) and Fix(3),
    # fusing it with L1 into a real group -- after which Fix's bare `prev_lqa` had two
    # candidate producers and was silently ambiguous, because the only guard add_step
    # ran was `_group_errors`, which never looks at run_conditions. The very same call
    # is now REFUSED: MEDIUM-4 moves the landing spot to the end of the spine (where
    # Fix sits between the two members, so the group is split) and HIGH-2's whole-graph
    # diff is the checker that reports it.
    msg = _err(_check, wid, "L2", key="h2_b", parallel_group="checks")
    assert "break the workflow graph" in msg and "consecutive" in msg, msg
    print(
        "  [H2] the mid-spine fusion call is now REFUSED through add_step's "
        f"whole-graph diff -- {msg[:96]!r}"
    )

    # ... and Fix's guard is still unambiguous, i.e. nothing was written.
    with Session(engine) as s:
        fix = [x for x in orchestrator.get_steps(s, wid) if x.key == "h2_fix"][0]  # noqa: RUF015
        assert fix.run_condition["ref"] == "prev_lqa"
        order = [(x.order_index, x.key) for x in orchestrator.get_steps(s, wid)]
    assert len(order) == 2, order

    # A plain append proves the landing spot itself: max(order_index)+1, never
    # len(existing) -- so it lands AFTER the spine, not inside it.
    new_id = _translate(wid, name="Appended")
    with Session(engine) as s:
        made = s.get(WorkflowStep, new_id)
        order = [(x.order_index, x.name) for x in orchestrator.get_steps(s, wid)]
    assert made.order_index == 4, f"the append did not land after the whole spine: {order!r}"
    assert order[-1][1] == "Appended", order
    print(
        "  [M4] append lands at max(order_index)+1 == 4 (len(existing) was 2), AFTER "
        f"the spine {order!r}"
    )


def test_h2b_group_workflow_stays_addable():
    """MEDIUM-4's other half: a workflow containing a group must not become
    PERMANENTLY unaddable after ordinary deletes. Pre-fix, every later append refused
    with "group is split" and the product has no reorder affordance to escape with."""
    wid = _mk_wf("H2b")
    a = _translate(wid, name="A")
    b = _translate(wid, name="B")
    _check(wid, "L1", key="h2b_a", parallel_group="g")
    _check(wid, "L2", key="h2b_b", parallel_group="g")
    with Session(engine) as s:
        assert wf.delete_step(s, a, workflow_id=wid) is True
        assert wf.delete_step(s, b, workflow_id=wid) is True
    for n in range(3):
        _translate(wid, name="After %d" % n)  # pre-fix: refused, "group is split"  # noqa: UP031
    with Session(engine) as s:
        order = [(x.order_index, x.name) for x in orchestrator.get_steps(s, wid)]
    assert [n for _o, n in order][:2] == ["L1", "L2"], order
    assert [n for _o, n in order][2:] == ["After 0", "After 1", "After 2"], order
    print(
        f"  [H2b] delete-then-append sequences keep appending AFTER the group {[n for _o, n in order]!r} -- "  # noqa: E501
        "the split refusal is unreachable via append"
    )


def test_h3_no_self_target_exemption():
    """MEDIUM-3. The rule-5 check skipped any branch whose SOURCE was inside the
    target's group, so a routing member could target a NON-FIRST member of its OWN
    group. At run time `_group_at` returns None for a mid-group cursor, so that lane
    re-ran ALONE and its siblings' visits never bumped."""
    wid = _mk_wf("H3")
    _translate(wid)
    _check(wid, "L1", key="h3_a", parallel_group="g", max_visits=3)
    second = _check(wid, "L2", key="h3_b", parallel_group="g", max_visits=3)
    with Session(engine) as s:
        msg = _err(wf.update_step, s, second, branch={"on_fail": "h3_b"})
    assert "NON-FIRST member" in msg and "'h3_a'" in msg, msg
    print(
        "  [H3] save-time: a member's branch targeting a NON-FIRST member of its OWN "
        f"group is refused -- {msg[:96]!r}"
    )

    # The LEGITIMATE self-loop -- the SECOND member routing back to the group's FIRST
    # step -- is still accepted, and at run time it re-enters the WHOLE group.
    with Session(engine) as s:
        wf.update_step(s, second, branch={"on_fail": "h3_a"})
    pid = _mk_project("H3")
    doc = _mk_doc(pid)
    member_ids = [s.id for s in _steps(wid)[1:]]
    _reset_stub()
    _install_stub()
    try:
        _GATE_BY_PROVIDER["h3_b"] = False  # the routing member always fails
        _run(wid, doc)
    finally:
        _remove_stub()
    run = _last_run(wid)
    rows = _run_rows(run.id)
    per = {mid: [r.outcome_index for r in rows if r.step_id == mid] for mid in member_ids}
    # ISSUE #95 (LQA no-progress guard): this graph is CHECKS ONLY -- a Phase-1 lane reads
    # the working text and produces none -- so the second visit judges BYTE-IDENTICAL text.
    # Re-judging it would be a re-roll of the same judge on the same bytes, so both lanes
    # now CARRY their first verdict forward on visit 2 and the run holds there (max_visits
    # 3 is never reached). The property THIS test pins is unchanged and still asserted: the
    # loop-back re-enters the WHOLE group, both lanes together -- never one lane alone.
    assert per[member_ids[0]] == [0, 1], per
    assert per[member_ids[1]] == [0, 1], (
        f"the loop-back re-ran only one lane -- the whole group must re-enter: {per!r}"
    )
    assert per[member_ids[0]] == per[member_ids[1]], ("lanes must move together", per)
    visits = (run.context or {}).get("visits", {})
    assert visits.get("h3_a") == 2 and visits.get("h3_b") == 2, visits
    print(
        "  [H3] run-time: the legal loop-back (second member -> the group's FIRST "
        f"step) re-entered the WHOLE group; #95 then held the no-progress re-roll {visits!r}"
    )


def test_h5_skipped_latest_visit_is_no_outcome():
    """MEDIUM-5. When the keyed step's LATEST visit is SKIPPED, `step:<key>` used to
    fall through to an EARLIER visit's gate -- handing a later guard a STALE verdict the
    named step did not reach this time round. Planted rows, the reviewer's shape."""
    wid = _mk_wf("H5")
    _translate(wid)
    gate_id = _check(wid, "Gate", key="h5_gate")
    run_id = _plant_run(wid, RunStatus.RUNNING)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        s.add(
            StepRun(
                workflow_run_id=run_id,
                step_id=gate_id,
                status=RunStatus.COMPLETED,
                input_text="",
                output_text="",
                score=10.0,
                gate="failed",
                outcome_index=0,
            )
        )
        s.commit()
        got = orchestrator._latest_outcome_steprun(s, run, "step:h5_gate")
        assert got is not None and got.gate == "failed", got
        # visit 1: SKIPPED (its guard evaluated false) -> NO outcome.
        s.add(
            StepRun(
                workflow_run_id=run_id,
                step_id=gate_id,
                status=RunStatus.SKIPPED,
                input_text="",
                output_text="",
                outcome_index=1,
            )
        )
        s.commit()
        stale = orchestrator._latest_outcome_steprun(s, run, "step:h5_gate")
        assert stale is None, (
            f"step:<key> resolved to a STALE earlier gate ({stale and stale.gate!r}) after the named step's "  # noqa: E501
            "latest visit was SKIPPED"
        )
        # ... and the guard therefore fails OPEN (the step runs), like a never-run ref.
        probe = WorkflowStep(
            workflow_id=wid,
            order_index=99,
            step_type=StepType.LLM_AGENT,
            run_condition={"ref": "step:h5_gate", "op": "failed", "value": None},
        )
        assert orchestrator._eval_run_condition(probe, run, s) is True
        # prev_lqa is DELIBERATELY unchanged (it means "the most recent step OF A KIND",
        # not one named step) -- pre-parallel condwf behaviour stays byte-identical.
        assert orchestrator._latest_outcome_steprun(s, run, "prev_lqa").gate == "failed"
    print(
        "  [H5] a SKIPPED latest visit = NO outcome for step:<key> (guard fails OPEN); "
        "prev_lqa's fall-through is deliberately untouched"
    )


def test_h7_empty_step_ref_matches_nothing():
    """LOW-7. `step:` with an empty key matched every KEYLESS step's row."""
    wid = _mk_wf("H7")
    keyless = _translate(wid, name="Keyless")  # no key at all
    run_id = _plant_run(wid, RunStatus.RUNNING)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        s.add(
            StepRun(
                workflow_run_id=run_id,
                step_id=keyless,
                status=RunStatus.COMPLETED,
                input_text="",
                output_text="",
                score=5.0,
                gate="failed",
                outcome_index=0,
            )
        )
        s.commit()
        assert orchestrator._latest_outcome_steprun(s, run, "step:") is None, (
            "an empty step: ref read a KEYLESS step's gate"
        )
        # Retire the planted run: add-step is (correctly) refused while a run is in
        # flight (run-cancel train, BUG-A203), so the save-time run_condition refusal
        # below is only reachable once no in-flight run remains.
        run.status = RunStatus.COMPLETED
        s.add(run)
        s.commit()
    # Save-time refuses it outright too (this is the defense in depth).
    msg = _err(
        _translate, wid, name="X", run_condition={"ref": "step:", "op": "failed", "value": None}
    )
    assert "names no step key" in msg, msg
    print("  [H7] `step:` with an empty key resolves to None (and is refused at save)")


def test_h10_propagation_reports_run_active_and_singleton():
    """PROPAGATION-10. (a) a graph-field sweep must SKIP a variant that has its own run
    in flight -- update_step's Guard 1 only ever asked about the BASE's runs; (b) a
    group that lands as a LONE member on a variant runs SEQUENTIALLY there while the
    base runs in parallel, and that divergence must be VISIBLE."""
    # Base: T -> [L1 || L2]. Fan out to two pairs, then diverge each variant.
    base = _mk_wf("H10")
    _translate(base)
    _check(base, "L1", key="h10_a", parallel_group="checks")
    _check(base, "L2", key="h10_b", parallel_group="checks")
    with Session(engine) as s:
        made = wf.copy_to_pairs(s, base, [("en-US", "fr-FR"), ("en-US", "de-DE")], adapt=False)
    v_busy = made["created"][0]["workflow_id"]
    v_lone = made["created"][1]["workflow_id"]

    # (a) the FIRST variant gets a run in flight.
    _plant_run(v_busy, RunStatus.PAUSED)
    # (b) the SECOND variant loses its OTHER member's group (the half-applied shape).
    with Session(engine) as s:
        other = [x for x in orchestrator.get_steps(s, v_lone) if x.key == "h10_b"][0]  # noqa: RUF015
        other.parallel_group = None
        s.add(other)
        s.commit()

    # Now edit the BASE member's group -- a GRAPH-field delta.
    report: dict = {}
    with Session(engine) as s:
        base_l1 = [x for x in orchestrator.get_steps(s, base) if x.key == "h10_a"][0]  # noqa: RUF015
        wf.update_step(s, base_l1.id, parallel_group="quality", report=report)

    ras = [e["workflow_id"] for e in report.get("run_active_skipped", [])]
    assert ras == [v_busy], (ras, report)
    with Session(engine) as s:
        untouched = [x for x in orchestrator.get_steps(s, v_busy) if x.key == "h10_a"][0]  # noqa: RUF015
    assert untouched.parallel_group == "checks", (
        f"a graph field was written into a variant with a run in flight: {untouched.parallel_group!r}"  # noqa: E501
    )
    print(
        "  [H10a] a graph-field sweep SKIPPED the variant with a PAUSED run and "
        "reported it (run_active_skipped) -- its config is untouched"
    )

    pas = [(e["workflow_id"], e.get("group")) for e in report.get("propagated_as_singleton", [])]
    assert pas == [(v_lone, "quality")], (pas, report)
    print(
        "  [H10b] the variant where the group landed as a LONE member is reported "
        "distinctly (propagated_as_singleton) -- no silent base-parallel/"
        "variant-sequential divergence"
    )

    # Both buckets reach the operator on BOTH surfaces.
    from app.web.workflows import _shared_base_notice

    notice = _shared_base_notice(report)
    assert "run is in flight" in notice and "SEQUENTIALLY" in notice, notice
    print(f"  [H10] the web banner names both: {notice[-150:]!r}")

    # RESIDUAL 1: the skip must trigger on fields that SHAPE A RUN, not on every field
    # `_graph_edit_errors` happens to read. That tuple also carries `name` (used only to
    # LABEL an error), so a plain RENAME used to be treated as a graph change and
    # skipped propagation to the busy variant -- a false refusal on the most innocuous
    # edit there is. Nothing routes by name.
    report2: dict = {}
    with Session(engine) as s:
        base_l1 = [x for x in orchestrator.get_steps(s, base) if x.key == "h10_a"][0]  # noqa: RUF015
        wf.update_step(s, base_l1.id, name="Renamed check", report=report2)
    assert report2.get("run_active_skipped") == [], report2
    assert v_busy in [e["workflow_id"] for e in report2.get("propagated", [])], report2
    with Session(engine) as s:
        swept = [x for x in orchestrator.get_steps(s, v_busy) if x.key == "h10_a"][0]  # noqa: RUF015
    assert swept.name == "Renamed check", swept.name
    print(
        "  [H10c] a RENAME-only edit still propagates to the variant with a paused "
        "run (nothing routes by name); only run-SHAPING fields skip it"
    )


def test_h11_diff_messages_use_a_stable_ref():
    """RESIDUAL 3. Messages that PARTICIPATE in the before/after diff must not embed
    anything that can drift for an unrelated reason. The rule-5 branch message named
    the group's first member through `_step_label`, which falls back to the step's
    NAME -- so RENAMING a KEYLESS first member rewrote a PRE-EXISTING error, the diff
    read it as NEW, and an innocent rename was refused."""
    wid = _mk_wf("H11")
    _translate(wid)
    with Session(engine) as s:
        # Planted directly: this graph is exactly what the validators refuse, which is
        # the point -- it stands in for a legacy/engine-tolerated row.
        s.add(
            WorkflowStep(
                workflow_id=wid,
                order_index=1,
                name="Alpha",
                step_type=StepType.LQA,
                parallel_group="g",
            )
        )  # keyless FIRST
        s.add(
            WorkflowStep(
                workflow_id=wid,
                order_index=2,
                name="Beta",
                key="r3_b",
                step_type=StepType.LQA,
                parallel_group="g",
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wid,
                order_index=3,
                name="Fix",
                key="r3_fix",
                step_type=StepType.LQA,
                branch={"on_fail": {"step": "r3_b"}},
            )
        )
        s.commit()
        steps = orchestrator.get_steps(s, wid)
        pre = wf._graph_edit_errors(steps)
    assert any("NON-FIRST member" in m for _i, _l, m, _id in pre), pre
    first_id = [x for x in steps if x.name == "Alpha"][0].id  # noqa: RUF015
    with Session(engine) as s:
        wf.update_step(s, first_id, name="Alpha renamed")
    with Session(engine) as s:
        assert s.get(WorkflowStep, first_id).name == "Alpha renamed"
        msgs = [m for _i, _l, m, _id in wf._graph_edit_errors(orchestrator.get_steps(s, wid))]
    assert not any("Alpha" in m for m in msgs), (
        f"a diff-participating message still embeds the step NAME: {msgs!r}"
    )
    print(
        "  [H11] rule-5 messages name steps by key/#id, so renaming a KEYLESS member "
        "no longer makes a pre-existing error read as NEW"
    )


def test_h8_lone_member_chip_is_honest():
    """SURFACES-8. The builder chip's title claimed concurrent execution even for an
    INERT lone member."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    wid = _mk_wf("H8")
    _translate(wid)
    _check(wid, "Lone", key="h8_lone", parallel_group="solo")
    page = client.get(f"/workflows/{wid}").text
    assert "group: solo" in page, "the chip must still render for a lone member"
    assert "Not active: no adjacent step shares this group" in page, page[:200]
    assert "Runs at the same time as the neighbouring steps" not in page, (
        "the chip claimed concurrency a LONE member will never get"
    )
    _check(wid, "Second", key="h8_two", parallel_group="solo")
    page = client.get(f"/workflows/{wid}").text
    assert "Runs at the same time as the neighbouring steps" in page, page[:200]
    print(
        "  [H8] the chip title is conditional: 'not active' for a lone member, the "
        "concurrency claim only once an adjacent step shares the group"
    )

    from tests._cli_runner import run_cli

    rc, out, _e = run_cli(["workflow", "show", str(wid)])
    assert rc == 0 and "A '.' marks a LONE member" in out, out
    print("  [H8] the CLI legend explains the '.' lone-member marker alongside / | \\")


def main():
    test_a_membership_rules()
    test_a_diff_dead_fields_and_copies()
    test_b_condition_refs()
    test_b_non_routing_lane_is_addressable()
    test_c_concurrency_barrier()
    test_c_frozen_snapshot()
    test_c_one_commit_join_and_id_order()
    test_c_all_skip_group_is_a_noop()
    test_c_member_failure_fails_run_after_all_persist()
    test_c_loopback_reenters_the_whole_group()
    test_c_loop_cap_trips_before_any_dispatch()
    test_c_full_text_mirror()
    test_d_routing_at_the_join()
    test_e_no_open_write_transaction_at_dispatch()
    test_e_spend_tap_and_replay()
    test_f1_seam_unreachable_when_no_groups()
    test_f2_armed_vs_disarmed_equivalence()
    test_f3_ungrouped_svg_snapshot()
    test_f3b_grouped_svg_snapshot()
    test_g_cli_round_trip()
    test_g_web_round_trip()
    test_g_shape_tag_and_group_graph()
    test_h1_delete_step_is_guarded()
    test_h2_add_step_uses_the_full_graph_diff()
    test_h2b_group_workflow_stays_addable()
    test_h3_no_self_target_exemption()
    test_h5_skipped_latest_visit_is_no_outcome()
    test_h7_empty_step_ref_matches_nothing()
    test_h8_lone_member_chip_is_honest()
    test_h11_diff_messages_use_a_stable_ref()
    test_h10_propagation_reports_run_active_and_singleton()
    print("PARALLEL ROUTING OK")


if __name__ == "__main__":
    main()
