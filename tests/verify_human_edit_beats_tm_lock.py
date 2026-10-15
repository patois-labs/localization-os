"""#154: a human reviewer's edit must beat a TM lock STORED BY AN EARLIER RUN.

Found on prod (campaign qa-0916w). A segment locked by a previous run's `tm_match` step is
still SHOWN to a reviewer at a `human_review` pause; the reviewer edits it; the resume is
accepted; the run reports `completed` with no warning -- and the edit was discarded, because
the persisted-lock seed overwrote `working[]` with the stored TM target before anything could
notice, and the machine write-back seam then refused the slot as hard-locked.

The seam's hard-lock gate documents its assumption plainly: "A HUMAN edit does NOT reach this
seam -- the CAT editor / key-grid saves are direct writers ... and they CLEAR the lock at
their own save site". That is true of the CAT editor and false of a human_review resume,
whose text goes back through this machine path.

The fix reuses the CAT editor's own audited override (`tm_lock.clear_lock_on_human_edit`,
"human edit wins, no stale lies") and classifies the edit from the PARKED BLOB, never from
divergence against the stored target -- the reasoning is the status-protection block's and
applies identically: divergence has two indistinguishable sources, an operator pause-edit and
a concurrent CAT approval landing mid-pause.

ASCII-only. Prints "HUMAN EDIT BEATS TM LOCK OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_humanlock_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Document, Project, RunStatus, Segment, StepType, Workflow, WorkflowStep
from app.services import orchestrator, tm_lock
from app.services.providers import register_provider

init_db()

SRC = ["Alpha source one.", "Beta source two.", "Gamma source three."]
TM_TEXT = "LOCKED-TM-TARGET"


class Echo:
    """A provider that marks its output, so machine text is never mistaken for human text."""

    CAPS = type("C", (), {"supports_prompts": True, "supports_system": True})()

    def complete(self, prompt, **kw):
        return "MT-OUTPUT"

    def translate(self, text, **kw):
        return "MT-OUTPUT"


register_provider("echo154", Echo())


def _world(name, *, lock_indexes, hard=True):
    """A document whose `lock_indexes` segments carry a PERSISTED lock, as if an earlier
    run's tm_match step had filled and locked them."""
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US", target_locales="es-419")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename=f"{name}.txt", fmt="txt", status="new")
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(SRC):
            seg = Segment(
                document_id=d.id,
                target_locale="es-419",
                order_index=i,
                source_text=src,
                target_text="",
            )
            if i in lock_indexes:
                seg.target_text = f"{TM_TEXT}-{i}"
                tm_lock._stamp_lock(seg, band=100, hard=hard, tm_id=None, entry_id=None)
            s.add(seg)
        w = Workflow(name=f"{name} wf")
        s.add(w)
        s.commit()
        s.refresh(w)
        s.add(
            WorkflowStep(
                workflow_id=w.id,
                order_index=0,
                name="Translate",
                step_type=StepType.LLM_AGENT,
                provider="echo154",
                prompt_text="Translate {text} into {target_locale}.",
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=w.id,
                order_index=1,
                name="Review",
                step_type=StepType.HUMAN_REVIEW,
            )
        )
        s.commit()
        return d.id, w.id


def _run(doc_id, wf_id):
    with Session(engine) as s:
        for _ in orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-419"):
            pass
        run = s.exec(
            select(orchestrator.WorkflowRun)
            .where(orchestrator.WorkflowRun.document_id == doc_id)
            .order_by(orchestrator.WorkflowRun.id.desc())
        ).first()
        return run.id, run.status


def _targets(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == doc_id).order_by(Segment.order_index)
        ).all()
        return [r.target_text for r in rows]


def _locks(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == doc_id).order_by(Segment.order_index)
        ).all()
        return [bool(r.lock_band) for r in rows]


def test_human_edit_lands_on_a_stored_hard_lock():
    """THE PROD SHAPE: segments 0 and 1 carry a hard lock from an earlier run."""
    doc_id, wf_id = _world("locked", lock_indexes={0, 1})
    run_id, status = _run(doc_id, wf_id)
    assert status == RunStatus.PAUSED, f"the run must pause for review, got {status}"

    edits = ["HUMAN-EDIT-0", "HUMAN-EDIT-1", "HUMAN-EDIT-2"]
    with Session(engine) as s:
        seed = orchestrator.prepare_resume_seed(s, run_id, edited_segments=edits, actor="rev")
        orchestrator.resume_workflow(s, run_id, "es-419", seed)

    got = _targets(doc_id)
    assert got == edits, (
        "#154: EVERY reviewer edit must land, including on segments carrying a lock from an "
        f"earlier run. got {got!r}"
    )
    # "no stale lies": the row must not keep a lock pointing at text a human replaced.
    assert _locks(doc_id) == [False, False, False], (
        f"#154: an edited slot must not keep its stale lock, got {_locks(doc_id)}"
    )
    print("  a reviewer's edits beat stored HARD locks, and the stale locks are cleared OK")


def test_unedited_locked_slots_keep_their_lock_and_text():
    """The protection still protects: a reviewer who changes NOTHING leaves the lock alone."""
    doc_id, wf_id = _world("untouched", lock_indexes={0, 1})
    run_id, status = _run(doc_id, wf_id)
    assert status == RunStatus.PAUSED

    with Session(engine) as s:
        paused = orchestrator._paused_blob(s, run_id)
    parked = paused.split(orchestrator.SEG_JOINER)
    assert parked[0] == f"{TM_TEXT}-0", f"the pause must show the TM text, got {parked[0]!r}"

    with Session(engine) as s:
        seed = orchestrator.prepare_resume_seed(s, run_id, edited_segments=parked, actor="rev")
        orchestrator.resume_workflow(s, run_id, "es-419", seed)

    got = _targets(doc_id)
    assert got[0] == f"{TM_TEXT}-0" and got[1] == f"{TM_TEXT}-1", (
        f"an unedited locked slot must keep its TM text, got {got!r}"
    )
    assert _locks(doc_id)[:2] == [True, True], (
        f"an unedited locked slot must KEEP its lock, got {_locks(doc_id)}"
    )
    print("  unedited locked slots keep both their TM text and their lock OK")


def test_soft_lock_edit_also_lands():
    """A soft lock is the weaker claim; a reviewer's edit must beat it too."""
    doc_id, wf_id = _world("soft", lock_indexes={0}, hard=False)
    run_id, _ = _run(doc_id, wf_id)
    edits = ["HUMAN-SOFT-0", "MT-OUTPUT", "MT-OUTPUT"]
    with Session(engine) as s:
        seed = orchestrator.prepare_resume_seed(s, run_id, edited_segments=edits, actor="rev")
        orchestrator.resume_workflow(s, run_id, "es-419", seed)
    assert _targets(doc_id)[0] == "HUMAN-SOFT-0", _targets(doc_id)
    print("  a reviewer's edit beats a stored SOFT lock too OK")


def test_no_locks_is_unchanged():
    """The control from the prod comparison: a document that never met tm_match."""
    doc_id, wf_id = _world("nolocks", lock_indexes=set())
    run_id, _ = _run(doc_id, wf_id)
    edits = ["CTRL-0", "CTRL-1", "CTRL-2"]
    with Session(engine) as s:
        seed = orchestrator.prepare_resume_seed(s, run_id, edited_segments=edits, actor="rev")
        orchestrator.resume_workflow(s, run_id, "es-419", seed)
    assert _targets(doc_id) == edits, _targets(doc_id)
    print("  a never-locked document is unchanged (the prod control) OK")


if __name__ == "__main__":
    test_human_edit_lands_on_a_stored_hard_lock()
    test_unedited_locked_slots_keep_their_lock_and_text()
    test_soft_lock_edit_also_lands()
    test_no_locks_is_unchanged()
    print("HUMAN EDIT BEATS TM LOCK OK")
