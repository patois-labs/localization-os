"""Issue #512 (workflow + key edge cases), the items a code defect remained for.

  (1) STEP-TYPE SWITCH vs GATE VALIDATION: switching a QE-gated llm_agent step to
      human_review (the web form posts the hidden qe_gate=1) must be judged against
      the state the edit PRODUCES (qe_gate cleared). A branch on the step, or a later
      prev_qe guard that reads its gate, can never be satisfied, so the switch is
      refused and nothing changes. A switch with no gate dependents still saves, and a
      switch to lqa (which always produces a gate) is unaffected.
  (3) add_to_group refuses a second variant for a (source, target) pair the group
      already covers (copy_to_pairs already did, #46): dispatch would always pick the
      lower id and the newer workflow would silently never run. Different pair, a same
      pair re-state of the member itself, and a universal add stay legal.
  (4) trash moves are not quadratic: moving a key's value rows into the trash
      document and back reads the append slot once per locale (an aggregate), not one
      full order_index load per row.

(2) char_limit on the key edit seam is another queue's item, not this suite's.
Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII-only. Final line: "I512 WORKFLOW KEYS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_i512_")

from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import ProjectType, Segment, StepType, Workflow, WorkflowStep
from app.services import content, keys
from app.services import workflows as wf

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _raises(fn):
    try:
        fn()
    except wf.WorkflowError as exc:
        return str(exc)
    return None


def _new_wf(name):
    with Session(engine) as s:
        w = Workflow(name=name, scope="global")
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _add(session, wid, **kw):
    kw.setdefault("prompt_text", "Translate: {text}")
    return wf.add_step(session, wid, **kw)


# ---------------------------------------------------------------- (1) type switch
def test_switch_refused_when_gate_dependents_exist():
    # a later prev_qe guard reads the gate the switch would clear
    wid = _new_wf("i512-guard")
    with Session(engine) as s:
        q = _add(s, wid, name="Draft", qe_gate=True, key="q")
        _add(s, wid, name="Fix", run_condition="prev_qe:failed", prompt_text="Fix: {text}")
        qid = q.id
    with Session(engine) as s:
        msg = _raises(lambda: wf.update_step(s, qid, step_type=StepType.HUMAN_REVIEW, qe_gate=True))
        if msg is None:
            _fail("QE-gated llm_agent -> human_review orphaning a prev_qe guard was saved")
    with Session(engine) as s:
        row = s.get(WorkflowStep, qid)
        if row.step_type != StepType.LLM_AGENT or row.qe_gate is not True:
            _fail("refused switch changed the step")
    # the same without the hidden form field (qe_gate carried over from the row)
    with Session(engine) as s:
        if _raises(lambda: wf.update_step(s, qid, step_type=StepType.HUMAN_REVIEW)) is None:
            _fail("carried-over qe_gate switch was saved")
    print("  (1a) switch that orphans a prev_qe guard is REFUSED, row untouched: OK")


def test_switch_refused_when_own_branch_needs_gate():
    wid = _new_wf("i512-branch")
    with Session(engine) as s:
        _add(s, wid, name="Fix", key="fix", prompt_text="Fix: {text}")
        q = _add(
            s,
            wid,
            name="Draft",
            qe_gate=True,
            key="q",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        qid = q.id
    with Session(engine) as s:
        if _raises(lambda: wf.update_step(s, qid, step_type=StepType.HUMAN_REVIEW, qe_gate=True)):
            pass
        else:
            _fail("a step keeping a gate branch after losing its gate was saved")
    with Session(engine) as s:
        if s.get(WorkflowStep, qid).step_type != StepType.LLM_AGENT:
            _fail("refused switch changed the step type")
    print("  (1b) switch that leaves a gate branch on a gate-less step is REFUSED: OK")


def test_switch_without_dependents_still_saves():
    wid = _new_wf("i512-ok")
    with Session(engine) as s:
        q = _add(s, wid, name="Draft", qe_gate=True, key="q")
        qid = q.id
    with Session(engine) as s:
        wf.update_step(s, qid, step_type=StepType.HUMAN_REVIEW, qe_gate=True)
    with Session(engine) as s:
        row = s.get(WorkflowStep, qid)
        if row.step_type != StepType.HUMAN_REVIEW or row.qe_gate:
            _fail("a harmless switch should save and clear qe_gate")
    # llm_agent(qe) -> lqa produces a gate regardless: guard keeps resolving
    wid2 = _new_wf("i512-lqa")
    with Session(engine) as s:
        _add(s, wid2, name="Fix", key="fix", prompt_text="Fix: {text}")
        q = _add(
            s,
            wid2,
            name="Draft",
            qe_gate=True,
            key="q",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        qid2 = q.id
    with Session(engine) as s:
        wf.update_step(s, qid2, step_type=StepType.LQA, qe_gate=True, prompt_text="")
    with Session(engine) as s:
        if s.get(WorkflowStep, qid2).step_type != StepType.LQA:
            _fail("switch to lqa should still save")
    print("  (1c) switches with no gate dependents (human_review, lqa) still save: OK")


# ---------------------------------------------------------------- (3) group pairs
def test_add_to_group_refuses_duplicate_pair():
    with Session(engine) as s:
        grp = wf.create_group(s, "i512-dup")
        gid = grp.id
    a, b, c = _new_wf("dup-a"), _new_wf("dup-b"), _new_wf("dup-c")
    with Session(engine) as s:
        wf.add_to_group(s, a, gid, source_locale="en-US", target_locale="fr-FR")
    with Session(engine) as s:
        msg = _raises(
            lambda: wf.add_to_group(s, b, gid, source_locale="en-US", target_locale="fr-FR")
        )
        if msg is None:
            _fail("a second en-US:fr-FR variant was added")
        if "already has" not in msg:
            _fail("refusal should say the group already has the pair: " + msg)
    with Session(engine) as s:
        if s.get(Workflow, b).group_id is not None:
            _fail("refused add still attached the workflow")
        # spelled differently is the same pair
        if (
            _raises(
                lambda: wf.add_to_group(s, b, gid, source_locale="en_us", target_locale="FR-fr")
            )
            is None
        ):
            _fail("a differently spelled duplicate pair was added")
        # a different pair, and a universal variant, are legal
        wf.add_to_group(s, b, gid, source_locale="en-US", target_locale="de-DE")
        wf.add_to_group(s, c, gid)
    # re-stating the member's own pair and flagging default stays legal
    with Session(engine) as s:
        wf.add_to_group(s, a, gid, source_locale="en-US", target_locale="fr-FR", make_default=True)
        wf.add_to_group(s, a, gid, make_default=True)
    # moving a member onto a pair another member holds is refused too (force or not)
    with Session(engine) as s:
        msg = _raises(
            lambda: wf.add_to_group(
                s, b, gid, source_locale="en-US", target_locale="fr-FR", force=True
            )
        )
        if msg is None:
            _fail("--force must not create a duplicate pair")
    with Session(engine) as s:
        pairs = sorted(
            (w.source_locale, w.target_locale) for w in wf.group_variants(s, gid) if w.source_locale
        )
        if pairs != [("en-US", "de-DE"), ("en-US", "fr-FR")]:
            _fail("group pairs changed unexpectedly: %r" % (pairs,))  # noqa: UP031
    print("  (3) add_to_group refuses a duplicate pair (any spelling, even --force): OK")


def test_duplicate_guard_ignores_siblings_that_never_compete():
    """Review: only a sibling that could be dispatched alongside the member holds the pair.
    An ARCHIVED variant is retired, and variants scoped to DIFFERENT customers each run
    only in their own ring, so neither blocks (nor is named in a refusal)."""
    import datetime

    from app.models import Customer

    with Session(engine) as s:
        c1, c2 = Customer(name="i512 c1"), Customer(name="i512 c2")
        s.add(c1)
        s.add(c2)
        s.commit()
        s.refresh(c1)
        s.refresh(c2)
        cid1, cid2 = c1.id, c2.id
        gid = wf.create_group(s, "i512-compete").id

    def _cust_wf(name, cust):
        with Session(engine) as s:
            w = Workflow(name=name, scope="global", customer_id=cust)
            s.add(w)
            s.commit()
            s.refresh(w)
            return w.id

    old, repl = _new_wf("cmp-old"), _new_wf("cmp-repl")
    with Session(engine) as s:
        wf.add_to_group(s, old, gid, source_locale="en-US", target_locale="fr-FR")
        row = s.get(Workflow, old)
        row.archived_at = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        s.add(row)
        s.commit()
    with Session(engine) as s:
        if _raises(
            lambda: wf.add_to_group(s, repl, gid, source_locale="en-US", target_locale="fr-FR")
        ):
            _fail("an ARCHIVED variant must not block a replacement for its pair")
    a, b, g = _cust_wf("cmp-c1", cid1), _cust_wf("cmp-c2", cid2), _new_wf("cmp-glob")
    with Session(engine) as s:
        wf.add_to_group(s, a, gid, source_locale="en-US", target_locale="de-DE")
        if _raises(
            lambda: wf.add_to_group(s, b, gid, source_locale="en-US", target_locale="de-DE")
        ):
            _fail("another customer's variant must not block this customer's own variant")
        msg = _raises(
            lambda: wf.add_to_group(s, g, gid, source_locale="en-US", target_locale="de-DE")
        )
        if msg is None:
            _fail("a GLOBAL variant still competes with a customer variant for the pair")
    print("  (3b) archived and other-customer siblings do not hold the pair: OK")


# ---------------------------------------------------------------- (4) trash moves
def test_trash_move_reads_the_append_slot_once_per_locale():
    with Session(engine) as s:
        p = content.project_create(
            s,
            "I512-keys",
            source_locale="en-US",
            target_locales="es-ES,de-DE",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
    with Session(engine) as s:
        proj = s.get(type(p), pid)
        # Gate: key 0 is PLURAL, so each locale holds SEVERAL value rows (one per form).
        # With one row per locale "once per locale" and "once per row" are the same
        # count, and the per-row read (main's #576 aggregate) passed this test unfixed.
        made = [
            keys.create_key(s, proj, "k%d" % i, base_value="Value %d" % i, is_plural=(i == 0))  # noqa: UP031
            for i in range(12)
        ]
        ids = [k.id for k in made]
    stmts: list[str] = []

    def _grab(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)

    event.listen(engine, "before_cursor_execute", _grab)
    try:
        with Session(engine) as s:
            key = s.get(keys.StringKey, ids[0])
            rows = keys.values_for(s, key)
            n_rows = len(rows)
            n_locales = len({r.target_locale for r in rows})
            del stmts[:]
            from app.services import keys_hub, keys_hub2

            doc = keys_hub.trash_document(s, s.get(type(p), key.project_id))
            doc_id = doc.id
            del stmts[:]
            moved = keys_hub2._move_value_rows(s, key, doc)
            s.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _grab)
    if moved != n_rows or n_rows <= n_locales:
        _fail("expected the key's value rows to move (%s of %s)" % (moved, n_rows))  # noqa: UP031
    slot_reads = [
        q
        for q in stmts
        if "order_index" in q and q.lstrip().upper().startswith("SELECT") and "max(" in q.lower()
    ]
    full_loads = [
        q
        for q in stmts
        if q.lstrip().upper().startswith("SELECT")
        and "segment.order_index" in q.lower()
        and "max(" not in q.lower()
        and "segment.id" not in q.lower()
    ]
    if full_loads:
        _fail("the append slot still loads every order_index: %r" % (full_loads[0][:120],))  # noqa: UP031
    if len(slot_reads) > n_locales:
        _fail(
            "append slot read %d times for %d rows in %d locales"  # noqa: UP031
            % (len(slot_reads), n_rows, n_locales)
        )
    # the rows really landed with distinct, increasing slots per locale
    with Session(engine) as s:
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        seen = {(g.target_locale, g.order_index) for g in segs}
        if len(seen) != len(segs):
            _fail("moved rows share an order_index slot")
    print("  (4) trash move reads the append slot once per locale via an aggregate: OK")


def main():
    test_switch_refused_when_gate_dependents_exist()
    test_switch_refused_when_own_branch_needs_gate()
    test_switch_without_dependents_still_saves()
    test_add_to_group_refuses_duplicate_pair()
    test_duplicate_guard_ignores_siblings_that_never_compete()
    test_trash_move_reads_the_append_slot_once_per_locale()
    print("I512 WORKFLOW KEYS OK")


if __name__ == "__main__":
    main()
