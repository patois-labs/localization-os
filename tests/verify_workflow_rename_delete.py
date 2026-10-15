"""Workflow-family rename + delete verification (SPEC-rename-delete, Branch 1).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls. Covers the Branch-1 slice: rename + delete for Workflow, WorkflowGroup
and PromptTemplate across the service seam + web + CLI + /api.

Every function added here is NET-NEW at HEAD (64f763a has no rename_workflow /
delete_workflow / rename_group / delete_group / rename_prompt_template /
delete_prompt_template in app/services/workflows.py, no /workflows/{id}/rename|delete
routes, no `workflow rename|delete` CLI subcommands, no /api/workflows/{wid}/rename|
delete endpoints), so this whole suite FAILS pre-fix (AttributeError / 404 / SystemExit).

Covers:

  service seam
    - rename_workflow: name changes, id-based references still resolve (propagation)
    - workflow delete FAILS CLOSED on RoutingRule, Customer.default_workflow_id,
      an in-flight run, and a group-default-with-siblings — each with an itemized count
    - workflow delete cascades EXACTLY its steps + WorkflowRun + StepRun
    - workflow delete reaps its OWN WorkflowBatch fan-out rows, un-scopes sibling-variant
      member runs (batch_id NULL) + Lab EvalCandidate.workflow_id, and records the counts
    - workflow delete BLOCKS on an active batch member owned by a sibling variant, on a
      live (finished_at NULL) zero-member batch (the dispatch pre-first-member window, H1),
      and on a LAW-2 MemorySuggestion tombstone (archive, never reap, H2)
    - workflow delete un-scopes a MemoryEvent captured on one of its runs (#137) and fails
      CLOSED on a VendorDispatch leg anchored to one (NOT NULL, durable vendor record) --
      no IntegrityError ever escapes delete_workflow for an FK onto workflowrun.id
    - rename_group / delete_group: group delete fails closed while it has members
    - rename_prompt_template / delete_prompt_template: fails closed while a step pins it

  web surface (auth ON, logged in)
    - linguist may rename a workflow (family gate) + audit has old->new
    - a non-admin (linguist) is BLOCKED from delete on web (403); admin may delete
    - group + prompt-template delete are admin-only on web too

  CLI surface
    - `workflow rename` + audit old->new; `workflow delete --force` cascades;
      `workflow delete --force` fails closed (nonzero) while a routing rule references it;
      preview without --force does not delete; group + prompt subcommands; ASCII-only

  /api surface
    - rename mirrors the family gate (can_tune_ai); delete is admin-only (403 for a
      non-admin token, 200 for admin); delete fails closed (409) while referenced

Final line: "WORKFLOW RENAME/DELETE OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfrd_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    AuditLog,
    BatchStatus,
    Customer,
    Document,
    EvalCandidate,
    MemoryEvent,
    MemorySuggestion,
    Project,
    PromptTemplate,
    Role,
    RoutingRule,
    RunStatus,
    Scope,
    StepRun,
    StepType,
    Vendor,
    VendorDispatch,
    Workflow,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
    _utcnow,
)
from app.services import apikeys, security, settings
from app.services import workflows as wf


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- small builders ----------------------------------------------------------
def _mk_workflow(session, name, **kw):
    w = Workflow(name=name, **kw)
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


def _mk_step(session, wid, name="Translator"):
    st = WorkflowStep(workflow_id=wid, order_index=0, name=name, step_type=StepType.LLM_AGENT)
    session.add(st)
    session.commit()
    session.refresh(st)
    return st


def _mk_doc(session, name="wfrd-proj"):
    p = Project(name=name, source_locale="en-US")
    session.add(p)
    session.commit()
    session.refresh(p)
    d = Document(project_id=p.id, filename="d.docx")
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def _mk_run(session, wid, doc_id, status=RunStatus.COMPLETED, batch_id=None):
    r = WorkflowRun(workflow_id=wid, document_id=doc_id, status=status, batch_id=batch_id)
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def _mk_batch(session, wid, doc_id, locales=("es-ES",), finished=True):
    # finished=True stamps finished_at (a FINALIZED batch, the reapable state); finished=False
    # leaves finished_at NULL = an IN-FLIGHT batch, which delete_workflow must block (H1).
    b = WorkflowBatch(
        workflow_id=wid,
        document_id=doc_id,
        requested_locales=list(locales),
        status=BatchStatus.COMPLETED if finished else BatchStatus.RUNNING,
        finished_at=_utcnow() if finished else None,
    )
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


# ============================================================================
def test_service_rename_and_propagation():
    with Session(engine) as s:
        w = _mk_workflow(s, "PropA")
        wid = w.id
        _mk_step(s, wid)
        d = _mk_doc(s, "prop-proj")
        run = _mk_run(s, wid, d.id, status=RunStatus.COMPLETED)
        run_id = run.id
        rr = RoutingRule(match_ext="srt", workflow_id=wid)
        s.add(rr)
        s.commit()
        rr_id = rr.id
    # rename via the service seam
    with Session(engine) as s:
        out = wf.rename_workflow(s, wid, "PropB")
        assert out.name == "PropB", out.name
    # propagation: id-based refs still resolve to the SAME (renamed) workflow
    with Session(engine) as s:
        assert s.get(Workflow, wid).name == "PropB"
        assert s.get(RoutingRule, rr_id).workflow_id == wid  # routing intact
        run = s.get(WorkflowRun, run_id)
        assert run.workflow_id == wid  # run intact
        # the "step display / SSE" reads the live workflow name off the id
        assert s.get(Workflow, run.workflow_id).name == "PropB"
    # blank name rejected
    with Session(engine) as s:
        try:
            wf.rename_workflow(s, wid, "   ")
            _fail("blank rename should raise")
        except wf.WorkflowError:
            pass
    print("  service rename + id-based propagation (routing rule + run resolve): OK")


def test_service_delete_fails_closed():
    # (a) routing-rule referrer
    with Session(engine) as s:
        w = _mk_workflow(s, "FCRouting")
        wid = w.id
        s.add(RoutingRule(match_ext="vtt", workflow_id=wid))
        s.commit()
    with Session(engine) as s:
        refs = wf.workflow_reference_count(s, wid)
        assert refs["routing_rules"] == 1 and refs["blockers"] >= 1, refs
        try:
            wf.delete_workflow(s, wid)
            _fail("delete must fail closed while a routing rule references it")
        except wf.WorkflowError as exc:
            assert "routing rule" in str(exc), str(exc)
            assert exc.refs and exc.refs["routing_rules"] == 1
    # (b) customer default referrer
    with Session(engine) as s:
        w = _mk_workflow(s, "FCCustDefault")
        wid = w.id
        cust = Customer(name="FC Cust", default_workflow_id=wid)
        s.add(cust)
        s.commit()
    with Session(engine) as s:
        refs = wf.workflow_reference_count(s, wid)
        assert refs["customer_defaults"] == 1, refs
        try:
            wf.delete_workflow(s, wid)
            _fail("delete must fail closed while a customer default references it")
        except wf.WorkflowError as exc:
            assert "customer default" in str(exc), str(exc)
    # (c) in-flight run (running / pending / paused all block)
    for st in (RunStatus.RUNNING, RunStatus.PENDING, RunStatus.PAUSED):
        with Session(engine) as s:
            w = _mk_workflow(s, f"FCRun-{st}")
            wid = w.id
            d = _mk_doc(s, f"fc-run-{st}")
            _mk_run(s, wid, d.id, status=st)
        with Session(engine) as s:
            refs = wf.workflow_reference_count(s, wid)
            assert refs["active_runs"] == 1, (st, refs)
            try:
                wf.delete_workflow(s, wid)
                _fail(f"delete must fail closed on an in-flight ({st}) run")
            except wf.WorkflowError as exc:
                assert "in-flight run" in str(exc), str(exc)
    # (d) group default with surviving siblings
    with Session(engine) as s:
        grp = wf.create_group(s, "FCGroup")
        gid = grp.id
        default_wf = _mk_workflow(s, "FCDefault", group_id=gid, is_default=True)
        _mk_workflow(s, "FCSibling", group_id=gid, is_default=False)
        wid = default_wf.id
    with Session(engine) as s:
        refs = wf.workflow_reference_count(s, wid)
        assert refs["group_default"] == 1, refs
        try:
            wf.delete_workflow(s, wid)
            _fail("delete must fail closed on a group default with siblings")
        except wf.WorkflowError as exc:
            assert "group" in str(exc), str(exc)
    print(
        "  service delete FAILS CLOSED (routing / customer-default / in-flight / group-default): OK"
    )


def test_service_delete_cascade_exact():
    with Session(engine) as s:
        w = _mk_workflow(s, "CascadeMe")
        wid = w.id
        st1 = _mk_step(s, wid, "S1")
        st2 = _mk_step(s, wid, "S2")
        d = _mk_doc(s, "cascade-proj")
        run = _mk_run(s, wid, d.id, status=RunStatus.COMPLETED)
        s.add(StepRun(workflow_run_id=run.id, step_id=st1.id))
        s.add(StepRun(workflow_run_id=run.id, step_id=st2.id))
        s.commit()
        step_ids = [st1.id, st2.id]
        run_id = run.id
        # an UNRELATED workflow whose step is an adapted COPY of one of ours -> its
        # adapted_from_id must be nulled (survives, loses provenance), not deleted.
        other = _mk_workflow(s, "OtherWf")
        copy_step = WorkflowStep(
            workflow_id=other.id,
            order_index=0,
            name="copy",
            step_type=StepType.LLM_AGENT,
            adapted_from_id=st1.id,
        )
        s.add(copy_step)
        s.commit()
        copy_id = copy_step.id
    with Session(engine) as s:
        refs = wf.delete_workflow(s, wid)
        assert refs["steps"] == 2 and refs["runs"] == 1 and refs["step_runs"] == 2, refs
    with Session(engine) as s:
        assert s.get(Workflow, wid) is None
        assert s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).all() == []
        assert s.exec(select(WorkflowRun).where(WorkflowRun.id == run_id)).all() == []
        assert s.exec(select(StepRun).where(StepRun.step_id.in_(step_ids))).all() == []
        # the cross-language copy survived with a NULLED provenance link
        surviving = s.get(WorkflowStep, copy_id)
        assert surviving is not None and surviving.adapted_from_id is None
    print("  service delete cascade (exactly steps+runs+step-runs; copy provenance nulled): OK")


def test_service_delete_reaps_own_batch():
    # A workflow that dispatched a fan-out batch with member runs of ITSELF must delete.
    # WorkflowBatch.workflow_id is a NOT-NULL FK to workflow.id, so before the 4b reap
    # session.delete(wf) raised IntegrityError and the whole delete rolled back -- the
    # workflow was permanently undeletable once it had ever fanned out.
    with Session(engine) as s:
        w = _mk_workflow(s, "BatchOwner")
        wid = w.id
        d = _mk_doc(s, "batch-owner-proj")
        batch = _mk_batch(s, wid, d.id, locales=("es-ES", "fr-FR"))
        bid = batch.id
        # two member runs of THIS workflow (terminal, so not blockers)
        _mk_run(s, wid, d.id, status=RunStatus.COMPLETED, batch_id=bid)
        _mk_run(s, wid, d.id, status=RunStatus.FAILED, batch_id=bid)
    with Session(engine) as s:
        refs = wf.delete_workflow(s, wid)
        assert refs["runs"] == 2, refs
    with Session(engine) as s:
        assert s.get(Workflow, wid) is None
        assert s.get(WorkflowBatch, bid) is None
        assert s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).all() == []
    print("  service delete reaps its OWN fan-out batch (was the IntegrityError repro): OK")


def test_service_delete_reaps_batch_with_variant_member():
    # A batch of THIS workflow can carry a member run owned by a sibling VARIANT workflow
    # (resolve_variant). That variant run is NOT in the workflow_id-keyed run sweep, so it
    # must SURVIVE with batch_id un-scoped to NULL; the batch row is gone; the variant
    # workflow is untouched.
    with Session(engine) as s:
        requested = _mk_workflow(s, "ReqWf")
        variant = _mk_workflow(s, "VariantWf")
        wid = requested.id
        vid = variant.id
        d = _mk_doc(s, "variant-member-proj")
        batch = _mk_batch(s, wid, d.id, locales=("es-ES",))
        bid = batch.id
        # member run belongs to the VARIANT workflow, not the requested one
        variant_run = _mk_run(s, vid, d.id, status=RunStatus.COMPLETED, batch_id=bid)
        variant_run_id = variant_run.id
    with Session(engine) as s:
        wf.delete_workflow(s, wid)
    with Session(engine) as s:
        assert s.get(Workflow, wid) is None
        assert s.get(WorkflowBatch, bid) is None
        # the variant workflow is untouched
        assert s.get(Workflow, vid) is not None
        # the variant member run SURVIVES with a NULLED batch_id
        surviving = s.get(WorkflowRun, variant_run_id)
        assert surviving is not None and surviving.batch_id is None, surviving
        assert surviving.workflow_id == vid
    print("  service delete un-scopes + reaps a batch whose member is a variant sibling: OK")


def test_service_delete_repro_finished_batch_mixed_members():
    # The exact PR #81 / issue #82 reproduction (scratchpad REPRO-pr81.md): a workflow with a
    # step + doc, a FINISHED WorkflowBatch it owns, TWO member runs OF ITSELF, and a THIRD
    # member run owned by a sibling VARIANT (same batch, different workflow_id). On main this
    # raised `IntegrityError FOREIGN KEY constraint failed` on `DELETE FROM workflow` because
    # step 4b never reaped the batch. Assert the full outcome AND preview/outcome symmetry
    # (LOW-4): the forecast in workflow_reference_count matches what delete un-scopes.
    with Session(engine) as s:
        owner = _mk_workflow(s, "ReproOwner")
        variant = _mk_workflow(s, "ReproVariant")
        wid = owner.id
        vid = variant.id
        _mk_step(s, wid)
        d = _mk_doc(s, "repro-mixed-proj")
        batch = _mk_batch(s, wid, d.id, locales=("es-ES", "fr-FR"), finished=True)
        bid = batch.id
        # two member runs of THIS workflow (terminal) ...
        _mk_run(s, wid, d.id, status=RunStatus.COMPLETED, batch_id=bid)
        _mk_run(s, wid, d.id, status=RunStatus.FAILED, batch_id=bid)
        # ... plus one sibling-variant member run on the SAME batch
        variant_run = _mk_run(s, vid, d.id, status=RunStatus.COMPLETED, batch_id=bid)
        variant_run_id = variant_run.id
    # preview forecasts what the delete WILL un-scope (siblings only; this workflow's own
    # members die at step 3), symmetric with the delete outcome keys.
    with Session(engine) as s:
        prev = wf.workflow_reference_count(s, wid)
        assert prev["workflow_batches"] == 1, prev
        assert prev["batch_members_unscoped"] == 1, prev  # the one variant sibling
        assert prev["eval_candidates_unscoped"] == 0, prev
        assert prev["blockers"] == 0 and prev["memory_suggestions"] == 0, prev
    with Session(engine) as s:
        refs = wf.delete_workflow(s, wid)
        # counts reported: own members cascaded, batch reaped, sibling un-scoped
        assert refs["runs"] == 2, refs
        assert refs["workflow_batches"] == 1, refs
        assert refs["batch_members_unscoped"] == 1, refs
        assert refs["eval_candidates_unscoped"] == 0, refs
    with Session(engine) as s:
        # workflow + its batch + its own runs are gone
        assert s.get(Workflow, wid) is None
        assert s.get(WorkflowBatch, bid) is None
        assert s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).all() == []
        # the variant workflow is untouched; its member run survives with batch_id nulled
        assert s.get(Workflow, vid) is not None
        surviving = s.get(WorkflowRun, variant_run_id)
        assert surviving is not None and surviving.batch_id is None
        assert surviving.workflow_id == vid
    print("  service delete: REPRO (finished batch + own members + variant member) succeeds: OK")


def test_service_delete_blocks_active_variant_member():
    # The new blocker: a batch of THIS workflow whose only ACTIVE member run belongs to a
    # sibling VARIANT would (before the fix) NOT block, and the 4b reap would destroy the
    # rollup mid-flight + un-scope a RUNNING member. It must fail closed while that member
    # is active, then delete once the member reaches a terminal status.
    with Session(engine) as s:
        requested = _mk_workflow(s, "ReqWfActive")
        variant = _mk_workflow(s, "VariantWfActive")
        wid = requested.id
        vid = variant.id
        d = _mk_doc(s, "active-variant-proj")
        batch = _mk_batch(s, wid, d.id, locales=("es-ES",))
        bid = batch.id
        run = _mk_run(s, vid, d.id, status=RunStatus.RUNNING, batch_id=bid)
        run_id = run.id
    with Session(engine) as s:
        # counts the variant member even though its own workflow_id != this workflow
        refs = wf.workflow_reference_count(s, wid)
        assert refs["active_runs"] == 1, refs
        try:
            wf.delete_workflow(s, wid)
            _fail("delete must fail closed on an in-flight variant member of this workflow's batch")
        except wf.WorkflowError as exc:
            assert "in-flight run" in str(exc), str(exc)
    # L6: the failed attempt must have mutated NOTHING (fail closed BEFORE any write) --
    # the workflow, the batch, and the member's batch_id are all intact.
    with Session(engine) as s:
        assert s.get(Workflow, wid) is not None
        assert s.get(WorkflowBatch, bid) is not None
        assert s.get(WorkflowRun, run_id).batch_id == bid
    # the member reaches a terminal status -> the batch no longer blocks
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.COMPLETED
        s.add(run)
        s.commit()
    with Session(engine) as s:
        assert wf.workflow_reference_count(s, wid)["active_runs"] == 0
        wf.delete_workflow(s, wid)
        assert s.get(Workflow, wid) is None
        assert s.get(WorkflowBatch, bid) is None
        # the (now terminal) variant member still survives with batch_id nulled
        surviving = s.get(WorkflowRun, run_id)
        assert surviving is not None and surviving.batch_id is None
    print("  service delete blocks on an ACTIVE variant batch member, allows once terminal: OK")


def test_service_delete_blocks_live_zero_member_batch():
    # H1: dispatch_batch COMMITS the WorkflowBatch row THEN spawns the dispatcher, so for a
    # window the batch is live (finished_at IS NULL) with ZERO member runs. The active-member
    # count reads 0 there, so liveness must come from finished_at -- otherwise step 4b reaps
    # the live batch and the dispatcher's next _create_member hits a vanished-parent
    # IntegrityError, silently dropping the whole fan-out. Block while finished_at is NULL.
    with Session(engine) as s:
        w = _mk_workflow(s, "LiveZeroBatch")
        wid = w.id
        d = _mk_doc(s, "live-zero-proj")
        batch = _mk_batch(s, wid, d.id, locales=("es-ES", "de-DE"), finished=False)
        bid = batch.id  # NO member runs -- the pre-first-member window
    with Session(engine) as s:
        refs = wf.workflow_reference_count(s, wid)
        assert refs["active_runs"] == 0 and refs["live_batches"] == 1, refs
        assert refs["blockers"] >= 1, refs
        try:
            wf.delete_workflow(s, wid)
            _fail("delete must fail closed on a live (finished_at NULL) zero-member batch")
        except wf.WorkflowError as exc:
            # discriminate the live_batches clause specifically -- "live fan-out batch" is unique
            # to it, whereas bare "fan-out batch" also matches the active_runs "fan-out batch
            # member(s)" wording, so it would pass even if the wrong blocker fired (LOW-6).
            assert "live fan-out batch" in str(exc), str(exc)
    # nothing mutated; once the batch finalizes (finished_at stamped) the delete proceeds
    with Session(engine) as s:
        assert s.get(Workflow, wid) is not None and s.get(WorkflowBatch, bid) is not None
        b = s.get(WorkflowBatch, bid)
        b.finished_at = _utcnow()
        b.status = BatchStatus.COMPLETED
        s.add(b)
        s.commit()
    with Session(engine) as s:
        assert wf.workflow_reference_count(s, wid)["live_batches"] == 0
        refs = wf.delete_workflow(s, wid)
        assert refs["workflow_batches"] == 1, refs
        assert s.get(Workflow, wid) is None and s.get(WorkflowBatch, bid) is None
    print("  service delete blocks a live zero-member batch, reaps it once finalized (H1): OK")


def test_service_delete_blocks_memory_suggestion():
    # H2: MemorySuggestion.workflow_id is a NOT-NULL FK and LAW 2 keeps the row as a permanent
    # audit tombstone (never hard-deleted), so it can be neither reaped nor un-scoped. Delete
    # must fail closed and point the operator at ARCHIVE (the sanctioned alternative).
    # MED-2: pin the PERMANENCE ordering. Give this workflow BOTH a LAW-2 tombstone (permanent,
    # archive-only) AND a live zero-member fan-out batch (transient, "just let it finish"). The
    # tombstone guard runs FIRST, so the operator must hear the permanent obstruction, never be
    # told to WAIT for a batch behind a blocker they can never clear. Without the batch this test
    # is satisfied by the tombstone guard alone regardless of where it sits in the order, so the
    # LOW-3 ordering could be silently demoted; the live batch is what makes the order load-bearing.
    with Session(engine) as s:
        cust = Customer(name="MS Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        w = _mk_workflow(s, "SuggestTarget")
        wid = w.id
        d = _mk_doc(s, "suggest-target-proj")
        _mk_batch(s, wid, d.id, locales=("es-ES", "de-DE"), finished=False)  # live, zero members
        s.add(MemorySuggestion(kind="tune_prompt", customer_id=cust.id, workflow_id=wid))
        s.commit()
    with Session(engine) as s:
        refs = wf.workflow_reference_count(s, wid)
        # BOTH obstructions present -> the ordering assertion below is not vacuous
        assert refs["memory_suggestions"] == 1, refs
        assert refs["live_batches"] == 1, refs
        try:
            wf.delete_workflow(s, wid)
            _fail("delete must fail closed while a MemorySuggestion tombstone targets it")
        except wf.WorkflowError as exc:
            msg = str(exc)
            assert "archive" in msg.lower() and "suggestion" in msg.lower(), msg
            # the PERMANENT tombstone message wins, NOT the transient live-batch one
            assert "permanent audit records" in msg, msg
            assert "live fan-out batch" not in msg, msg
    with Session(engine) as s:
        # workflow AND the suggestion tombstone both survive intact
        assert s.get(Workflow, wid) is not None
        assert (
            s.exec(select(MemorySuggestion).where(MemorySuggestion.workflow_id == wid)).first()
            is not None
        )
    print("  service delete blocks (archive, never reap) while a LAW-2 suggestion targets it: OK")


def test_service_delete_unscopes_eval_candidate():
    # H2: EvalCandidate.workflow_id is a NULLABLE FK -- it does not self-null on parent delete,
    # so it would trip the same IntegrityError. UN-SCOPE it (the candidate survives, its config
    # snapshot preserved), never reap.
    with Session(engine) as s:
        w = _mk_workflow(s, "EvalTarget")
        wid = w.id
        cand = EvalCandidate(name="cand-under-test", workflow_id=wid, config={"provider": "mock"})
        s.add(cand)
        s.commit()
        s.refresh(cand)
        cand_id = cand.id
    with Session(engine) as s:
        refs = wf.delete_workflow(s, wid)
        assert refs["eval_candidates_unscoped"] == 1, refs
    with Session(engine) as s:
        assert s.get(Workflow, wid) is None
        surviving = s.get(EvalCandidate, cand_id)
        assert surviving is not None and surviving.workflow_id is None
        assert surviving.config == {"provider": "mock"}  # snapshot preserved
    print("  service delete un-scopes a Lab EvalCandidate (survives, config intact): OK")


def test_service_delete_unscopes_memory_event():
    # #137: MemoryEvent.workflow_run_id is a NULLABLE FK onto workflowrun.id. A nullable FK
    # does NOT self-null when its parent row is deleted, so an event captured against one of
    # this workflow's runs made the step-3 WorkflowRun delete raise sqlite3.IntegrityError
    # (FOREIGN KEY constraint failed) and roll the whole delete back -- the crash the showcase
    # seed's --cleanup hit. UN-SCOPE the event first (the episode is the CUSTOMER's, not the
    # run's: the same treatment retention.unscope_memory_event_runs already gives the document
    # and project deletes), then reap the run.
    with Session(engine) as s:
        w = _mk_workflow(s, "MemEventTarget")
        wid = w.id
        doc = _mk_doc(s, "wfrd-memevent")
        run = _mk_run(s, wid, doc.id, status=RunStatus.COMPLETED)
        run_id = run.id
        ev = MemoryEvent(
            kind="edit",
            document_id=doc.id,
            workflow_run_id=run_id,
            source_locale="en-US",
            target_locale="es-ES",
            before_text="before",
            after_text="after",
            actor="tester",
        )
        s.add(ev)
        s.commit()
        s.refresh(ev)
        ev_id = ev.id
    # the forecast names it BEFORE the delete (preview/outcome symmetry, LOW-4)
    with Session(engine) as s:
        assert wf.workflow_reference_count(s, wid)["memory_events_unscoped"] == 1
    with Session(engine) as s:
        refs = wf.delete_workflow(s, wid)
        assert refs["memory_events_unscoped"] == 1, refs
    with Session(engine) as s:
        assert s.get(Workflow, wid) is None
        assert s.get(WorkflowRun, run_id) is None
        surviving = s.get(MemoryEvent, ev_id)
        assert surviving is not None, "the memory event must OUTLIVE its run (un-scoped)"
        assert surviving.workflow_run_id is None, surviving.workflow_run_id
        assert surviving.before_text == "before" and surviving.after_text == "after"
    print("  service delete un-scopes a MemoryEvent on its run (#137, no IntegrityError): OK")


def test_service_delete_blocks_vendor_dispatch():
    # #137 (the second FK onto workflowrun.id): VendorDispatch.workflow_run_id is NOT NULL and
    # a dispatch leg is a DURABLE vendor record (the claim-token / estimate-vs-actual anchor),
    # so it can be neither un-scoped (NOT NULL) nor reaped (it is the buyer's commercial
    # receipt). Fail CLOSED naming the way out (archive), never an escaping IntegrityError.
    with Session(engine) as s:
        w = _mk_workflow(s, "VendorLegTarget")
        wid = w.id
        step = _mk_step(s, wid, name="External")
        step_id = step.id
        doc = _mk_doc(s, "wfrd-vendorleg")
        run = _mk_run(s, wid, doc.id, status=RunStatus.COMPLETED)
        run_id = run.id
        trace = StepRun(workflow_run_id=run_id, step_id=step_id, status=RunStatus.COMPLETED)
        s.add(trace)
        vendor = Vendor(name="Acme Localization")
        s.add(vendor)
        s.commit()
        s.refresh(trace)
        s.refresh(vendor)
        trace_id = trace.id
        leg = VendorDispatch(
            vendor_id=vendor.id,
            workflow_run_id=run_id,
            step_id=step_id,
            document_id=doc.id,
            target_locale="es-ES",
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        leg_id = leg.id
    with Session(engine) as s:
        refs = wf.workflow_reference_count(s, wid)
        assert refs["vendor_dispatches"] == 1, refs
        # a permanent guard, NOT summed into the transient blockers (its own wording)
        assert refs["blockers"] == 0, refs
    with Session(engine) as s:
        try:
            wf.delete_workflow(s, wid)
            _fail("workflow delete must fail closed while a vendor dispatch leg anchors a run")
        except wf.WorkflowError as exc:
            text = str(exc)
            assert "vendor dispatch" in text, text
            assert "archive" in text.lower(), text
        except Exception as exc:  # an IntegrityError must NEVER escape delete_workflow
            _fail(f"delete_workflow leaked {type(exc).__name__}: {exc}")
    # rollback proof: the refusal destroyed NOTHING
    with Session(engine) as s:
        assert s.get(Workflow, wid) is not None
        assert s.get(WorkflowStep, step_id) is not None
        assert s.get(WorkflowRun, run_id) is not None
        assert s.get(StepRun, trace_id) is not None
        assert s.get(VendorDispatch, leg_id) is not None
    print("  service delete fails closed on a VendorDispatch leg (archive; nothing lost): OK")


def test_workflow_delete_block_reason_covers_all_guards():
    # LOW-2: ONE read-only eligibility check backs both delete_workflow's raise AND the
    # showcase-seed --dry-run forecast, so a preview can never disagree with the outcome.
    # It must cover EVERY delete_workflow guard, in the same permanence-first order.
    from unittest.mock import patch

    from app.services import workflows_part3

    # clean workflow -> deletable (None)
    with Session(engine) as s:
        w = _mk_workflow(s, "EligClean")
        clean_id = w.id
    with Session(engine) as s:
        assert wf.workflow_delete_block_reason(s, clean_id) is None

    # (1) a LAW-2 suggestion tombstone
    cust_id = _customer("EligSuggCust")
    with Session(engine) as s:
        w = _mk_workflow(s, "EligSuggestion")
        sug_id = w.id
        s.add(MemorySuggestion(kind="tune_prompt", customer_id=cust_id, workflow_id=sug_id))
        s.commit()
    with Session(engine) as s:
        reason = wf.workflow_delete_block_reason(s, sug_id)
        assert reason and "suggestion(s)" in reason and "Archive" in reason, reason

    # (2) a vendor dispatch leg
    with Session(engine) as s:
        w = _mk_workflow(s, "EligVendor")
        ven_id = w.id
        doc = _mk_doc(s, "elig-vendor")
        run = _mk_run(s, ven_id, doc.id)
        vendor = Vendor(name="Elig vendor")
        s.add(vendor)
        s.commit()
        s.refresh(vendor)
        s.add(VendorDispatch(vendor_id=vendor.id, workflow_run_id=run.id, target_locale="es-ES"))
        s.commit()
    with Session(engine) as s:
        reason = wf.workflow_delete_block_reason(s, ven_id)
        assert reason and "vendor dispatch leg(s)" in reason, reason

    # (3) the empty-held tm_match provenance orphan -- the guard the old forecast OMITTED.
    #     Patched at the seam (the orphan detection itself is exercised in verify_workflow_*
    #     delete tests); here we prove the SHARED check consults it and surfaces its message.
    with Session(engine) as s:
        w = _mk_workflow(s, "EligHeld")
        held_id = w.id
    with Session(engine) as s:
        with patch.object(
            workflows_part3, "_empty_held_marker_orphans", return_value=[(42, "es-ES")]
        ):
            reason = wf.workflow_delete_block_reason(s, held_id)
        assert reason and "tm_match provenance marker" in reason and "doc 42" in reason, reason

    # (4) a transient blocker (routing rule)
    with Session(engine) as s:
        w = _mk_workflow(s, "EligBlocker")
        blk_id = w.id
        s.add(RoutingRule(match_ext="po", workflow_id=blk_id))
        s.commit()
    with Session(engine) as s:
        reason = wf.workflow_delete_block_reason(s, blk_id)
        assert reason and "routing rule(s)" in reason, reason
    print("  workflow_delete_block_reason covers suggestion/vendor/empty-held/blocker: OK")


def test_service_group_rename_delete():
    with Session(engine) as s:
        grp = wf.create_group(s, "GrpA")
        gid = grp.id
        member = _mk_workflow(s, "GrpMember", group_id=gid)
        member_id = member.id
    with Session(engine) as s:
        assert wf.rename_group(s, gid, "GrpB").name == "GrpB"
    # delete fails closed while it has members
    with Session(engine) as s:
        refs = wf.group_reference_count(s, gid)
        assert refs["members"] == 1, refs
        try:
            wf.delete_group(s, gid)
            _fail("group delete must fail closed while it has members")
        except wf.WorkflowError as exc:
            assert "member workflow" in str(exc), str(exc)
    # ungroup the member -> delete now succeeds
    with Session(engine) as s:
        m = s.get(Workflow, member_id)
        m.group_id = None
        s.add(m)
        s.commit()
    with Session(engine) as s:
        wf.delete_group(s, gid)
        assert s.get(WorkflowGroup, gid) is None
        # the (ungrouped) member survives — it was independent, never owned
        assert s.get(Workflow, member_id) is not None
    print("  service group rename + delete-fails-closed-on-members: OK")


def test_service_prompt_template_rename_delete():
    with Session(engine) as s:
        t = wf.create_prompt_template(s, "PTA", body="Translate {text}")
        tid = t.id
        w = _mk_workflow(s, "PTWf")
        st = WorkflowStep(
            workflow_id=w.id, order_index=0, step_type=StepType.LLM_AGENT, prompt_template_id=tid
        )
        s.add(st)
        s.commit()
        step_id = st.id
    with Session(engine) as s:
        assert wf.rename_prompt_template(s, tid, "PTB").name == "PTB"
    # delete fails closed while a step pins it
    with Session(engine) as s:
        refs = wf.prompt_template_reference_count(s, tid)
        assert refs["steps"] == 1, refs
        try:
            wf.delete_prompt_template(s, tid)
            _fail("prompt-template delete must fail closed while a step pins it")
        except wf.WorkflowError as exc:
            assert "workflow step" in str(exc), str(exc)
    # detach the step -> delete now succeeds
    with Session(engine) as s:
        st = s.get(WorkflowStep, step_id)
        st.prompt_template_id = None
        s.add(st)
        s.commit()
    with Session(engine) as s:
        wf.delete_prompt_template(s, tid)
        assert s.get(PromptTemplate, tid) is None
    print("  service prompt-template rename + delete-fails-closed-on-steps: OK")


# --- CLI round-trip ----------------------------------------------------------
def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    out = buf.getvalue()
    assert out.isascii(), ("CLI output must be ASCII-only", out)
    return rc, out


def test_cli_round_trip():
    # workflow rename
    with Session(engine) as s:
        w = _mk_workflow(s, "CliWfA")
        wid = w.id
    rc, out = _run_cli(["workflow", "rename", str(wid), "--name", "CliWfB"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Workflow, wid).name == "CliWfB"
    # workflow delete preview (no --force) must NOT delete
    rc, out = _run_cli(["--json", "workflow", "delete", str(wid)])
    assert rc == 0 and _json.loads(out)["preview"] is True, out
    with Session(engine) as s:
        assert s.get(Workflow, wid) is not None
    # workflow delete --force cascades
    rc, out = _run_cli(["workflow", "delete", str(wid), "--force"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Workflow, wid) is None
    # workflow delete --force FAILS CLOSED while a routing rule references it
    with Session(engine) as s:
        w = _mk_workflow(s, "CliWfBlocked")
        wid = w.id
        s.add(RoutingRule(match_ext="po", workflow_id=wid))
        s.commit()
    rc, out = _run_cli(["workflow", "delete", str(wid), "--force"])
    assert rc != 0, ("CLI delete should fail closed while referenced", out)
    with Session(engine) as s:
        assert s.get(Workflow, wid) is not None
    # group rename + delete
    with Session(engine) as s:
        grp = wf.create_group(s, "CliGrpA")
        gid = grp.id
    rc, out = _run_cli(["workflow", "group", "rename", str(gid), "--name", "CliGrpB"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(WorkflowGroup, gid).name == "CliGrpB"
    rc, out = _run_cli(["workflow", "group", "delete", str(gid), "--force"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(WorkflowGroup, gid) is None
    # prompt list / rename / delete
    with Session(engine) as s:
        t = wf.create_prompt_template(s, "CliPtA", body="x {text}")
        tid = t.id
    rc, out = _run_cli(["--json", "workflow", "prompt", "list"])
    assert rc == 0 and any(r["id"] == tid for r in _json.loads(out)["prompt_templates"])
    rc, out = _run_cli(["workflow", "prompt", "rename", str(tid), "--name", "CliPtB"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(PromptTemplate, tid).name == "CliPtB"
    rc, out = _run_cli(["workflow", "prompt", "delete", str(tid), "--force"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(PromptTemplate, tid) is None
    # CLI rename wrote an audit entry with old->new
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "workflow.rename")).all()
        assert any(
            "'CliWfA'" in (r.detail or "") and "'CliWfB'" in (r.detail or "") for r in rows
        ), [r.detail for r in rows]
    print(
        "  CLI round-trip (workflow/group/prompt rename+delete, preview, fail-closed, "
        "ASCII, audit old->new): OK"
    )


# --- web + /api RBAC (auth ON) -----------------------------------------------
_UN = 0


def _mk_user(role):
    global _UN
    _UN += 1
    return security.create_user(f"wfrd{role}{_UN}@x.com", role.title(), role, "wfrd-pw-12345")


def _key(user):
    _row, raw = apikeys.create_key(user.id, "wfrd", scopes="api docs")
    return raw


def _login(c, who, pw):
    from tests._csrf import csrf_for

    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def test_web_rbac():
    from fastapi.testclient import TestClient

    from app.main import app

    security.seed_initial()  # admin/admin + linguist/linguist + user1/user1, auth ON
    settings.set_many({"audit_enabled": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            w = _mk_workflow(s, "WebRenameA")
            wid = w.id
            w2 = _mk_workflow(s, "WebDelLinguist")
            wid2 = w2.id
            w3 = _mk_workflow(s, "WebDelAdmin")
            wid3 = w3.id
        # linguist: rename ALLOWED (family gate = can_tune_ai)
        c = TestClient(app)
        assert _login(c, "linguist", "linguist").status_code == 303
        r = c.post(f"/workflows/{wid}/rename", data={"name": "WebRenameB"}, follow_redirects=False)
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            assert s.get(Workflow, wid).name == "WebRenameB"
        # linguist: DELETE blocked (admin-only)
        r = c.post(f"/workflows/{wid2}/delete", follow_redirects=False)
        assert r.status_code == 403, ("linguist must be blocked from delete", r.status_code)
        with Session(engine) as s:
            assert s.get(Workflow, wid2) is not None
        # admin: DELETE allowed
        ca = TestClient(app)
        assert _login(ca, "admin", "admin").status_code == 303
        r = ca.post(f"/workflows/{wid3}/delete", follow_redirects=False)
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            assert s.get(Workflow, wid3) is None
        # audit: web rename recorded old->new
        with Session(engine) as s:
            rows = s.exec(select(AuditLog).where(AuditLog.action == "workflow.rename")).all()
            assert any(
                "'WebRenameA'" in (r.detail or "") and "'WebRenameB'" in (r.detail or "")
                for r in rows
            ), [r.detail for r in rows]
        # group + prompt-template delete are admin-only too (linguist 403)
        with Session(engine) as s:
            g = wf.create_group(s, "WebGrpDel")
            gid = g.id
            t = wf.create_prompt_template(s, "WebPtDel")
            tid = t.id
        assert c.post(f"/workflow-groups/{gid}/delete", follow_redirects=False).status_code == 403
        assert c.post(f"/templates/prompts/{tid}/delete", follow_redirects=False).status_code == 403
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  web RBAC (linguist renames, non-admin delete 403, admin delete 303, audit old->new): OK"
    )


def test_api_rbac():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many(
        {"security_auth_required": "1", "audit_enabled": "1", "multi_org_enabled": "0"}
    )
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        linguist = _mk_user("linguist")
        admin = _mk_user("admin")
        lh = {"X-Loc-Token": _key(linguist)}
        ah = {"X-Loc-Token": _key(admin)}
        with Session(engine) as s:
            w = _mk_workflow(s, "ApiRenameA")
            wid = w.id
            w2 = _mk_workflow(s, "ApiDelLinguist")
            wid2 = w2.id
            w3 = _mk_workflow(s, "ApiDelAdmin")
            wid3 = w3.id
            w4 = _mk_workflow(s, "ApiDelBlocked")
            wid4 = w4.id
            s.add(RoutingRule(match_ext="xliff", workflow_id=wid4))
            s.commit()
        # rename mirrors the family gate (can_tune_ai) -> linguist OK
        r = c.post(f"/api/workflows/{wid}/rename", headers=lh, json={"name": "ApiRenameB"})
        assert r.status_code == 200 and r.json()["name"] == "ApiRenameB", r.text
        # delete is admin-only: linguist token -> 403
        r = c.post(f"/api/workflows/{wid2}/delete", headers=lh)
        assert r.status_code == 403, ("non-admin token must be blocked", r.text)
        with Session(engine) as s:
            assert s.get(Workflow, wid2) is not None
        # admin token -> 200
        r = c.post(f"/api/workflows/{wid3}/delete", headers=ah)
        assert r.status_code == 200 and r.json()["deleted"], r.text
        with Session(engine) as s:
            assert s.get(Workflow, wid3) is None
        # admin delete FAILS CLOSED (routing rule) -> 409
        r = c.post(f"/api/workflows/{wid4}/delete", headers=ah)
        assert r.status_code == 409, ("delete must fail closed while referenced", r.text)
        assert r.json()["references"]["routing_rules"] == 1, r.text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  /api RBAC (rename can_tune_ai, delete admin-only 403/200, fail-closed 409): OK")


# --- FIX 1: /api workflow-family CUSTOMER ring (adversarial review, HIGH) ------
# The /api rename/delete/prompt-template routes gated ORG only; the web routes go
# through _wf_or_404/_grp_or_404 -> _wf_customer_block, so a customer-bound caller's
# lsu_ key could mutate a FOREIGN customer's workflow/group/prompt-template over /api
# while the web session correctly 404s. The two surfaces now share ONE predicate
# (access.customer_ring_blocks): a foreign customer-scoped row is 404 (indistinguishable
# from missing); a GLOBAL (customer_id NULL) row stays visible to everyone; an UNBOUND
# caller is unaffected. Auth ON + multi_org OFF: customer binding is active independent
# of the org gate (access.current_customer_id / deps._ident_customer_id design).
def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _bound_key(email, customer_id, role=Role.LINGUIST):
    u = security.create_user(email, email, role, "wfrd-iso-pw-12345", customer_id=customer_id)
    _row, raw = apikeys.create_key(u.id, "iso", scopes="api docs")
    return raw


def _wf_name(wid):
    with Session(engine) as s:
        row = s.get(Workflow, wid)
        return row.name if row else None


def test_api_customer_isolation():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many(
        {"security_auth_required": "1", "audit_enabled": "1", "multi_org_enabled": "0"}
    )
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        cust_a = _customer("IsoCustA")
        cust_b = _customer("IsoCustB")
        # A bound-A linguist (customer-bindable AND passes the can_tune_ai rename gate,
        # so these probes exercise the CUSTOMER ring, not the role ladder) + an UNBOUND
        # admin (org-wide visibility must be unchanged for it).
        a_key = {"X-Loc-Token": _bound_key("iso-a@x.test", cust_a)}
        admin = _mk_user("admin")
        admin_h = {"X-Loc-Token": _key(admin)}

        # --- foreign customer-B resources (must be UNREACHABLE to bound-A) ---
        with Session(engine) as s:
            wf_b = _mk_workflow(s, "IsoWfB", customer_id=cust_b)
            wid_b = wf_b.id
            wf_b_del = _mk_workflow(s, "IsoWfBDel", customer_id=cust_b)
            wid_b_del = wf_b_del.id
            g_b = WorkflowGroup(name="IsoGrpB", customer_id=cust_b)
            s.add(g_b)
            s.commit()
            s.refresh(g_b)
            gid_b = g_b.id
            t_b = PromptTemplate(
                name="IsoPtB", scope=Scope.CUSTOMER, customer_id=cust_b, body="x {text}"
            )
            s.add(t_b)
            s.commit()
            s.refresh(t_b)
            tid_b = t_b.id

        # workflow rename -> 404 + unmutated
        r = c.post(f"/api/workflows/{wid_b}/rename", headers=a_key, json={"name": "HIJACKED"})
        assert r.status_code == 404, (
            f"bound-A renaming customer-B workflow must 404, got {r.status_code}"
        )
        assert _wf_name(wid_b) == "IsoWfB", "customer-B workflow was mutated"
        # workflow DELETE -> 404 (the ring resolves BEFORE the admin gate, so a bound
        # caller sees 404, never a 403 that would leak the row's existence) + survives
        r = c.post(f"/api/workflows/{wid_b_del}/delete", headers=a_key)
        assert r.status_code == 404, (
            f"bound-A deleting customer-B workflow must 404, got {r.status_code}"
        )
        with Session(engine) as s:
            assert s.get(Workflow, wid_b_del) is not None, "customer-B workflow deleted"
        # group rename -> 404 + unmutated
        r = c.post(f"/api/workflow-groups/{gid_b}/rename", headers=a_key, json={"name": "HJ-G"})
        assert r.status_code == 404, (
            f"bound-A renaming customer-B group must 404, got {r.status_code}"
        )
        with Session(engine) as s:
            assert s.get(WorkflowGroup, gid_b).name == "IsoGrpB", "customer-B group mutated"
        # prompt-template rename -> 404 + unmutated
        r = c.post(f"/api/prompt-templates/{tid_b}/rename", headers=a_key, json={"name": "HJ-T"})
        assert r.status_code == 404, (
            f"bound-A renaming customer-B prompt template must 404, got {r.status_code}"
        )
        with Session(engine) as s:
            assert s.get(PromptTemplate, tid_b).name == "IsoPtB", "customer-B template mutated"

        # --- POSITIVE: own-customer + GLOBAL rows stay reachable; unbound unaffected ---
        with Session(engine) as s:
            wf_a = _mk_workflow(s, "IsoWfA", customer_id=cust_a)  # bound-A's OWN
            wid_a = wf_a.id
            wf_g = _mk_workflow(s, "IsoWfGlobal")  # GLOBAL (customer NULL)
            wid_g = wf_g.id
        # bound-A renames its OWN customer's workflow -> 200
        r = c.post(f"/api/workflows/{wid_a}/rename", headers=a_key, json={"name": "IsoWfA2"})
        assert r.status_code == 200 and _wf_name(wid_a) == "IsoWfA2", r.text
        # bound-A can READ a GLOBAL workflow (shared, customer_id NULL) but, #468, may not
        # change it: renaming it is a 403 and the name is unchanged
        r = c.get(f"/api/workflows/{wid_g}", headers=a_key)
        assert r.status_code == 200, r.text
        r = c.post(f"/api/workflows/{wid_g}/rename", headers=a_key, json={"name": "IsoWfGlobal2"})
        assert r.status_code == 403 and _wf_name(wid_g) == "IsoWfGlobal", r.text
        # an UNBOUND admin still reaches the customer-B workflow (org-wide visibility
        # for internal staff is UNCHANGED — the ring only confines BOUND callers)
        r = c.post(f"/api/workflows/{wid_b}/rename", headers=admin_h, json={"name": "IsoWfBStaff"})
        assert r.status_code == 200 and _wf_name(wid_b) == "IsoWfBStaff", (
            "unbound staff visibility must be unchanged",
            r.text,
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  /api customer ring (foreign wf/group/pt rename + wf delete -> 404 "
        "unmutated; own reachable, global read-only (#468); unbound staff unchanged): OK"
    )


# --- FIX 2: non-ASCII CLI output is console-safe (MEDIUM) -----------------------
# Originally the human echo (emit.result -> say -> sys.stdout.write) crashed with a raw
# UnicodeEncodeError on a cp1252 console (nonzero exit + traceback AFTER the DB change).
# #15 fixed this AT THE ROOT: the CLI reconfigures stdout to UTF-8 at startup, so user
# DATA (a workflow name with non-ASCII, incl. a non-cp1252 arrow) renders as REAL UTF-8
# rather than crashing or being backslash-escaped. The rename still commits the true
# unicode to the DB / audit; the echo now carries it faithfully.
def test_cli_unicode_output_cp1252():
    unicode_name = "Wörkflöw -> ünïcode → done"  # → is NOT cp1252-encodable
    with Session(engine) as s:
        w = _mk_workflow(s, "UniStart")
        wid = w.id
    # A cp1252 stream stands in for a legacy console; the startup reconfigure
    # (_force_utf8_streams -> TextIOWrapper.reconfigure) flips it to UTF-8 so the write
    # never meets an unencodable char.
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli_main(["workflow", "rename", str(wid), "--name", unicode_name])
    finally:
        sys.stdout = old
    buf.flush()
    raw = buf.buffer.getvalue()
    assert rc == 0, f"unicode rename must exit 0 on a legacy console (got {rc})"
    # The non-cp1252 arrow is emitted as REAL UTF-8 (not escaped, not a lone byte).
    assert "→".encode() in raw, f"expected real UTF-8 bytes for the data char, got {raw[-120:]!r}"
    out = raw.decode("utf-8")
    assert unicode_name in out, "the echo must carry the true unicode name (real UTF-8)"
    # the DB carries the TRUE unicode name (echo shaping never touched the write)
    assert _wf_name(wid) == unicode_name, "DB must hold the real unicode name"
    print(
        "  CLI unicode output (startup UTF-8 reconfigure: exit 0, real UTF-8 echo, "
        "DB true unicode): OK"
    )


def main():
    init_db()
    test_service_rename_and_propagation()
    test_service_delete_fails_closed()
    test_service_delete_cascade_exact()
    test_service_delete_reaps_own_batch()
    test_service_delete_reaps_batch_with_variant_member()
    test_service_delete_repro_finished_batch_mixed_members()
    test_service_delete_blocks_active_variant_member()
    test_service_delete_blocks_live_zero_member_batch()
    test_service_delete_blocks_memory_suggestion()
    test_service_delete_unscopes_eval_candidate()
    test_service_delete_unscopes_memory_event()
    test_service_delete_blocks_vendor_dispatch()
    test_workflow_delete_block_reason_covers_all_guards()
    test_service_group_rename_delete()
    test_service_prompt_template_rename_delete()
    test_cli_round_trip()
    test_web_rbac()
    test_api_rbac()
    test_api_customer_isolation()
    test_cli_unicode_output_cp1252()
    print("WORKFLOW RENAME/DELETE OK")


if __name__ == "__main__":
    main()
