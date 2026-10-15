"""Workflow-engine robustness verification (Branch B, offline).

Covers the live-QA bugs T1-01, T1-02, T1-05, and the workflow/group slice of T2-01:

  (a) {text}-less step prompt is REJECTED on save (T1-01) at the service seam, the
      web add-step route, AND the CLI add-step command — with a clear message; a
      prompt WITH {text} or {prior} is accepted, and an EMPTY prompt is accepted
      (it falls back to a DEFAULT_PROMPT that already carries {text}).
  (b) the no-placeholder detector unit (prompts.has_source_placeholder).
  (c) resuming a paused human_review run with edited_text="" KEEPS the produced MT
      (run completes, targets preserved) rather than failing + discarding it — while
      a real (non-empty) edited blob still re-splits and applies (T1-02).
  (d) creating a workflow OR a workflow group scoped to an ARCHIVED customer is
      refused with a clean error on BOTH the web route and the CLI (T2-01).
  (e) an empty (0-segment / whitespace-only) document run reports a clear
      "no translatable segments" reason instead of a bare 'failed' (T1-05), and the
      web submit rejects it up front.

NOTE (why the mock can't reproduce the T1-01 WITHHOLDING): the MockProvider
translates from payload["text"] and IGNORES the rendered prompt, so a mock run of a
{text}-less workflow still "translates" and reports completed — the suite is
structurally blind to the withholding. So (a) exercises the VALIDATION path
(reject-on-save) directly and (b) unit-tests the detector; neither relies on a mock
run to prove the footgun.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (force MockProvider).
ASCII-only prints.
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfrobust_")

from datetime import datetime

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    RunStatus,
    Segment,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import ingest, orchestrator, prompts


# --- helpers -----------------------------------------------------------------
def run_cli(argv):
    """Run the CLI in-process, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def run_cli_json(argv):
    code, out, err = run_cli(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_project(client, name, targets="fr-FR"):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": targets,
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def upload(client, pid, paragraphs, mode="segmented"):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": mode},
        files={
            "file": (
                "d.docx",
                docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def add_step(
    session,
    wf_id,
    order,
    name,
    step_type=StepType.LLM_AGENT,
    provider="claude",
    prompt_text="",
    chunk_policy="auto",
):
    session.add(
        WorkflowStep(
            workflow_id=wf_id,
            order_index=order,
            name=name,
            step_type=step_type,
            provider=provider,
            prompt_text=prompt_text,
            chunk_policy=chunk_policy,
            retry_max=1,
            backoff_base=0.0,
        )
    )
    session.commit()


def segments_for(doc_id, locale="fr-FR"):
    with Session(engine) as s:
        return sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all(),
            key=lambda x: x.order_index,
        )


def make_archived_customer(name="Archived Co"):
    with Session(engine) as s:
        c = Customer(name=name, archived_at=datetime.utcnow())
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


# ============================================================================
# (b) the no-placeholder detector unit
# ============================================================================
def test_placeholder_detector_unit():
    assert prompts.has_source_placeholder("Translate: {text}") is True
    assert prompts.has_source_placeholder("Improve {prior}") is True
    assert prompts.has_source_placeholder("has both {text} and {prior}") is True
    # NO source placeholder — the footgun shapes
    assert (
        prompts.has_source_placeholder(
            "Translate the source text into Spanish. Output only the translation."
        )
        is False
    )
    assert prompts.has_source_placeholder("Only {source} and {glossary}") is False
    assert prompts.has_source_placeholder("") is False
    assert prompts.has_source_placeholder(None) is False
    print("  (b) has_source_placeholder detects {text}/{prior} presence/absence")


# ============================================================================
# (a) validation on save — service seam, web route, CLI command
# ============================================================================
def test_validate_step_prompt_service():
    bad = "Translate the source text into Spanish. Output only the translation."
    # llm_agent + non-empty + no {text}/{prior} -> rejected
    raised = False
    try:
        prompts.validate_step_prompt(StepType.LLM_AGENT, bad)
    except prompts.PromptValidationError as exc:
        raised = True
        assert "{text}" in str(exc), str(exc)
    assert raised, "service must reject a {text}-less non-empty llm_agent prompt"
    # accepted shapes: with {text}, with {prior}, and EMPTY (falls back to default)
    prompts.validate_step_prompt(StepType.LLM_AGENT, "Translate: {text}")
    prompts.validate_step_prompt(StepType.LLM_AGENT, "Refine the {prior} output")
    prompts.validate_step_prompt(StepType.LLM_AGENT, "")
    prompts.validate_step_prompt(StepType.LLM_AGENT, "   ")
    # non-llm_agent steps ignore prompt_text entirely (lqa/human_review never render it)
    prompts.validate_step_prompt(StepType.LQA, bad)
    prompts.validate_step_prompt(StepType.HUMAN_REVIEW, bad)
    print(
        "  (a) service validate_step_prompt: rejects {text}-less llm_agent, allows "
        "{text}/{prior}/empty and non-llm_agent"
    )


def test_add_step_rejects_missing_text_web(client):
    with Session(engine) as s:
        wf = Workflow(name="Web validate wf", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
    bad = "Translate the source text into Spanish. Output only the translation."
    r = client.post(
        f"/workflows/{wf_id}/steps",
        data={
            "step_type": StepType.LLM_AGENT,
            "name": "T",
            "provider": "claude",
            "prompt_text": bad,
        },
        follow_redirects=False,
    )
    assert r.status_code == 400, ("web add-step must 400 on a {text}-less prompt", r.status_code)
    assert "{text}" in r.text, "web error must name the {text} requirement"
    with Session(engine) as s:
        n = len(orchestrator.get_steps(s, wf_id))
    assert n == 0, ("no step should have been created on rejection", n)
    # a valid prompt is accepted (303 redirect back to the builder, step created)
    r = client.post(
        f"/workflows/{wf_id}/steps",
        data={
            "step_type": StepType.LLM_AGENT,
            "name": "T",
            "provider": "claude",
            "prompt_text": "Translate: {text}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    # an EMPTY prompt is accepted too (uses DEFAULT_PROMPTS)
    r = client.post(
        f"/workflows/{wf_id}/steps",
        data={
            "step_type": StepType.LLM_AGENT,
            "name": "E",
            "provider": "claude",
            "prompt_text": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        n = len(orchestrator.get_steps(s, wf_id))
    assert n == 2, ("valid + empty prompts should both create a step", n)
    print("  (a) web add-step: 400 on {text}-less prompt (no step), 303 on valid/empty prompt")


def test_add_step_rejects_missing_text_cli():
    code, payload, _ = run_cli_json(["workflow", "create", "--name", "CLI validate wf"])
    assert code == 0 and payload["ok"], payload
    wf_id = payload["id"]
    bad = "Translate the source text into Spanish. Output only the translation."
    code, out, err = run_cli(
        ["workflow", "add-step", str(wf_id), "--type", "llm_agent", "--prompt", bad]
    )
    assert code != 0, ("CLI add-step must fail on a {text}-less prompt", code, out, err)
    assert "{text}" in (out + err), ("CLI error must name the {text} requirement", out, err)
    # valid prompt accepted
    code, payload, _ = run_cli_json(
        ["workflow", "add-step", str(wf_id), "--type", "llm_agent", "--prompt", "Translate: {text}"]
    )
    assert code == 0 and payload["ok"], payload
    # empty prompt accepted
    code, payload, _ = run_cli_json(
        ["workflow", "add-step", str(wf_id), "--type", "llm_agent", "--prompt", ""]
    )
    assert code == 0 and payload["ok"], payload
    code, payload, _ = run_cli_json(["workflow", "show", str(wf_id)])
    assert len(payload["steps"]) == 2, payload
    print("  (a) CLI add-step: non-zero exit on {text}-less prompt, accepts valid/empty prompt")


# ============================================================================
# (c) human_review empty-resume keeps the MT
# ============================================================================
def _human_review_workflow(name):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Human", step_type=StepType.HUMAN_REVIEW)
    return wf_id


def test_human_review_empty_resume_keeps_mt(client):
    pid = new_project(client, "Empty resume proj")
    # --- empty resume: MT preserved, run NOT failed ---
    wf_id = _human_review_workflow("HR empty resume")
    doc = upload(client, pid, ["First para here.", "Second para here."])
    with Session(engine) as s:
        paused = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert paused["status"] == "paused", paused
    run_id = paused["run_id"]
    # the produced MT that sits in the paused blob (what we must NOT discard)
    mt_parts = paused["paused_text"].split(orchestrator.SEG_JOINER)
    assert len(mt_parts) == 2 and all("[fr-FR]" in p for p in mt_parts), mt_parts
    # resume with an EMPTY review box
    with Session(engine) as s:
        resumed = orchestrator.resume_workflow(s, run_id, "fr-FR", "")
    assert resumed["status"] == "completed", ("empty resume must NOT fail the run", resumed)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.COMPLETED, run.status
    segs = segments_for(doc)
    assert len(segs) == 2, len(segs)
    for i, sg in enumerate(segs):
        assert (sg.target_text or "").strip(), ("MT discarded on empty resume", i)
        assert "[fr-FR]" in sg.target_text, (
            "kept MT should be the produced target",
            sg.target_text,
        )
    print("  (c) human_review empty resume: run COMPLETED, produced MT preserved")

    # --- real edit still applies (regression: the non-empty path is unchanged) ---
    wf_id2 = _human_review_workflow("HR real edit")
    doc2 = upload(client, pid, ["Alpha line.", "Beta line."])
    with Session(engine) as s:
        paused2 = orchestrator.run_workflow(s, wf_id2, doc2, "fr-FR")
    run_id2 = paused2["run_id"]
    edited = orchestrator.SEG_JOINER.join(["EDITED ALPHA", "EDITED BETA"])
    with Session(engine) as s:
        resumed2 = orchestrator.resume_workflow(s, run_id2, "fr-FR", edited)
    assert resumed2["status"] == "completed", resumed2
    segs2 = segments_for(doc2)
    assert "EDITED ALPHA" in segs2[0].target_text and "EDITED BETA" in segs2[1].target_text, (
        "a real edited blob must still re-split + apply",
        segs2[0].target_text,
        segs2[1].target_text,
    )
    print("  (c) human_review real edit: non-empty blob still re-splits and applies")


# ============================================================================
# (d) archived customer refused on workflow + group create (web + CLI)
# ============================================================================
def test_archived_customer_refused_web(client):
    cid = make_archived_customer("Web Archived")
    # workflow create scoped to the archived customer
    r = client.post(
        "/workflows",
        data={"name": "For archived", "scope": "customer", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 400, (
        "archived customer workflow create must be refused",
        r.status_code,
    )
    assert "archived" in r.text.lower(), r.text[:200]
    with Session(engine) as s:
        made = s.exec(select(Workflow).where(Workflow.name == "For archived")).all()
    assert not made, "no workflow should be created for an archived customer"
    # workflow-group create scoped to the archived customer
    r = client.post(
        "/workflow-groups",
        data={"name": "Grp archived", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 400, ("archived customer group create must be refused", r.status_code)
    assert "archived" in r.text.lower(), r.text[:200]
    print("  (d) web: workflow + workflow-group create refused for an archived customer")


def test_archived_customer_refused_cli():
    cid = make_archived_customer("CLI Archived")
    code, out, err = run_cli(
        [
            "workflow",
            "create",
            "--name",
            "CLI arch wf",
            "--scope",
            "customer",
            "--customer-id",
            str(cid),
        ]
    )
    assert code != 0, ("CLI workflow create for archived customer must fail", code, out)
    assert "archived" in (out + err).lower(), (out, err)
    code, out, err = run_cli(
        ["workflow", "group", "create", "--name", "CLI arch grp", "--customer-id", str(cid)]
    )
    assert code != 0, ("CLI group create for archived customer must fail", code, out)
    assert "archived" in (out + err).lower(), (out, err)
    print("  (d) CLI: workflow + group create refused for an archived customer")


# ============================================================================
# (e) empty-document run gives a clear reason
# ============================================================================
def test_empty_document_message(client):
    pid = new_project(client, "Empty doc proj")
    # #573 (b6897edc5): a whitespace-only upload is now refused at the ingest seam with
    # "The file has no text to translate." and no Document row, so the web upload can no
    # longer create one: pin that refusal exactly.
    with Session(engine) as s:
        before = len(s.exec(select(Document.id).where(Document.project_id == pid)).all())
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(["   ", ""]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert r.headers["location"] == (
        f"/projects/{pid}?err=The%20file%20has%20no%20text%20to%20translate."
    ), r.headers["location"]
    with Session(engine) as s:
        after = len(s.exec(select(Document.id).where(Document.project_id == pid)).all())
    assert after == before, ("a refused empty upload created a document", before, after)
    # A 0-segment document is still reachable through the seam's allow_empty path (an
    # image with no detected text, #573 gate) and on installs that predate #573, so the
    # run must still fail with the clear reason. Build one through that same seam.
    with Session(engine) as s:
        doc = ingest.ingest_document(
            s,
            s.get(Project, pid),
            "d.docx",
            docx_from(["   ", ""]),
            "segmented",
            allow_empty=True,
        ).id
    with Session(engine) as s:
        wf = Workflow(name="Empty doc wf", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "failed", ("empty doc run should be failed", summary)
    assert summary.get("error") == orchestrator.EMPTY_DOCUMENT_MSG, (
        "empty doc run must carry the clear reason",
        summary.get("error"),
    )
    with Session(engine) as s:
        # every run for this doc must be FAILED (no bare success)
        runs = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc)).all()
        assert runs and all(r.status == RunStatus.FAILED for r in runs), [r.status for r in runs]
    print(
        "  (e) empty document: run FAILED with the clear "
        f"'{orchestrator.EMPTY_DOCUMENT_MSG}' reason"
    )

    # web submit rejects an empty document up front with a clear message
    r = client.post(
        f"/documents/{doc}/run",
        data={"workflow_id": str(wf_id), "locale": "fr-FR"},
        follow_redirects=False,
    )
    assert r.status_code == 400, ("web run submit must reject an empty document", r.status_code)
    assert "no translatable segments" in r.text.lower(), r.text[:200]
    print("  (e) web submit: empty document rejected up front (400, clear message)")


def main() -> None:
    init_db()
    client = TestClient(app)

    test_placeholder_detector_unit()
    test_validate_step_prompt_service()
    test_add_step_rejects_missing_text_web(client)
    test_add_step_rejects_missing_text_cli()
    test_human_review_empty_resume_keeps_mt(client)
    test_archived_customer_refused_web(client)
    test_archived_customer_refused_cli()
    test_empty_document_message(client)

    print("verify_workflow_engine_robustness OK")


if __name__ == "__main__":
    main()
