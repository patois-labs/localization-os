"""Prompt Tap RUN ENVELOPES -- Wave A suite (PROPOSAL-prompt-tap-run-envelopes).

Proves the Wave A "schema + capture spine" contracts:

  A  A real segmented WORKFLOW run creates ONE TapAction (kind=workflow) + ONE TapRun,
     and EVERY captured call for that run carries meta.action_id == the action id and
     meta.run_id == the run id (stamping correctness). The calls land in the per-action
     file prompt_tap/actions/<action_id>.jsonl, NOT the legacy global file.
  B  A FULL_TEXT workflow run gets the same envelope + stamping (the other orchestrator
     impl branch).
  C  A DIRECT-MT pass (translate.pretranslate_stream) creates a kind=direct auto-action
     + run; every per-segment call is stamped; ad-hoc translate_text_once too.
  D  A BATCH leg (a member WorkflowRun carrying batch_id) creates a TapAction carrying
     that batch_id -- the anchor the Wave C viewer groups a project-wide translate by.
  E  RESUME REUSE: begin_workflow_envelope is idempotent on workflow_run_id -- a second
     call returns the SAME (action, run) and creates no duplicate TapRun.
  F  LEGACY COEXISTENCE: a pre-existing global-file record AND new per-action records
     are all returned by the legacy read path (read_records), and clear() empties ONLY
     the global file (proposal section 8), leaving run capture intact.
  G  EVAL SEPARATION: a capture whose meta carries eval_* and NO action_id routes to the
     global file and belongs to no action; a capture with an action_id routes to its
     per-action file. (eval_runner never calls tap_envelope, so a Lab call is unstamped
     by construction -- this proves the capture-routing mechanism that keeps them apart.)
  H  OBSERVER PURITY: an induced envelope-WRITE failure never fails (or perturbs) a
     translation, but it WARNs (once) rather than debug-swallowing (the 2026-08-06
     outage lesson). Finalization is transactional status-only (append-only).
  I  ENVELOPES OFF: with prompt_tap_envelopes_enabled=0 a run creates NO envelope rows
     and still succeeds; captures then carry no action_id (global file).
  J  MIGRATION fresh + upgrade: `alembic upgrade head` on a fresh DB builds tapaction +
     taprun with the section-6 indexes, and create_all() builds them too.

Fixtures drive the REAL orchestrator / translate paths (the fixture-realism rule).
Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only prints.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_tapenv_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TapAction,
    TapActionKind,
    TapRun,
    TapStatus,
    TransMode,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, prompt_tap, settings, tap_envelope, translate

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _reset(tap_on=True, envelopes_on=True):
    """Clean per-test slate: clear the legacy file, remove per-action files, wipe the
    envelope tables, and set the two toggles."""
    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    with Session(engine) as s:
        for row in s.exec(select(TapRun)).all():
            s.delete(row)
        for row in s.exec(select(TapAction)).all():
            s.delete(row)
        s.commit()
    settings.set_many(
        {
            "prompt_tap_enabled": "1" if tap_on else "0",
            "prompt_tap_envelopes_enabled": "1" if envelopes_on else "0",
        }
    )
    # reset the once-per-process WARN latches so a purity test can see its warning
    tap_envelope._WARNED = False
    prompt_tap._CAPTURE_WARNED = False


def _mk_project(session):
    p = Project(name="tapenv-%d" % _seq(), source_locale="en-US", default_provider="mock")  # noqa: UP031
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_segmented_doc(session, project, sources=("One.", "Two.")):
    doc = Document(project_id=project.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
    session.add(doc)
    session.commit()
    session.refresh(doc)
    for i, s in enumerate(sources):
        session.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=i,
                source_text=s,
                status=SegStatus.NEW,
            )
        )
    session.commit()
    return doc


def _mk_fulltext_doc(session, project, text="Hello full text."):
    doc = Document(
        project_id=project.id,
        filename="ft.txt",
        fmt="txt",
        mode=TransMode.FULL_TEXT,
        skeleton={"units": [{"text": text}]},
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


def _mk_translate_workflow(session):
    wf = Workflow(name="wf-%d" % _seq(), scope="global")  # noqa: UP031
    session.add(wf)
    session.commit()
    session.refresh(wf)
    session.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            provider="mock",
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate: {text}",
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
            key="translate",
        )
    )
    session.commit()
    return wf


def _action_records(action_id):
    """Every record in this action's per-action file (parsed)."""
    return list(prompt_tap._iter_file(prompt_tap._action_path(action_id)))


def _global_records():
    return list(prompt_tap._iter_file(prompt_tap._path()))


# --- A: segmented workflow envelope + stamping -------------------------------
def test_segmented_workflow_envelope_and_stamping():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("One.", "Two."))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id

    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    run_id = summary["run_id"]

    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
        runs = s.exec(select(TapRun)).all()
        assert len(actions) == 1, ("exactly one action", [a.id for a in actions])
        assert len(runs) == 1, ("exactly one run", [r.id for r in runs])
        a, r = actions[0], runs[0]
        assert a.kind == TapActionKind.WORKFLOW, a.kind
        assert a.workflow_id == wf_id and a.document_id == doc_id, (a.workflow_id, a.document_id)
        assert a.project_id is not None, "the action should carry the project id"
        assert a.source_locale == "en-US" and a.requested_locales == ["es-ES"], a
        assert a.schema_version == 1 and prompt_tap.is_action_id(a.id), a
        assert a.status == TapStatus.COMPLETED and a.finished_at is not None, a
        assert r.action_id == a.id and r.workflow_run_id == run_id, (r.action_id, r.workflow_run_id)
        assert r.target_locale == "es-ES" and r.status == TapStatus.COMPLETED, r
        aid, rid = a.id, r.id

    # Every captured call for this run is stamped, and lives in the per-action file.
    recs = _action_records(aid)
    assert recs, "the run must have captured calls into its per-action file"
    for rec in recs:
        meta = rec.get("meta") or {}
        assert meta.get("action_id") == aid, meta
        assert meta.get("run_id") == rid, meta
        assert meta.get("workflow_run_id") == run_id, meta
    # ...and NOT in the legacy global file.
    assert _global_records() == [], "run capture must not land in the legacy global file"
    print("  (A) segmented workflow: 1 action + 1 run, every call stamped, per-action file OK")


# --- B: FULL_TEXT workflow envelope ------------------------------------------
def test_fulltext_workflow_envelope():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_fulltext_doc(s, proj, text="Alpha beta.")
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id

    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary

    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
        runs = s.exec(select(TapRun)).all()
        assert len(actions) == 1 and len(runs) == 1, (len(actions), len(runs))
        a = actions[0]
        assert a.kind == TapActionKind.WORKFLOW and a.document_id == doc_id, a
        recs = _action_records(a.id)
        assert recs, "the FULL_TEXT run must have captured at least one call"
        assert all((rec.get("meta") or {}).get("action_id") == a.id for rec in recs)
    print("  (B) FULL_TEXT workflow: envelope created + calls stamped OK")


# --- C: direct MT + ad-hoc translate_text ------------------------------------
def test_direct_mt_and_adhoc_envelope():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Solo.", "Duo."))
        doc_id = doc.id
    with Session(engine) as s:
        for _ in translate.pretranslate_stream(s, doc_id, "es-ES"):
            pass

    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
        runs = s.exec(select(TapRun)).all()
        direct = [a for a in actions if a.kind == TapActionKind.DIRECT]
        assert len(direct) == 1, ("one direct action", [(a.kind, a.id) for a in actions])
        a = direct[0]
        assert a.document_id == doc_id and a.requested_locales == ["es-ES"], a
        assert a.workflow_id is None and a.status == TapStatus.COMPLETED, a
        assert len(runs) == 1 and runs[0].workflow_run_id is None, runs
        recs = _action_records(a.id)
        assert recs, "direct MT must capture per-segment calls"
        assert all((rec.get("meta") or {}).get("action_id") == a.id for rec in recs)

    # ad-hoc translate_text_once: its own direct action, no project/document.
    _reset()
    res = translate.translate_text_once("Bonjour", "fr-FR", "es-ES")
    assert res["text"], res
    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
        assert len(actions) == 1 and actions[0].kind == TapActionKind.DIRECT, actions
        a = actions[0]
        assert a.document_id is None and a.project_id is None, a
        assert a.source_locale == "fr-FR" and a.requested_locales == ["es-ES"], a
        assert a.status == TapStatus.COMPLETED, a
        recs = _action_records(a.id)
        assert recs and all((rec.get("meta") or {}).get("action_id") == a.id for rec in recs)
    print("  (C) direct MT + ad-hoc translate_text: kind=direct auto-action, calls stamped OK")


# --- D: batch leg carries batch_id -------------------------------------------
def test_batch_leg_envelope_carries_batch_id():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Batch one.",))
        wf = _mk_translate_workflow(s)
        batch = WorkflowBatch(
            workflow_id=wf.id, document_id=doc.id, requested_locales=["es-ES"], created_by="cli"
        )
        s.add(batch)
        s.commit()
        s.refresh(batch)
        # A member run exactly as batch_runs._create_member builds it.
        member = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc.id,
            target_locale="es-ES",
            created_by="cli",
            status=RunStatus.RUNNING,
            current_step_index=0,
            batch_id=batch.id,
        )
        s.add(member)
        s.commit()
        s.refresh(member)
        wf_id, doc_id, member_id, batch_id = wf.id, doc.id, member.id, batch.id

    # Drive the member leg the way _run_member does (run_id set).
    with Session(engine) as s:
        for _ in orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES", run_id=member_id):
            pass

    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
        assert len(actions) == 1, actions
        a = actions[0]
        assert a.batch_id == batch_id, ("the leg's action must carry batch_id", a.batch_id)
        assert a.kind == TapActionKind.WORKFLOW, a.kind
        r = s.exec(select(TapRun)).first()
        assert r.workflow_run_id == member_id, r
    print("  (D) batch leg: TapAction carries batch_id (Wave C grouping anchor) OK")


# --- E: resume reuse is idempotent, and GATED on is_resume -------------------
def test_resume_reuse_idempotent():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("R.",))
        wf = _mk_translate_workflow(s)
        wr = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc.id,
            target_locale="de-DE",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        s.add(wr)
        s.commit()
        s.refresh(wr)
        wr_id, doc_id = wr.id, doc.id

    # Fresh launch: is_resume=False -> mint an envelope.
    aid1, rid1 = tap_envelope.begin_workflow_envelope(
        workflow_run_id=wr_id,
        is_resume=False,
        document_id=doc_id,
        source_locale="en-US",
        target_locale="de-DE",
    )
    assert aid1 and rid1, (aid1, rid1)
    # A genuine RESUME (is_resume=True) with matching identity must REUSE, not fork.
    aid2, rid2 = tap_envelope.begin_workflow_envelope(
        workflow_run_id=wr_id,
        is_resume=True,
        document_id=doc_id,
        source_locale="en-US",
        target_locale="de-DE",
    )
    assert (aid2, rid2) == (aid1, rid1), ((aid1, rid1), (aid2, rid2))
    with Session(engine) as s:
        runs = s.exec(select(TapRun).where(TapRun.workflow_run_id == wr_id)).all()
        assert len(runs) == 1, ("no duplicate TapRun on resume", len(runs))
    print("  (E) resume reuse: begin_workflow_envelope idempotent on (resume, doc, locale) OK")


# --- R1: a FRESH batch leg with a reused rowid must NOT adopt a stale envelope ---
def test_r1_batch_leg_rowid_reuse_no_adopt():
    """FAIL-PRE-FIX: a batch leg drives run_workflow_stream with a pre-created run_id, so
    `is_resume = run_id is not None` misclassified it as a resume; with rowid reuse on the
    SAME doc+locale the doc/locale validation agreed and the leg ADOPTED (reopened) a
    deleted run's completed envelope, losing batch_id. Fixed by is_resume=False (batch)
    + the created_at floor."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Original.",))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id

    with Session(engine) as s:
        sm = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    run1 = sm["run_id"]
    with Session(engine) as s:
        old_action = s.exec(select(TapAction)).all()[0].id

    from app.models import StepRun

    with Session(engine) as s:  # jobs cleanup deletes the run -> rowid freed for reuse
        for sr in s.exec(select(StepRun).where(StepRun.workflow_run_id == run1)).all():
            s.delete(sr)
        s.delete(s.get(WorkflowRun, run1))
        s.commit()

    # A batch member run pre-created (exactly like batch_runs._create_member), same
    # doc+locale, whose rowid SQLite reuses.
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf_id, document_id=doc_id, requested_locales=["es-ES"], created_by="cli"
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        m = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale="es-ES",
            created_by="cli",
            status=RunStatus.RUNNING,
            current_step_index=0,
            batch_id=b.id,
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        member_id, batch_id = m.id, b.id
    assert member_id == run1, ("this test needs the rowid reused", member_id, run1)

    # Drive exactly as batch_runs._run_member does: a FRESH leg asserts is_resume=False,
    # so the reused rowid never triggers an envelope lookup. Pre-fix (is_resume derived
    # from run_id) this adopted the stale completed envelope.
    with Session(engine) as s:
        for _ in orchestrator.run_workflow_stream(
            s, wf_id, doc_id, "es-ES", run_id=member_id, is_resume=False
        ):
            pass

    with Session(engine) as s:
        acts = s.exec(select(TapAction)).all()
        assert len(acts) == 2, (
            "the leg must MINT a fresh envelope, not adopt the deleted run's",
            [(a.id, a.batch_id, a.status) for a in acts],
        )
        leg = [a for a in acts if a.id != old_action]
        assert len(leg) == 1 and leg[0].batch_id == batch_id, (
            "the fresh leg envelope must carry batch_id",
            [(a.id, a.batch_id) for a in acts],
        )
        # The old completed envelope was never reopened (append-only preserved).
        old = s.get(TapAction, old_action)
        assert old.status == "completed" and old.batch_id is None, (old.status, old.batch_id)
    print("  (R1) batch leg + rowid reuse: fresh envelope with batch_id, old not reopened OK")


# --- R1b: a multi-pause run keeps ONE envelope across repeated resumes --------
def _mk_two_pause_workflow(session):
    """t1 -> review -> t2 -> review -> t3 (two human_review pauses)."""
    wf = Workflow(name="wf2p-%d" % _seq(), scope="global")  # noqa: UP031
    session.add(wf)
    session.commit()
    session.refresh(wf)
    plan = [
        (StepType.LLM_AGENT, "t1"),
        (StepType.HUMAN_REVIEW, "r1"),
        (StepType.LLM_AGENT, "t2"),
        (StepType.HUMAN_REVIEW, "r2"),
        (StepType.LLM_AGENT, "t3"),
    ]
    for i, (kind, key) in enumerate(plan):
        session.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=i,
                name=key,
                provider="mock",
                step_type=kind,
                prompt_text="Translate: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                key=key,
            )
        )
    session.commit()
    return wf.id


def _poll_status(rid, target, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            r = s.get(WorkflowRun, rid)
            if r is not None and r.status == target:
                return target
        time.sleep(0.05)
    with Session(engine) as s:
        r = s.get(WorkflowRun, rid)
        return r.status if r is not None else None


def _env_counts():
    with Session(engine) as s:
        return len(s.exec(select(TapAction)).all()), len(s.exec(select(TapRun)).all())


def test_r1_multi_pause_resume_single_envelope():
    """FAIL-PRE-FIX (the run_created_at floor): started_at is re-stamped on every
    execution entry, so from the SECOND resume the floor exceeds the envelope's own
    created_at and the run rejected its OWN envelope -> a duplicate TapAction/TapRun and
    an orphan 'paused' TapRun that never finalizes. With the floor dropped, is_resume +
    newest-wins keep exactly ONE envelope across both resumes, ending terminal."""
    from app.models import RunStatus

    # ----- SYNC path: run_workflow_stream launch + resume_workflow twice -----
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Multi pause.",))
        wf_id, doc_id = _mk_two_pause_workflow(s), doc.id
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES"))
    with Session(engine) as s:
        rid = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc_id)).all()[-1].id
    assert _env_counts() == (1, 1), ("launch: one envelope", _env_counts())
    with Session(engine) as s:
        orchestrator.resume_workflow(s, rid, "es-ES", "edit one")  # past r1 -> pause r2
    assert _env_counts() == (1, 1), ("after resume #1: still one envelope", _env_counts())
    with Session(engine) as s:
        orchestrator.resume_workflow(s, rid, "es-ES", "edit two")  # past r2 -> t3 -> done
    a, r = _env_counts()
    assert (a, r) == (1, 1), ("after resume #2: still exactly one envelope", (a, r))
    with Session(engine) as s:
        act = s.exec(select(TapAction)).all()[0]
        run = s.exec(select(TapRun)).all()[0]
        assert act.status == "completed" and run.status == "completed", (
            "sync: envelope must finalize terminal, no orphan",
            act.status,
            run.status,
        )

    # ----- ASYNC path: start_run + resume_run_async twice (background threads) -----
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Async pause.",))
        wf_id, doc_id = _mk_two_pause_workflow(s), doc.id
    with Session(engine) as s:
        det = {}
        orchestrator.start_run(s, wf_id, doc_id, "es-ES", detail=det)
        rid = det["run_id"]
    assert _poll_status(rid, RunStatus.PAUSED) == RunStatus.PAUSED, (
        "async launch should pause at r1"
    )
    assert _env_counts() == (1, 1), ("async launch: one envelope", _env_counts())
    with Session(engine) as s:
        orchestrator.resume_run_async(s, rid, "es-ES", "edit one")
    assert _poll_status(rid, RunStatus.PAUSED) == RunStatus.PAUSED, (
        "async resume #1 should pause at r2"
    )
    assert _env_counts() == (1, 1), ("async after resume #1: one envelope", _env_counts())
    with Session(engine) as s:
        orchestrator.resume_run_async(s, rid, "es-ES", "edit two")
    assert _poll_status(rid, RunStatus.COMPLETED) == RunStatus.COMPLETED, (
        "async resume #2 should complete"
    )
    a, r = _env_counts()
    assert (a, r) == (1, 1), ("async after resume #2: still exactly one envelope", (a, r))
    with Session(engine) as s:
        act = s.exec(select(TapAction)).all()[0]
        run = s.exec(select(TapRun)).all()[0]
        assert act.status == "completed" and run.status == "completed", (
            "async: envelope must finalize terminal, no orphan",
            act.status,
            run.status,
        )
    print("  (R1b) two-pause run: ONE envelope across both resumes (sync + async), terminal OK")


# --- RACE: the envelope is finalized ATOMICALLY with the run's terminal commit ------
def _run_race_case(mk_doc):
    """Drive `run_workflow` while capturing the envelope status at the instant the wrapper's
    `finally` finalizer is entered. The fix stages the envelope's terminal transition onto the
    run's OWN session inside _commit_run_terminal, so it commits in the SAME transaction as
    run.status -- and is therefore ALREADY terminal by the time the `finally` runs (and by the
    time any poller can observe the run terminal). Pre-fix (no staging) it is still 'running'
    at finally-entry. Returns (summary, seen)."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = mk_doc(s, proj)
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    seen = {}
    real = orchestrator._finalize_run_envelope

    def _capture(wf_run_id, d, loc, **k):
        with Session(engine) as s:
            a = s.exec(select(TapAction)).first()
            r = s.exec(select(TapRun)).first()
            seen["action"] = a.status if a else None
            seen["run"] = r.status if r else None
        return real(wf_run_id, d, loc, **k)

    set_pieces("app.services.orchestrator", "_finalize_run_envelope", _capture)
    try:
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    finally:
        set_pieces("app.services.orchestrator", "_finalize_run_envelope", real)
    return summary, seen


def test_race_envelope_terminal_atomic_with_run():
    """FAIL-PRE-FIX (the tap-finalize race): the impl committed run.status terminal (via
    _commit_run_terminal) while the TapAction/TapRun envelope stayed 'running' until the
    wrapper's `finally` fired -- a window in which a status poller (UI / API / MCP / the async
    driver) could read a terminal run whose telemetry envelope was still 'running', and a crash
    in that window orphaned it. The fix STAGES the envelope's terminal transition onto the
    run's OWN session so it lands in the SAME transaction as run.status. Proven for BOTH
    document modes (L5): SEGMENTED and FULL_TEXT both finalize terminal in-transaction."""
    for label, mk in (
        ("segmented", lambda s, p: _mk_segmented_doc(s, p, sources=("Race.",))),
        ("fulltext", lambda s, p: _mk_fulltext_doc(s, p, text="Race text.")),
    ):
        summary, seen = _run_race_case(mk)
        assert summary["status"] == "completed", (label, summary)
        assert (
            seen.get("action") == TapStatus.COMPLETED and seen.get("run") == TapStatus.COMPLETED
        ), (
            "envelope must be terminal in the same transaction as run.status (already terminal "
            "by finally-entry)",
            label,
            seen,
        )
        with Session(engine) as s:
            a = s.exec(select(TapAction)).first()
            r = s.exec(select(TapRun)).first()
            assert a.status == TapStatus.COMPLETED and r.status == TapStatus.COMPLETED, (
                label,
                a.status,
                r.status,
            )
    print("  (RACE) envelope finalized terminal atomically with the run (segmented + fulltext) OK")


def test_race_finalize_is_atomic_with_run():
    """H1 probe A1 -- the SHIPPED property: stage_run_finalize_in_session writes onto the run's
    OWN session, so the envelope's terminal transition commits/ROLLS BACK atomically with
    run.status. A second connection can NEVER observe a torn state (run terminal + envelope
    running, or vice versa). This would FAIL under the ABANDONED first isolated-write design
    (a separate commit would survive the run's rollback). Pins COMPLETED and FAILED."""
    for run_status, tap_terminal in (
        (RunStatus.COMPLETED, TapStatus.COMPLETED),
        (RunStatus.FAILED, TapStatus.FAILED),
    ):
        _reset()
        with Session(engine) as s:
            proj = _mk_project(s)
            doc = _mk_segmented_doc(s, proj, sources=("Atom.",))
            wf = _mk_translate_workflow(s)
            wr = WorkflowRun(
                workflow_id=wf.id,
                document_id=doc.id,
                target_locale="es-ES",
                status=RunStatus.RUNNING,
                current_step_index=0,
            )
            s.add(wr)
            s.commit()
            s.refresh(wr)
            wr_id, doc_id = wr.id, doc.id
        aid, rid = tap_envelope.begin_workflow_envelope(
            workflow_run_id=wr_id,
            is_resume=False,
            document_id=doc_id,
            source_locale="en-US",
            target_locale="es-ES",
        )
        assert aid and rid, (aid, rid)

        # (1) stage -> ROLLBACK: BOTH the run write and the envelope stage revert together.
        with Session(engine) as s:
            wr = s.get(WorkflowRun, wr_id)
            wr.status = run_status
            wr.finished_at = tap_envelope._now()
            s.add(wr)
            tap_envelope.stage_run_finalize_in_session(
                s,
                workflow_run_id=wr_id,
                document_id=doc_id,
                target_locale="es-ES",
                status=tap_terminal,
            )
            s.rollback()
        with Session(engine) as s:  # a SECOND connection sees NEITHER moved (no torn state)
            assert s.get(WorkflowRun, wr_id).status == RunStatus.RUNNING, (
                "run reverted",
                run_status,
            )
            assert s.get(TapRun, rid).status == TapStatus.RUNNING, (
                "leg reverted with the run",
                run_status,
            )
            assert s.get(TapAction, aid).status == TapStatus.RUNNING, (
                "action reverted with the run",
                run_status,
            )

        # (2) stage -> COMMIT: BOTH land together.
        with Session(engine) as s:
            wr = s.get(WorkflowRun, wr_id)
            wr.status = run_status
            wr.finished_at = tap_envelope._now()
            s.add(wr)
            tap_envelope.stage_run_finalize_in_session(
                s,
                workflow_run_id=wr_id,
                document_id=doc_id,
                target_locale="es-ES",
                status=tap_terminal,
            )
            s.commit()
        with Session(engine) as s:
            assert s.get(WorkflowRun, wr_id).status == run_status, run_status
            assert s.get(TapRun, rid).status == tap_terminal, (
                "leg landed with the run",
                run_status,
            )
            assert s.get(TapAction, aid).status == tap_terminal, (
                "action landed with the run",
                run_status,
            )
    print(
        "  (ATOMIC) envelope stage commits/rolls back atomically with the run -- no torn state OK"
    )


def test_n1_terminal_commit_rides_out_held_writer_lock():
    """FAIL-PRE-FIX (N1 lock-upgrade abort): _commit_run_terminal staged the envelope BEFORE
    writing run.status, so the savepoint's SELECTs took SQLite's SHARED lock first and the
    run write below was a lock UPGRADE -- SQLITE_BUSY returns IMMEDIATELY on upgrades
    (busy_timeout does NOT apply), aborting a completing run in ~0.02s under any briefly-held
    writer lock. Writing run.status FIRST makes the stage's flush take the writer lock from
    NONE, so the terminal commit RIDES OUT the held lock via busy_timeout. Deterministic: a
    holder thread holds the writer lock ~0.8s; the terminal commit must WAIT it out (dt well
    above the instant-abort floor) and succeed -- not raise / abort fast."""
    import threading

    from sqlalchemy import text as _text

    _reset()
    with Session(engine) as s0:
        proj = _mk_project(s0)
        doc = _mk_segmented_doc(s0, proj, sources=("Lock.",))
        wf = _mk_translate_workflow(s0)
        wr = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc.id,
            target_locale="es-ES",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        s0.add(wr)
        s0.commit()
        s0.refresh(wr)
        wr_id, doc_id = wr.id, doc.id
    aid, rid = tap_envelope.begin_workflow_envelope(
        workflow_run_id=wr_id,
        is_resume=False,
        document_id=doc_id,
        source_locale="en-US",
        target_locale="es-ES",
    )
    assert aid and rid, (aid, rid)

    lock_held = threading.Event()
    release = threading.Event()

    def _holder():
        # A real (uncommitted) write takes + HOLDS SQLite's RESERVED writer lock. The no-op
        # UPDATE changes nothing (rolled back), it only holds the lock.
        with Session(engine) as s2:
            s2.execute(
                _text(
                    "UPDATE workflowrun SET current_step_index = current_step_index WHERE id = :i"
                ),
                {"i": wr_id},
            )
            lock_held.set()
            release.wait(5.0)
            s2.rollback()

    th = threading.Thread(target=_holder, daemon=True)
    th.start()
    assert lock_held.wait(3.0), "holder failed to take the writer lock"
    threading.Thread(target=lambda: (time.sleep(0.8), release.set()), daemon=True).start()

    # A lock-free session (load then commit releases the read lock), then the terminal commit
    # under the held writer lock. Pre-fix this raised/aborted instantly; post-fix it rides out.
    s = Session(engine)
    run = s.get(WorkflowRun, wr_id)
    s.commit()
    t0 = time.time()
    ok = orchestrator._commit_run_terminal(s, run, RunStatus.COMPLETED)
    dt = time.time() - t0
    s.close()
    th.join(3.0)
    assert ok is True, "terminal commit must succeed (ride out the lock), not abort"
    assert dt >= 0.4, (
        "the terminal commit must RIDE OUT the held writer lock via busy_timeout, "
        "not abort instantly on a SHARED->RESERVED lock upgrade",
        dt,
    )
    with Session(engine) as s:
        assert s.get(WorkflowRun, wr_id).status == RunStatus.COMPLETED
        assert s.get(TapRun, rid).status == TapStatus.COMPLETED, s.get(TapRun, rid).status
        assert s.get(TapAction, aid).status == TapStatus.COMPLETED, s.get(TapAction, aid).status
    print("  (N1) terminal commit rides out a held writer lock (no lock-upgrade abort) OK")


def test_h2_segmented_failure_finalizes_envelope():
    """H2 / L5: the SEGMENTED (default mode) FAILURE paths set run.status=FAILED directly (not
    via _commit_run_terminal, whose only segmented call is hardcoded COMPLETED), so pre-fix an
    ordinary bad run left run=failed with the envelope 'running' until the finally.
    _commit_run_terminal_direct now stages the envelope terminal in the SAME commit as the
    FAILED status, closing the same window on the failure path."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Fail.",))
        wf = _mk_translate_workflow(s)
        wr = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc.id,
            target_locale="es-ES",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        s.add(wr)
        s.commit()
        s.refresh(wr)
        wr_id, doc_id = wr.id, doc.id
    aid, rid = tap_envelope.begin_workflow_envelope(
        workflow_run_id=wr_id,
        is_resume=False,
        document_id=doc_id,
        source_locale="en-US",
        target_locale="es-ES",
    )
    assert aid and rid, (aid, rid)
    with Session(engine) as s:
        run = s.get(WorkflowRun, wr_id)
        orchestrator._commit_run_terminal_direct(s, run, RunStatus.FAILED)
    with Session(engine) as s:  # run FAILED and the envelope FAILED landed in the same commit
        assert s.get(WorkflowRun, wr_id).status == RunStatus.FAILED
        assert s.get(TapRun, rid).status == TapStatus.FAILED, s.get(TapRun, rid).status
        assert s.get(TapAction, aid).status == TapStatus.FAILED, s.get(TapAction, aid).status
    print("  (H2) segmented/step FAILURE finalizes the envelope FAILED atomically with the run OK")


# --- CANCELLED: a cancelled run's `finally` finalizes its envelope honestly, in-band ---
def test_cancelled_run_envelope_abandoned():
    """FAIL-PRE-FIX (the cancelled terminal path): cancel_run makes the run terminal but does
    NOT itself finalize the envelope -- the live run's own `finally` (_finalize_run_envelope)
    fires, reading the CANCELLED row. status_for_run_status mapped CANCELLED to a non-terminal
    RUNNING, so that finalize left the envelope stuck 'running' (to be reaped 24h later as
    ORPHANED). The mapping is now CANCELLED -> ABANDONED (a cancel is an honest DROP, the same
    mapping the reaper's _paused_leg_terminal already uses), so the finally finalizes the
    envelope terminal in-band. Deterministic: we drive the wrapper's finalizer directly on a
    CANCELLED run, no threads."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Cancel me.",))
        wf = _mk_translate_workflow(s)
        wr = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc.id,
            target_locale="es-ES",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        s.add(wr)
        s.commit()
        s.refresh(wr)
        wr_id, doc_id = wr.id, doc.id
    aid, rid = tap_envelope.begin_workflow_envelope(
        workflow_run_id=wr_id,
        is_resume=False,
        document_id=doc_id,
        source_locale="en-US",
        target_locale="es-ES",
    )
    assert aid and rid, (aid, rid)
    # cancel_run makes the run CANCELLED (terminal) without finalizing the envelope.
    with Session(engine) as s:
        orchestrator.cancel_run(s, wr_id)
    with Session(engine) as s:
        assert s.get(WorkflowRun, wr_id).status == RunStatus.CANCELLED
        assert s.get(TapRun, rid).status == TapStatus.RUNNING, (
            "cancel_run does not finalize the envelope"
        )
    # The wrapper's finally, on a CANCELLED run, honestly finalizes the envelope to ABANDONED.
    orchestrator._finalize_run_envelope(wr_id, doc_id, "es-ES")
    with Session(engine) as s:
        assert s.get(TapRun, rid).status == TapStatus.ABANDONED, (
            "a cancelled run's finally must finalize the leg ABANDONED, not leave it running",
            s.get(TapRun, rid).status,
        )
        assert s.get(TapAction, aid).status == TapStatus.ABANDONED, s.get(TapAction, aid).status
    # The mapping itself, pinned: CANCELLED -> ABANDONED (honest drop, terminal).
    assert tap_envelope.status_for_run_status(RunStatus.CANCELLED) == TapStatus.ABANDONED
    assert tap_envelope.resolve_finalize_status(RunStatus.CANCELLED) == TapStatus.ABANDONED
    print("  (CANCELLED) a cancelled run's finally finalizes its envelope ABANDONED, in-band OK")


# --- H1: SQLite rowid reuse must NOT leak an envelope across tenants ----------
def test_h1_rowid_reuse_no_cross_tenant():
    """FAIL-PRE-FIX: pre-fix begin_workflow_envelope always looked up by the bare
    workflow_run_id, so a NEW run of a DIFFERENT project reusing a deleted run's rowid
    inherited the old envelope AND appended its prompts into the old action file."""
    _reset()
    with Session(engine) as s:
        wf = _mk_translate_workflow(s)
        wf_id = wf.id
        pA = _mk_project(s)
        docA = _mk_segmented_doc(s, pA, sources=("Alpha secret.",))
        pB = _mk_project(s)
        docB = _mk_segmented_doc(s, pB, sources=("Bravo secret.",))
        docA_id, docB_id = docA.id, docB.id

    with Session(engine) as s:
        sumA = orchestrator.run_workflow(s, wf_id, docA_id, "es-ES")
    runA = sumA["run_id"]
    with Session(engine) as s:
        envA = s.exec(select(TapAction)).all()[0]
        envA_id, envA_doc = envA.id, envA.document_id
    assert envA_doc == docA_id, envA_doc

    # Jobs cleanup / retention deletes the WorkflowRun; SQLite will reuse its rowid.
    from app.models import StepRun

    with Session(engine) as s:
        for sr in s.exec(select(StepRun).where(StepRun.workflow_run_id == runA)).all():
            s.delete(sr)
        s.delete(s.get(WorkflowRun, runA))
        s.commit()

    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, docB_id, "es-ES")

    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
    # Two distinct envelopes, each pinned to its own document -- no adoption.
    assert len(actions) == 2, (
        "run B must mint a FRESH envelope, not reuse A's",
        [(a.id, a.document_id) for a in actions],
    )
    b_actions = [a for a in actions if a.document_id == docB_id]
    assert len(b_actions) == 1 and b_actions[0].id != envA_id, b_actions
    # Org A's action file must contain ONLY org A's prompts (no org B leakage).
    a_recs = _action_records(envA_id)
    a_prompts = " ".join((r.get("request") or {}).get("prompt", "") for r in a_recs)
    assert "Bravo" not in a_prompts, ("org B's prompt leaked into org A's file!", a_prompts)
    print("  (H1) rowid reuse: run B mints a fresh envelope; no cross-tenant file append OK")


# --- H3: union read orders by each record's own timestamp --------------------
def test_h3_union_read_orders_by_timestamp():
    """FAIL-PRE-FIX: pre-fix _select/page_snapshot reversed file-concatenation order
    (global file first), so a fresh unstamped eval capture sorted OLDER than every
    per-action record and fell off a limited page/export."""
    _reset()

    def _rec(prompt, meta=None):
        return prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="s",
            prompt=prompt,
            max_tokens=8,
            payload={"text": prompt},
            outcome="ok",
            response_text="x",
            tap_meta=meta,
        )

    aid = tap_envelope.new_id()
    rid = tap_envelope.new_id()
    for i in range(12):  # OLD stamped run capture (per-action file), written FIRST
        prompt_tap.capture(_rec("OLD-RUN-%02d" % i, {"action_id": aid, "run_id": rid}))  # noqa: UP031
    time.sleep(0.01)
    prompt_tap.capture(_rec("NEWEST-EVAL", {"eval_run_id": 7}))  # NEW eval, global file

    rows, _matching, _stored = prompt_tap.page_snapshot(limit=5)
    top = (rows[0].get("request") or {}).get("prompt") if rows else None
    assert top == "NEWEST-EVAL", ("newest-by-ts must be the top row", top)
    assert any((r.get("request") or {}).get("prompt") == "NEWEST-EVAL" for r in rows)
    lines = list(prompt_tap.export_bundle(fmt="jsonl", limit=5)["lines"])
    assert any("NEWEST-EVAL" in ln for ln in lines), "fresh eval capture must be in the export"
    print("  (H3) union read: newest-by-timestamp wins the page + export, not file order OK")


# --- M2: direct-MT envelope honesty (break-on-summary vs abandonment) --------
def test_m2_direct_status_honesty():
    """FAIL-PRE-FIX: pre-fix pretranslate_stream set _ok only on StopIteration, so a
    consumer that broke right after the summary recorded a completed run as FAILED."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("One.", "Two.", "Three."))
        doc_id = doc.id
    # (b) consume all events, break on summary -> COMPLETED.
    with Session(engine) as s:
        for ev in translate.pretranslate_stream(s, doc_id, "es-ES"):
            if ev.get("type") == "summary":
                break
    with Session(engine) as s:
        a = s.exec(select(TapAction)).all()[-1]
        assert a.status == "completed", ("break-on-summary must be completed", a.status)

    # (a) client disconnect mid-stream (before summary) -> NOT completed (abandoned).
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("A.", "B.", "C.", "D."))
        doc_id = doc.id
    with Session(engine) as s:
        gen = translate.pretranslate_stream(s, doc_id, "es-ES")
        next(gen)  # first segment
        gen.close()
    with Session(engine) as s:
        a = s.exec(select(TapAction)).all()[-1]
        assert a.status == "abandoned", ("mid-stream disconnect must be abandoned", a.status)
        assert a.status != "completed"
    print("  (M2) direct MT: break-on-summary=completed, disconnect=abandoned OK")


# --- M1: workflow abandonment / mid-run exception are not stuck 'running' ----
def test_m1_workflow_outcome_status():
    """FAIL-PRE-FIX: pre-fix the wrapper derived status only from the WorkflowRun row,
    which stayed RUNNING on abandonment / mid-run exception -> envelope stuck running."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("S0.", "S1.", "S2.", "S3."))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    # Abandon after the envelope exists (run_started consumed).
    with Session(engine) as s:
        gen = orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES")
        next(gen)  # run_started (envelope now exists)
        gen.close()
    with Session(engine) as s:
        acts = s.exec(select(TapAction)).all()
        assert acts and acts[-1].status == "abandoned", (
            "abandon must finalize to abandoned, not stay running",
            [a.status for a in acts],
        )

    # Mid-run provider exception -> FAILED, finished.
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("X.", "Y."))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    orig = orchestrator.run_completion
    set_pieces(
        "app.services.orchestrator",
        "run_completion",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    try:
        with Session(engine) as s:
            gen = orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES")
            next(gen)
            try:
                for _ in gen:
                    pass
            except RuntimeError:
                pass
    finally:
        set_pieces("app.services.orchestrator", "run_completion", orig)
    with Session(engine) as s:
        acts = s.exec(select(TapAction)).all()
        assert acts and acts[-1].status in ("failed", "abandoned"), (
            "mid-run exception must be terminal, not running",
            [a.status for a in acts],
        )
        assert acts[-1].status != "running"
    print("  (M1) workflow: abandon=abandoned, mid-run exception=terminal (not running) OK")


# --- M3: reserved JSON columns persist as SQL NULL, not the string 'null' ----
def test_m3_json_columns_are_sql_null():
    """FAIL-PRE-FIX: pre-fix Column(JSON) stored Python None as JSON 'null', so
    `WHERE manifest IS NULL` matched 0 rows -- Wave B keys on IS NULL."""
    _reset()
    from sqlalchemy import text as _text

    aid = tap_envelope.begin_action(kind="direct")
    rid = tap_envelope.begin_run(action_id=aid, target_locale="es-ES")
    assert aid and rid
    with Session(engine) as s:
        n_m = s.exec(_text("SELECT COUNT(*) FROM tapaction WHERE manifest IS NULL")).all()
        n_t = s.exec(_text("SELECT COUNT(*) FROM tapaction WHERE totals IS NULL")).all()
        n_d = s.exec(_text("SELECT COUNT(*) FROM taprun WHERE detail IS NULL")).all()
        assert n_m[0][0] == 1, ("manifest must be SQL NULL", n_m)
        assert n_t[0][0] == 1, ("totals must be SQL NULL", n_t)
        assert n_d[0][0] == 1, ("detail must be SQL NULL", n_d)
    print("  (M3) reserved JSON columns persist as SQL NULL (Wave B IS NULL works) OK")


# --- H2: disk-budget backstop withholds capture, flags the envelope ----------
def test_h2_disk_budget_backstop():
    """A tiny prompt_tap_actions_max_bytes must WITHHOLD new per-action capture (file
    stops growing) while the envelope keeps recording and is flagged capture_withheld."""
    _reset()
    # Pre-fill the actions dir OVER budget so the very next capture is withheld (an empty
    # dir would let the first write through before the budget could bite).
    ad = prompt_tap._actions_dir()
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "01BBBBBBBBBBBBBBBBBBBBBBBB.jsonl").write_text("x" * 4096, encoding="utf-8")
    settings.set_many({"prompt_tap_actions_max_bytes": "1024"})
    # Reset the budget cache + per-process flags so the tiny budget is seen immediately.
    prompt_tap._budget_cache["bytes"] = None
    prompt_tap._withheld_flagged.clear()
    prompt_tap._BUDGET_WARNED = False
    try:
        aid = tap_envelope.begin_action(kind="direct")
        rid = tap_envelope.begin_run(action_id=aid, target_locale="es-ES")
        rec = prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="translate",
            system="s",
            prompt="WITHHELD",
            max_tokens=8,
            payload={"text": "x"},
            outcome="ok",
            response_text="y",
            tap_meta={"action_id": aid, "run_id": rid},
        )
        prompt_tap.capture(rec)
        # No per-action file was written for this action (capture withheld).
        assert not prompt_tap._action_path(aid).exists(), (
            "over-budget capture must NOT write the per-action file"
        )
        # The envelope keeps recording and is flagged.
        with Session(engine) as s:
            a = s.get(TapAction, aid)
            assert a is not None and a.capture_withheld is True, (
                "envelope must be flagged capture_withheld",
                getattr(a, "capture_withheld", None),
            )
    finally:
        settings.set_many({"prompt_tap_actions_max_bytes": ""})
        prompt_tap._budget_cache["bytes"] = None
    print("  (H2) disk-budget backstop: capture withheld + envelope flagged, still recording OK")


# --- L3: a DIRECT call to the segmented generator also finalizes -------------
def test_l3_segmented_direct_call_finalizes():
    """~12 suites call run_workflow_stream_segmented directly; its own finalizer must
    transition the envelope (pre-L3 it would be left 'running')."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Seg one.", "Seg two."))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_id, doc_id, "es-ES"))
    with Session(engine) as s:
        acts = s.exec(select(TapAction)).all()
        assert len(acts) == 1 and acts[0].status == "completed", (
            "direct segmented call must finalize",
            [a.status for a in acts],
        )
    print("  (L3) direct run_workflow_stream_segmented call finalizes its envelope OK")


# --- F: legacy coexistence + clear scope -------------------------------------
def test_legacy_coexistence_and_clear_scope():
    _reset()
    # A legacy record straight to the global file (no action id) -- an "old" capture.
    legacy = prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="sys",
        prompt="LEGACY",
        max_tokens=8,
        payload={"text": "LEGACY"},
        outcome="ok",
        response_text="x",
    )
    prompt_tap.capture(legacy)
    assert _global_records(), "legacy record should be in the global file"

    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("New.",))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")

    # The legacy read path returns BOTH the legacy record and the new run captures.
    prompts = {(r.get("request") or {}).get("prompt") for r in prompt_tap.read_records(limit=100)}
    assert "LEGACY" in prompts, ("legacy record still readable", prompts)
    assert any("New." in (p or "") or "Translate" in (p or "") for p in prompts), prompts

    # clear() empties ONLY the legacy file; per-action run capture survives.
    with Session(engine) as s:
        action_ids = [a.id for a in s.exec(select(TapAction)).all()]
    dropped = prompt_tap.clear()
    assert dropped == 1, ("clear reports only the legacy record", dropped)
    assert _global_records() == [], "legacy file emptied"
    assert any(_action_records(aid) for aid in action_ids), (
        "per-action run capture must survive clear() (proposal section 8)"
    )
    print("  (F) legacy coexistence + clear() scoped to the global file OK")


# --- G: eval separation via capture routing ----------------------------------
def test_eval_separation_routing():
    _reset()
    # An eval-shaped capture: eval_* meta, NO action_id -> global file, no action.
    eval_rec = prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="s",
        prompt="EVAL",
        max_tokens=8,
        payload={"text": "EVAL"},
        outcome="ok",
        response_text="x",
        tap_meta={"eval_run_id": 5, "eval_kind": "translate"},
    )
    prompt_tap.capture(eval_rec)
    g = _global_records()
    assert len(g) == 1 and (g[0].get("meta") or {}).get("action_id") is None, g
    assert (g[0].get("meta") or {}).get("eval_run_id") == 5, g

    # A run-shaped capture WITH an action id -> its per-action file, not the global one.
    aid = tap_envelope.new_id()
    run_rec = prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="s",
        prompt="RUN",
        max_tokens=8,
        payload={"text": "RUN"},
        outcome="ok",
        response_text="y",
        tap_meta={"action_id": aid, "run_id": tap_envelope.new_id()},
    )
    prompt_tap.capture(run_rec)
    assert len(_global_records()) == 1, "the run record must not join the global file"
    assert len(_action_records(aid)) == 1, "the run record must be in its per-action file"
    print("  (G) eval separation: eval_* (no action_id) -> global; action_id -> per-action file OK")


# --- H: observer purity under an induced envelope-write failure --------------
def test_observer_purity_on_envelope_write_failure():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Pure.", "Safe."))
        doc_id = doc.id

    # Force every isolated envelope session to blow up at open.
    orig = tap_envelope._session
    tap_envelope._session = lambda: (_ for _ in ()).throw(RuntimeError("induced envelope failure"))
    logger = logging.getLogger("loc_tms.tap_envelope")
    handler = _ListHandler()
    logger.addHandler(handler)
    try:
        segs = []
        with Session(engine) as s:
            for ev in translate.pretranslate_stream(s, doc_id, "es-ES"):
                if ev.get("type") == "segment":
                    segs.append(ev)
        # The translation SUCCEEDED despite every envelope write failing.
        assert len(segs) == 2, ("translation must not be perturbed", segs)
        with Session(engine) as s:
            done = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
            assert all(x.target_text for x in done), "all segments translated"
        # No envelope rows were written (every write raised) ...
        with Session(engine) as s:
            assert s.exec(select(TapAction)).all() == [], "no envelope rows on failure"
        # ... but the failure was made VISIBLE at WARNING, not debug-swallowed.
        assert any(rec.levelno == logging.WARNING for rec in handler.records), (
            "an envelope-write failure must WARN (the outage lesson), not be silent"
        )
    finally:
        tap_envelope._session = orig
        logger.removeHandler(handler)
    print("  (H) observer purity: induced envelope-write failure -> run OK + one WARNING OK")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


# --- I: envelopes OFF -> no rows, run still succeeds -------------------------
def test_envelopes_off_no_rows():
    _reset(tap_on=True, envelopes_on=False)
    assert not tap_envelope.envelopes_enabled()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Off.",))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        assert s.exec(select(TapAction)).all() == [], "no envelopes when recording is OFF"
    # Captures then carry no action_id -> the global legacy file.
    assert _global_records(), "captures still happen (tap on), routed to the global file"
    assert all((r.get("meta") or {}).get("action_id") is None for r in _global_records())
    print("  (I) envelopes OFF: no rows, run completes, captures fall back to global file OK")


# --- J: migration fresh + create_all -----------------------------------------
def test_migration_and_create_all_build_tables():
    # create_all path (this process already ran init_db at import via other tests, but
    # assert the tables + indexes exist here explicitly).
    from sqlalchemy import inspect

    insp = inspect(engine)
    for t in ("tapaction", "taprun"):
        assert insp.has_table(t), t
    tai = {i["name"] for i in insp.get_indexes("tapaction")}
    assert {
        "ix_tapaction_started_at",
        "ix_tapaction_project_id",
        "ix_tapaction_document_id",
        "ix_tapaction_batch_id",
        "ix_tapaction_status",
    } <= tai, tai
    tri = {i["name"] for i in insp.get_indexes("taprun")}
    assert {
        "ix_taprun_started_at",
        "ix_taprun_action_id",
        "ix_taprun_workflow_run_id",
        "ix_taprun_status",
    } <= tri, tri

    # alembic upgrade head on a FRESH db builds them too (no create_all first).
    fresh = tempfile.mkdtemp(prefix="loctms_tapenv_mig_")
    env = dict(os.environ, LOC_TMS_DATA_DIR=fresh)
    code = (
        f"import os,sys; sys.path.insert(0, r'{_ROOT}');"
        "from alembic import command; from alembic.config import Config;"
        f"cfg=Config(os.path.join(r'{_ROOT}','alembic.ini'));"
        f"cfg.set_main_option('script_location', os.path.join(r'{_ROOT}','alembic'));"
        f"url='sqlite:///'+os.path.join(r'{fresh}','loc_tms.db').replace('\\\\','/');"
        "cfg.set_main_option('sqlalchemy.url', url);"
        "command.upgrade(cfg,'head');"
        "from sqlalchemy import create_engine, inspect;"
        "insp=inspect(create_engine(url));"
        "assert insp.has_table('tapaction') and insp.has_table('taprun');"
        "cols=set(c['name'] for c in insp.get_columns('tapaction'));"
        "assert 'manifest' in cols and 'totals' in cols and 'batch_id' in cols, cols;"
        "print('MIGRATION_TABLES_OK')"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180
        )
        assert "MIGRATION_TABLES_OK" in out.stdout, (out.stdout, out.stderr)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    print("  (J) migration fresh-upgrade + create_all both build tapaction/taprun OK")


def test_guarded_envelope_switch():
    from app.services import ledger_controls

    ledger_controls.set_envelopes(False, confirm="disable envelope recording", actor="suite")
    assert not tap_envelope.envelopes_enabled()
    ledger_controls.set_envelopes(True, actor="suite")
    assert tap_envelope.envelopes_enabled()


def test_recording_stops_resumes_and_reaper_runs_off():
    """Gate round-1 LOW-3: prove RECORDING itself stops when the switch is off and resumes
    when on (observer-level row counts, not just the flag), and that the orphan reaper still
    transitions a stale RUNNING envelope while recording is OFF (checkpoint 13 / L5)."""
    from datetime import datetime as _dt

    # OFF: a real workflow run records NO envelope rows.
    _reset(tap_on=True, envelopes_on=False)
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("Off.",))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        assert orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")["status"] == "completed"
    with Session(engine) as s:
        assert s.exec(select(TapAction)).all() == [], "OFF -> zero envelope rows recorded"
    # ON: the SAME run shape records at least one envelope row.
    _reset(tap_on=True, envelopes_on=True)
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_segmented_doc(s, proj, sources=("On.",))
        wf = _mk_translate_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        assert orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")["status"] == "completed"
    with Session(engine) as s:
        assert len(s.exec(select(TapAction)).all()) >= 1, "ON -> envelope rows recorded"
    # REAPER runs even with recording OFF: a stale RUNNING row from before the toggle must
    # still be closed (never stranded).
    _reset(tap_on=True, envelopes_on=False)
    aid = tap_envelope.new_id()
    with Session(engine) as s:
        s.add(TapAction(id=aid, status=TapStatus.RUNNING, started_at=_dt(2020, 1, 1)))
        s.commit()
    out = tap_envelope.reap_orphaned_envelopes(now=_dt(2020, 1, 2), max_age_hours=1)
    assert out["actions"] >= 1, out
    with Session(engine) as s:
        assert s.get(TapAction, aid).status in TapStatus.TERMINAL


def main():
    assert {name for name in globals() if name.startswith("test_")} <= set(main.__code__.co_names)
    init_db()
    test_guarded_envelope_switch()
    test_recording_stops_resumes_and_reaper_runs_off()
    test_segmented_workflow_envelope_and_stamping()
    test_fulltext_workflow_envelope()
    test_direct_mt_and_adhoc_envelope()
    test_batch_leg_envelope_carries_batch_id()
    test_resume_reuse_idempotent()
    test_r1_batch_leg_rowid_reuse_no_adopt()
    test_r1_multi_pause_resume_single_envelope()
    test_race_envelope_terminal_atomic_with_run()
    test_race_finalize_is_atomic_with_run()
    test_n1_terminal_commit_rides_out_held_writer_lock()
    test_h2_segmented_failure_finalizes_envelope()
    test_cancelled_run_envelope_abandoned()
    test_h1_rowid_reuse_no_cross_tenant()
    test_h3_union_read_orders_by_timestamp()
    test_m2_direct_status_honesty()
    test_m1_workflow_outcome_status()
    test_m3_json_columns_are_sql_null()
    test_h2_disk_budget_backstop()
    test_l3_segmented_direct_call_finalizes()
    test_legacy_coexistence_and_clear_scope()
    test_eval_separation_routing()
    test_observer_purity_on_envelope_write_failure()
    test_envelopes_off_no_rows()
    test_migration_and_create_all_build_tables()
    print("TAP ENVELOPES OK")


if __name__ == "__main__":
    main()
