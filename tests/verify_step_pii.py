"""Offline #381 provider captures with global redaction disabled."""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._bootstrap import init_env

init_env("loctms_step_pii_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Project, StepType, TargetLocale, TransMode, Workflow, WorkflowStep
from app.services import (
    ingest,
    lqa,
    orchestrator,
    orchestrator_part8,
    pii_boundary,
    settings,
    tag_llm_repair,
)
from app.services.providers import ProviderSpec, register_provider
from app.services.providers_common import Provider

SEEN = []
FIELDS = ("tm", "memory", "brief", "feedback", "glossary", "style", "findings", "system")
VALUES = [f"{field}@example.test" for field in FIELDS]
SOURCE = "Contact source@example.test."
RETRY = False
TAG_TASKS = []


class Recording(Provider):
    name = "stepprivacy"

    def complete(self, prompt, system=None, task=None, payload=None, **kwargs):
        SEEN.append((prompt, system, payload))
        if RETRY == "tags":
            TAG_TASKS.append(task)
            if task == "tag_repair":
                return payload["source"]
            if task == "tag_verify":
                return json.dumps({"correct": True})
            return re.sub(r"«TAG[^»]+»", "", (payload or {}).get("text", ""))
        if task == "lqa":
            return json.dumps({"errors": []})
        if RETRY == "transport" and len(SEEN) == 1:
            raise ConnectionError("offline retry probe")
        if RETRY and len(SEEN) == 1:
            return "```\npage dump\n```"
        return (payload or {}).get("text", "Bonjour.")


def segment_case(enabled, retry=False):
    global RETRY
    RETRY = retry
    SEEN.clear()
    step = WorkflowStep(step_type=StepType.LLM_AGENT, redact_pii=enabled, chunk_policy="none")
    result = orchestrator_part8._run_step_on_segment(
        ProviderSpec(provider="stepprivacy", model="mock", retry_max=1, backoff_base=0),
        "{text}\n{source}\n{glossary}\n{style_guide}\n{lqa_findings}",
        {"source_locale": "en", "style_guide": VALUES[5], "lqa_findings": VALUES[6]},
        step,
        SOURCE,
        SOURCE,
        "fr",
        [{"source_term": VALUES[4], "target_term": VALUES[4], "dnt": False}],
        redact=enabled,
        tm_examples_block=VALUES[0],
        memory_block=VALUES[1],
        brief_block=VALUES[2],
        feedback_block=VALUES[3],
        findings_suffix="\n" + VALUES[6],
        system_head=VALUES[7],
    )
    assert result["output"] == SOURCE, result
    assert len(SEEN) == (2 if retry else 1), SEEN
    for row in SEEN:
        wire = json.dumps(row)
        for value in [*VALUES, "source@example.test"]:
            assert (value not in wire) if enabled else (value in wire), (value, row)
    RETRY = False


def workflow_case(mode, judge=False, parallel=False, brief=False, tags=False, enabled=True):
    global RETRY
    RETRY = "tags" if tags else False
    SEEN.clear()
    TAG_TASKS.clear()
    with Session(engine) as session:
        project = Project(name="Step privacy", source_locale="en")
        workflow = Workflow(name="Step privacy", scope="global")
        session.add_all([project, workflow])
        session.commit()
        session.add(TargetLocale(project_id=project.id, locale="fr"))
        session.commit()
        doc = ingest.ingest_document(
            session,
            project,
            "pii.html" if tags else "pii.txt",
            b"<p>Contact <b>source@example.test</b> today please.</p>" if tags else SOURCE.encode(),
            mode,
        )
        for index in range(2 if parallel else 1):
            session.add(
                WorkflowStep(
                    workflow_id=workflow.id,
                    order_index=index,
                    step_type=StepType.LQA if judge else StepType.LLM_AGENT,
                    provider="stepprivacy",
                    model="mock",
                    redact_pii=enabled,
                    prompt_text="{text}\n" + " ".join(VALUES),
                    chunk_policy="none",
                    parallel_group="judges" if parallel else None,
                    doc_context_prepass=brief,
                )
            )
        session.commit()
        with (
            patch.object(lqa, "provider_is_live", return_value=True),
            patch.object(lqa, "load_rulebook", return_value="Rules: " + " ".join(VALUES)),
            patch.object(tag_llm_repair, "provider_is_live", return_value=True),
        ):
            events = list(orchestrator.run_workflow_stream(session, workflow.id, doc.id, "fr"))
    assert SEEN, (mode, judge, parallel, events)
    if tags:
        assert "tag_repair" in TAG_TASKS and "tag_verify" in TAG_TASKS, (TAG_TASKS, events)
    if brief:
        assert len(SEEN) >= 2, SEEN
    for row in SEEN:
        if not enabled:
            assert "source@example.test" in str(row), row
            continue
        assert "PII_EMAIL_" in str(row), row
        assert all(value not in str(row) for value in [*VALUES, "source@example.test"]), row


def scope_case():
    provider = Recording()

    def nested():
        return pii_boundary.call_with_redaction(
            False, provider.complete, SOURCE, payload={"text": SOURCE}
        )

    assert pii_boundary.call_with_redaction(True, nested) == SOURCE
    assert "source@example.test" not in str(SEEN[-1])

    def fail():
        assert pii_boundary._redaction_on()
        raise ValueError("test scope cleanup")

    with suppress(ValueError):
        pii_boundary.call_with_redaction(True, fail)
    assert not pii_boundary._redaction_on()
    # Reused worker threads must reset their scope, and separate workers must not
    # share the setting. Barrier keeps enabled/disabled calls active together.
    from threading import Barrier

    barrier = Barrier(2)

    def check(expected):
        barrier.wait(timeout=10)
        return pii_boundary._redaction_on() == expected

    with ThreadPoolExecutor(max_workers=2) as pool:
        on = pool.submit(pii_boundary.call_with_redaction, True, check, True)
        off = pool.submit(pii_boundary.call_with_redaction, False, check, False)
        assert on.result() and off.result()
        assert not pool.submit(pii_boundary._redaction_on).result()


def main():
    init_db()
    register_provider("stepprivacy", Recording())
    settings.set_many({"pii_redaction": "0", "pii_ner_enabled": "0", "tag_llm_repair_enabled": "1"})
    segment_case(False)
    workflow_case(TransMode.SEGMENTED, tags=True, enabled=False)
    workflow_case(TransMode.FULL_TEXT, tags=True, enabled=False)
    actions = (
        lambda: segment_case(True),
        lambda: segment_case(True, retry=True),
        lambda: segment_case(True, retry="transport"),
        lambda: workflow_case(TransMode.FULL_TEXT),
        lambda: workflow_case(TransMode.FULL_TEXT, judge=True),
        lambda: workflow_case(TransMode.SEGMENTED, judge=True),
        lambda: workflow_case(TransMode.SEGMENTED, judge=True, parallel=True),
        lambda: workflow_case(TransMode.SEGMENTED, brief=True),
        lambda: workflow_case(TransMode.SEGMENTED, tags=True),
        lambda: workflow_case(TransMode.FULL_TEXT, tags=True),
    )
    for action in actions:
        action()
        # Remove only the new scope: every case must detect the original leak.
        with patch.object(
            pii_boundary, "call_with_redaction", lambda force, fn, *a, **kw: fn(*a, **kw)
        ):
            try:
                action()
            except AssertionError:
                assert SEEN and "@example.test" in str(SEEN)
            else:
                raise AssertionError("case did not detect the unfixed step boundary")
    scope_case()
    segment_case(False)
    settings.set_many({"pii_redaction": "1"})
    scope_case_global = pii_boundary.call_with_redaction(
        False, Recording().complete, SOURCE, payload={"text": SOURCE}
    )
    assert scope_case_global == SOURCE and "source@example.test" not in str(SEEN[-1])
    print("STEP PII OK")


if __name__ == "__main__":
    main()
