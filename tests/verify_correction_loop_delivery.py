"""#156: a correction loop that REPAIRS the text must not hold delivery.

The maintainer found this shooting the demo video against prod (run #208, workflow #61): the
canonical loop did exactly what it exists to do -- first check FAILED, the fix step
repaired, the re-check PASSED at 98 -- and the export was still refused permanently, with
a message claiming there was "no correction loop to repair the text".

WHY. `_terminal_gate_hold` holds when ANY gate's STANDING verdict is `failed`.
`_standing_gate_verdicts` documents itself as "a loop that failed and then passed stands
at passed" -- true when the SAME check re-runs, which is the one-check loop shape it was
written for. The shape the app now ships by default has TWO checks: `lqa2` loops back to
`fix`, so `lqa1` never re-runs and stands `failed` for the life of the run. Its verdict is
about text that no longer exists, because the fix step replaced it.

THE RULE THIS PINS. A gate's failed verdict is SUPERSEDED when, later in the same run, a
text-modifying step ran AND a later gate passed. Both halves are load-bearing: without the
text-modifying half, a second unrelated check passing would release a failure that nothing
ever tried to repair.

ASCII-only. Prints "CORRECTION LOOP DELIVERY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_loopdeliv_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import export_gate, orchestrator
from app.services.providers import register_provider

init_db()

SOURCE = "The battery indicator shows the remaining charge."
REPAIRED = "El indicador de bateria muestra la carga restante."


class Scripted:
    """First llm_agent call echoes the SOURCE (an untranslated segment, which the offline
    LQA scores as a Major error -> the gate FAILS). Every later call returns a real-looking
    translation, so the re-check PASSES. That is the canonical repair, driven by text."""

    CAPS = type("C", (), {"supports_prompts": True, "supports_system": True})()

    def __init__(self):
        self.calls = 0

    def _answer(self):
        self.calls += 1
        return SOURCE if self.calls == 1 else REPAIRED

    def complete(self, prompt, **kw):
        return self._answer()

    def translate(self, text, **kw):
        return self._answer()


def _world(name, *, steps_spec):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US", target_locales="es-419")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename=f"{name}.txt", fmt="txt", status="new")
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale="es-419",
                order_index=0,
                source_text=SOURCE,
                target_text="",
            )
        )
        w = Workflow(name=name)
        s.add(w)
        s.commit()
        s.refresh(w)
        for i, spec in enumerate(steps_spec):
            st = WorkflowStep(workflow_id=w.id, order_index=i, **spec)
            if st.step_type == StepType.LLM_AGENT and not st.prompt_text:
                st.prompt_text = "Translate {text} into {target_locale}."
            s.add(st)
        s.commit()
        return d.id, w.id


def _run(doc_id, wf_id, provider_name):
    register_provider(provider_name, Scripted())
    with Session(engine) as s:
        orchestrator.run_workflow(s, doc_id, wf_id, "es-419")


def _held(doc_id):
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        return (
            export_gate.gate_held_reason(session=s, doc=doc, locale="es-419")
            if hasattr(export_gate, "gate_held_reason")
            else export_gate._gate_held_reason_for_docs(s, [doc_id], "es-419", unit="segment")
        )


def _flags(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment.status, Segment.qa_flags).where(Segment.document_id == doc_id)
        ).all()
        return rows


CANONICAL = [
    {"name": "Translate", "step_type": StepType.LLM_AGENT, "provider": "loop156"},
    {
        "name": "Initial check",
        "step_type": StepType.LQA,
        "key": "lqa1",
        "lqa_threshold": 90,
        "branch": {"on_pass": "end", "on_fail": "next"},
    },
    {
        "name": "Post-Edit Fix",
        "step_type": StepType.LLM_AGENT,
        "provider": "loop156",
        "key": "fix",
        "run_condition": {"ref": "prev_lqa", "op": "failed", "value": None},
        "max_visits": 2,
        "prompt_text": "Fix {prior} into {target_locale}.",
    },
    {
        "name": "Re-check",
        "step_type": StepType.LQA,
        "key": "lqa2",
        "lqa_threshold": 90,
        "branch": {"on_pass": "end", "on_fail": {"step": "fix"}},
    },
]


def test_repaired_loop_delivers():
    """#156: fail -> fix -> pass must NOT hold delivery."""
    doc_id, wf_id = _world("repaired", steps_spec=CANONICAL)
    _run(doc_id, wf_id, "loop156")
    held = _held(doc_id)
    assert held is None, (
        f"#156: a correction loop that repaired the text must not hold delivery; got:\n  {held}"
    )
    print("  a repaired correction loop delivers (fail -> fix -> pass) OK")


def test_unrepaired_check_still_holds():
    """The protection this hold exists for: a failing check with NO repair after it."""
    spec = [
        {"name": "Translate", "step_type": StepType.LLM_AGENT, "provider": "loop156b"},
        {"name": "Only check", "step_type": StepType.LQA, "key": "lqa1", "lqa_threshold": 90},
    ]
    doc_id, wf_id = _world("unrepaired", steps_spec=spec)
    _run(doc_id, wf_id, "loop156b")
    held = _held(doc_id)
    assert held is not None, "a failing check with nothing to repair it MUST still hold"
    print("  an unrepaired failing check still holds delivery OK")


def test_later_pass_without_a_repair_still_holds():
    """The half that stops this becoming 'any later pass clears anything': a second check
    passing with NO text-modifying step between them repairs nothing, so the hold stands."""
    spec = [
        {"name": "Translate", "step_type": StepType.LLM_AGENT, "provider": "loop156c"},
        {"name": "Check A", "step_type": StepType.LQA, "key": "lqa1", "lqa_threshold": 90},
        {"name": "Check B", "step_type": StepType.LQA, "key": "lqa2", "lqa_threshold": 0},
    ]
    doc_id, wf_id = _world("nolrepair", steps_spec=spec)
    _run(doc_id, wf_id, "loop156c")
    held = _held(doc_id)
    assert held is not None, (
        "a later gate passing with NO repair between them must NOT release the hold"
    )
    print("  a later pass with no repair between does NOT release the hold OK")


def test_healthy_run_is_unchanged():
    """Byte-identical for the common case: every gate passes, nothing is held."""
    spec = [
        {
            "name": "Translate",
            "step_type": StepType.LLM_AGENT,
            "provider": "loop156d",
            "prompt_text": "Translate {text} into {target_locale}.",
        },
        {"name": "Check", "step_type": StepType.LQA, "key": "lqa1", "lqa_threshold": 0},
    ]
    doc_id, wf_id = _world("healthy", steps_spec=spec)

    class AlwaysGood(Scripted):
        """No source echo: every answer is a real translation, so the gate passes first
        time. This is the ordinary workflow whose behaviour must not change at all."""

        def _answer(self):
            return REPAIRED

    register_provider("loop156d", AlwaysGood())
    with Session(engine) as s:
        orchestrator.run_workflow(s, doc_id, wf_id, "es-419")
    assert _held(doc_id) is None, f"a healthy run must not hold: {_held(doc_id)}"
    print("  a healthy run holds nothing (unchanged) OK")


if __name__ == "__main__":
    test_repaired_loop_delivers()
    test_unrepaired_check_still_holds()
    test_later_pass_without_a_repair_still_holds()
    test_healthy_run_is_unchanged()
    print("CORRECTION LOOP DELIVERY OK")
