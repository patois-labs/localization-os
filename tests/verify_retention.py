"""Data-retention purge + GDPR Art.17-reaching deletes (offline).

  - purge deletes uploads / Document+Segment rows / TERMINAL Job rows older than
    retention_days
  - purge clears project TM (SegmentTM + FullTextTM) ONLY for a project it empties
    completely (all docs aged out) — NEVER per-document: a project with one aged +
    one fresh doc keeps its TM (the fresh doc still depends on it)
  - recent rows (inside the window) survive
  - kill switch: retention_enabled=0 -> purge is a total no-op
  - retention_days=0 short-circuits even when enabled (no window != delete all)
  - QUEUED / RUNNING jobs are NEVER purged (only terminal states)
  - delete_document(cascade_tm=True) reaches TM; delete_project_tm removes both stores
  - #276: TM + LLMemory rows age out ROW BY ROW (a live project's aged
    SegmentTM/FullTextTM/embedding rows, and aged MemoryEvent/MemoryEntry/
    MemorySuggestion rows quoting them, are deleted even while the project has a
    fresh doc); document deletes DELETE the document's MemoryEvents (they carry
    the before/after text) instead of un-scoping them
  - #399 (finding dbdata-07): a FAILED blob delete during a document erase keeps its
    retry identity: the key is persisted in BlobDeleteOutbox in the same transaction,
    purge retries it every sweep and reports it pending until a delete succeeds;
    a MISSING blob never enters the ledger

All rows are backdated in a throwaway data dir; uploads are real files on disk.
"""

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_retention_")

from sqlmodel import Session, select

from app import config, ports
from app.db import engine, init_db
from app.models import (
    BlobDeleteOutbox,
    Customer,
    DeliveryEmptyApproval,
    Document,
    FullTextTM,
    Glossary,
    Job,
    JobStatus,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemoryEvidence,
    MemorySuggestion,
    MemorySuggestionEvidence,
    Project,
    QueryPost,
    QueryThread,
    RunStatus,
    Segment,
    SegmentEmbedding,
    SegmentPostEdit,
    SegmentTM,
    StepRun,
    TermCandidate,
    Vendor,
    VendorDispatch,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.ports import blobstore
from app.services import retention, settings


def _touch_upload(doc_id: int, fmt: str, data: bytes = b"hello") -> None:
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (config.UPLOADS_DIR / f"{doc_id}.{fmt}").write_bytes(data)


def _backdate(model_id, model, days_ago: int) -> None:
    when = datetime.utcnow() - timedelta(days=days_ago)
    with Session(engine) as s:
        row = s.get(model, model_id)
        row.created_at = when
        if model in (SegmentTM, FullTextTM):
            row.activity_at = when
        s.add(row)
        s.commit()


def _seed_doc(project_id: int, fmt: str, days_ago: int, *, with_tm=True) -> int:
    """A document (+ two segments + one SegmentTM + one FullTextTM in its project)
    with an on-disk upload, all backdated `days_ago`."""
    with Session(engine) as s:
        doc = Document(project_id=project_id, filename=f"f.{fmt}", fmt=fmt)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(
            Segment(
                document_id=did,
                target_locale="fr-FR",
                order_index=0,
                source_text="a",
                target_text="[fr] a",
            )
        )
        s.add(
            Segment(
                document_id=did,
                target_locale="fr-FR",
                order_index=1,
                source_text="b",
                target_text="[fr] b",
            )
        )
        if with_tm:
            s.add(
                SegmentTM(
                    project_id=project_id,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    source_text="a",
                    target_text="[fr] a",
                    source_hash="h1",
                    created_at=datetime.utcnow() - timedelta(days=days_ago),
                )
            )
            s.add(
                FullTextTM(
                    project_id=project_id,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    source_hash="h2",
                    created_at=datetime.utcnow() - timedelta(days=days_ago),
                    source_text="a",
                    target_text="[fr] a",
                )
            )
        s.commit()
    _touch_upload(did, fmt)
    _backdate(did, Document, days_ago)
    return did


def _seed_job(status: str, days_ago: int) -> int:
    with Session(engine) as s:
        j = Job(kind="translate_document", status=status, created_by="x@example.com")
        s.add(j)
        s.commit()
        s.refresh(j)
        jid = j.id
    _backdate(jid, Job, days_ago)
    return jid


def _seed_run(
    doc_id: int,
    project_id: int,
    *,
    batched: bool = False,
    scoped: bool = False,
    status: str = RunStatus.COMPLETED,
) -> dict:
    """Give a document the run history a TRANSLATED document really has: a Workflow +
    WorkflowStep, a WorkflowRun anchored to the document (`WorkflowRun.document_id` is the
    NON-NULL FK that used to make the whole purge raise), one StepRun trace, and optionally
    the WorkflowBatch fan-out row (whose `document_id` is ALSO a NON-NULL FK to document).
    Returns the ids so a test can assert they are gone afterwards.

    `scoped=True` makes the Workflow PROJECT-SCOPED (`Workflow.project_id`), which is what
    puts it in `content.delete_project`'s `wf_ids` and therefore makes step 3c try to delete
    it. That matters for the batch test: `workflowbatch.workflow_id` FKs the workflow, so a
    batch only blocks step 3c when the workflow is project-scoped. With an unscoped (global)
    workflow the batch is reaped harmlessly by the per-document pass at step 4 and step 3b is
    never load-bearing — a fixture that forgets this proves nothing."""
    with Session(engine) as s:
        wf = Workflow(name=f"WF for doc {doc_id}", project_id=project_id if scoped else None)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step = WorkflowStep(workflow_id=wf.id, order_index=0, name="MT")
        s.add(step)
        s.commit()
        s.refresh(step)
        batch_id = None
        if batched:
            batch = WorkflowBatch(
                workflow_id=wf.id, document_id=doc_id, requested_locales=["fr-FR"]
            )
            s.add(batch)
            s.commit()
            s.refresh(batch)
            batch_id = batch.id
        # A FINISHED run: retention never ages out a document with a live run (#543).
        run = WorkflowRun(
            workflow_id=wf.id,
            document_id=doc_id,
            target_locale="fr-FR",
            batch_id=batch_id,
            status=status,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        sr = StepRun(workflow_run_id=run.id, step_id=step.id)
        s.add(sr)
        s.commit()
        s.refresh(sr)
        return {
            "workflow_id": wf.id,
            "step_id": step.id,
            "run_id": run.id,
            "step_run_id": sr.id,
            "batch_id": batch_id,
        }


def _counts():
    with Session(engine) as s:
        return {
            "docs": len(s.exec(select(Document.id)).all()),
            "segs": len(s.exec(select(Segment.id)).all()),
            "jobs": len(s.exec(select(Job.id)).all()),
            "seg_tm": len(s.exec(select(SegmentTM.id)).all()),
            "ft_tm": len(s.exec(select(FullTextTM.id)).all()),
        }


def _review_regressions() -> None:
    """P1-P5 from the blind review, all in init_env's throwaway database."""
    from pathlib import Path
    from unittest.mock import patch

    from sqlalchemy import insert

    from app.services import tm

    failures = []

    def check(ok, label):
        print(f"  {label}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append(label)

    old = datetime.utcnow() - timedelta(days=400)
    with Session(engine) as s:
        p = Project(name="Review live")
        keys = Project(name="Review keys", mode="keys")
        cust = Customer(name="Review customer")
        wf = Workflow(name="Review workflow")
        s.add_all([p, keys, cust, wf])
        s.commit()
        pid, kpid, cid, wid = p.id, keys.id, cust.id, wf.id
    live = _seed_doc(pid, "txt", 1, with_tm=False)
    key_doc = _seed_doc(kpid, "keys", 400, with_tm=False)
    with Session(engine) as s:
        s.add(
            Segment(
                document_id=key_doc,
                target_locale="fr-FR",
                order_index=2,
                source_text="catalogue",
                target_text="catalogue",
            )
        )
        s.commit()
    with Session(engine) as s:
        approved = tm.write_back(s, pid, "approved", "old", "en-US", "fr-FR")
        aid = approved.id
        full = tm.ft_store(s, pid, "full approval", "old", "en-US", "fr-FR")
        fid = full.id
        imported = tm.write_back(s, pid, "imported", "old", "en-US", "fr-FR")
        iid = imported.id
    _backdate(aid, SegmentTM, 400)
    _backdate(fid, FullTextTM, 400)
    _backdate(iid, SegmentTM, 400)
    with Session(engine) as s:
        tm.write_back(s, pid, "approved", "approved today", "en-US", "fr-FR")
        tm.ft_store(s, pid, "full approval", "approved today", "en-US", "fr-FR")
        tm.import_tmx(
            s,
            pid,
            b'<tmx version="1.4"><body><tu>'
            b'<tuv xml:lang="en-US"><seg>imported</seg></tuv>'
            b'<tuv xml:lang="fr-FR"><seg>updated today</seg></tuv>'
            b"</tu></body></tmx>",
            "en-US",
            "fr-FR",
        )
        for project, source, used in [
            (pid, "a", None),
            (pid, "reused", datetime.utcnow()),
            (kpid, "catalogue", None),
        ]:
            s.add(
                SegmentTM(
                    project_id=project,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    source_text=source,
                    target_text="live",
                    source_hash=source,
                    created_at=old,
                    activity_at=used,
                )
            )
        s.commit()
        protected = s.exec(select(SegmentTM.id).where(SegmentTM.project_id.in_([pid, kpid]))).all()
        for tid in protected:
            s.add(
                SegmentEmbedding(
                    segment_tm_id=tid,
                    project_id=pid,
                    model="mock",
                    dim=1,
                    embedding=b"1234",
                    created_at=old,
                )
            )
        s.commit()
        # 300k reproduces the actual SQLite bind-variable failure, not just a toy batch.
        settings.set_many({"retention_enabled": "1", "retention_days": "30"})
        retention.purge()
        with Session(engine) as probe:
            check(
                probe.get(SegmentTM, aid) is not None
                and probe.get(FullTextTM, fid) is not None
                and probe.get(SegmentTM, iid) is not None,
                "P1 approval/full-text/TMX update arms refresh retention age",
            )
            check(
                all(probe.get(SegmentTM, tid) is not None for tid in protected),
                "P1/P2 old live/reused/catalogue TM survives before bulk insert",
            )
        for start in range(0, 300000, 1000):
            s.execute(
                insert(SegmentTM),
                [
                    {
                        "project_id": pid,
                        "source_locale": "en-US",
                        "target_locale": "fr-FR",
                        "source_text": f"bulk-{i}",
                        "target_text": "aged",
                        "source_hash": f"bulk-{i}",
                        "created_at": old,
                    }
                    for i in range(start, start + 1000)
                ],
            )
        s.commit()

    def memories(document_id, aged=False):
        with Session(engine) as s:
            event = MemoryEvent(
                customer_id=cid,
                document_id=document_id,
                before_text="SECRET",
                created_at=old if aged else datetime.utcnow(),
            )
            entry = MemoryEntry(
                customer_id=cid,
                source_term="SECRET",
                body="SECRET",
                status="active",
                evidence_count=1,
            )
            suggestion = MemorySuggestion(
                customer_id=cid, workflow_id=wid, kind="enable_recall", rationale="because SECRET"
            )
            s.add_all([event, entry, suggestion])
            s.commit()
            s.add(MemoryEvidence(memory_id=entry.id, event_id=event.id))
            s.add(MemorySuggestionEvidence(suggestion_id=suggestion.id, event_id=event.id))
            s.commit()
            return event.id, entry.id, suggestion.id

    art = memories(live)
    aged = memories(None, aged=True)
    with Session(engine) as s:
        independent = MemoryEvent(customer_id=cid, before_text="independent")
        mixed = MemoryEntry(
            customer_id=cid, source_term="supported", status="active", evidence_count=2
        )
        s.add_all([independent, mixed])
        s.commit()
        mixed_id, independent_id = mixed.id, independent.id
        s.add_all(
            [
                MemoryEvidence(memory_id=mixed.id, event_id=art[0]),
                MemoryEvidence(memory_id=mixed.id, event_id=independent.id),
            ]
        )
        s.commit()
    # A shared erasure failure must roll back the complete document transaction.
    real_erase = retention._erase_memory_events

    def erase_then_fail(session, ids):
        real_erase(session, ids)
        raise RuntimeError("injected erasure failure")

    with patch.object(retention, "_erase_memory_events", side_effect=erase_then_fail):
        try:
            with Session(engine) as s:
                retention.delete_document(s, live, cascade_tm=False)
        except RuntimeError:
            pass
    with Session(engine) as s:
        check(
            s.get(Document, live) is not None
            and s.get(MemoryEvent, art[0]) is not None
            and s.get(MemoryEntry, art[1]).source_term == "SECRET",
            "P4 shared erasure is atomic",
        )
    with Session(engine) as s:
        retention.delete_document(s, live, cascade_tm=False)
    # Replace the removed live document so its surviving matching segment protects TM.
    _seed_doc(pid, "txt", 1, with_tm=False)
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    result = retention.purge()
    with Session(engine) as s:
        check(
            s.get(SegmentTM, aid) is not None and s.get(FullTextTM, fid) is not None,
            "P1 re-approved segment and full-text TM survive",
        )
        check(
            all(s.get(SegmentTM, tid) is not None for tid in protected),
            "P1/P2 live reused and key catalogue TM survive",
        )
        check(
            len(s.exec(select(SegmentEmbedding)).all()) >= len(protected),
            "P1/P2 live embeddings survive",
        )
        check(
            not s.exec(select(SegmentTM.id).where(SegmentTM.source_hash.like("bulk-%"))).first()
            and result["tm_aged"] >= 300000,
            "P3 300k purge completes",
        )
        for label, ids in [("P4 Art17", art), ("P5 aged event with fresh belief", aged)]:
            event, entry, suggestion = (
                s.get(model, id_)
                for model, id_ in zip(
                    (MemoryEvent, MemoryEntry, MemorySuggestion), ids, strict=True
                )
            )
            if label.startswith("P4"):
                # Document delete with the LLMemory add-on ABSENT (database upgraded from the
                # full edition): the event, and the entry and suggestion learned ONLY from it,
                # are erased in plain SQL.
                check(event is None and entry is None and suggestion is None, label)
            else:
                # Aging: the event is deleted with its joins; a current belief and a current
                # suggestion are the add-on's to tombstone, so they stay.
                check(event is None and entry is not None and suggestion is not None, label)
        check(
            s.get(MemoryEntry, mixed_id).status == "active"
            and s.get(MemoryEntry, mixed_id).evidence_count == 1
            and s.get(MemoryEvent, independent_id) is not None,
            "P4 independent evidence survives with corrected count",
        )
    with Session(engine) as s:
        s.add(
            SegmentTM(
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="retry",
                target_text="retry",
                source_hash="retry",
                created_at=old,
            )
        )
        s.commit()

    def age_then_fail(session, cutoff):
        # Fail before the first batch commit; a later failure preserves prior progress.
        raise RuntimeError("injected post-delete failure")

    with patch.object(retention, "_age_out_tm_and_memory_rows", side_effect=age_then_fail):
        failed = retention.purge()
    check(failed.get("row_aging_failed") == 1 and failed["tm_aged"] == 0, "P3 failure is reported")
    check(
        retention.purge()["tm_aged"] == 1 and not any(retention.purge().values()),
        "P3 rolled-back backlog drains on retry and is idempotent",
    )
    card = (Path(_ROOT) / "app/templates/settings.html").read_text(encoding="utf-8")
    check(
        "including imported reference TM" in card and "catalogues and their TM are exempt" in card,
        "settings describes destructive scope and exemptions",
    )
    assert not failures, failures


def main() -> None:
    init_db()
    settings.set_many({"audit_enabled": "1"})

    with Session(engine) as s:
        proj = Project(name="Retention P", source_locale="en-US", created_by="x@example.com")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        pid = proj.id

    # =====================================================================
    # (1) kill switch OFF -> total no-op even with aged rows
    # =====================================================================
    old_doc = _seed_doc(pid, "txt", days_ago=100)
    old_job = _seed_job(JobStatus.COMPLETED, days_ago=100)
    settings.set_many({"retention_enabled": "0", "retention_days": "30"})
    res = retention.purge()
    # EXACT dict (not a subset): the disabled sweep must report a fully zeroed result, and a
    # new counter must be added here deliberately. `documents_skipped` joined the contract with
    # the 2026-08-31 run-history fix — purge now isolates a failing document instead of letting
    # it abort the whole sweep, and it REPORTS how many it skipped (a purge that silently
    # deleted nothing is precisely how the WorkflowRun FK bug stayed invisible).
    assert res == {
        "documents": 0,
        "segments": 0,
        "tm": 0,
        "jobs": 0,
        "jobqueue_rows": 0,
        "files": 0,
        "key_image_blobs": 0,
        "documents_skipped": 0,
        # The advanced_* counters joined the contract 2026-09-05 (H3 + round-1 H1/M1): a document
        # erasure now reaps the Advanced tier's run-parented findings AND severs its staged text /
        # transcript content / mission-roster previews for the aged documents, and REPORTS each
        # count (the same "a purge that reaped nothing looked like nothing to do" reasoning that
        # put documents_skipped on the dict). A NEW counter must be added here DELIBERATELY.
        "advanced_findings_reaped": 0,
        "advanced_staged_erased": 0,
        "advanced_transcript_rows_erased": 0,
        "advanced_roster_previews_erased": 0,
        # round-3 M2 (2026-09-05): scripted roster units with a non-integer segment_id/key_id are
        # SKIPPED and counted (never raised, so the sweep no longer blocks the whole document).
        "advanced_units_unparseable": 0,
        # #276 (2026-09-28): per-document MemoryEvent deletions + the row-level TM/LLMemory
        # aging counters (see section 9) joined the contract deliberately.
        "memory_events_deleted": 0,
        "lab_datasets_erased": 0,
        "tm_aged": 0,
        "row_aging_failed": 0,
        "memory_events_aged": 0,
        "memory_entries_aged": 0,
        "memory_suggestions_aged": 0,
        "memory_evidence_aged": 0,
        # #399 (2026-10-04, finding dbdata-07): the blob-delete outbox drain joined the
        # contract deliberately (see section 10) -- retried-resolved vs still-pending failed
        # blob deletes, reported every sweep until the storage gives the blob up.
        "blob_deletes_retried": 0,
        "blob_deletes_pending": 0,
    }, res
    assert _counts()["docs"] == 1 and _counts()["jobs"] == 1, "purge ran while disabled"
    print("  kill switch: retention_enabled=0 -> purge is a total no-op")

    # =====================================================================
    # (2) enabled but retention_days=0 -> short-circuit (no window != delete all)
    # =====================================================================
    settings.set_many({"retention_enabled": "1", "retention_days": "0"})
    res = retention.purge()
    assert res["documents"] == 0 and res["jobs"] == 0, res
    assert _counts()["docs"] == 1 and _counts()["jobs"] == 1, "days=0 deleted rows"
    print("  window: retention_days=0 short-circuits (no window, nothing deleted)")

    # =====================================================================
    # (3) enabled + window: the old doc's project fully ages out (it is that
    #     project's only doc), so its docs+segments+upload go AND the now-empty
    #     project's TM is cleared. The recent doc lives in a DIFFERENT project and
    #     survives untouched.
    # =====================================================================
    upload_path = config.UPLOADS_DIR / f"{old_doc}.txt"
    assert upload_path.exists(), "seed upload missing"

    with Session(engine) as s:
        proj2 = Project(name="Recent P", source_locale="en-US", created_by="x@example.com")
        s.add(proj2)
        s.commit()
        s.refresh(proj2)
        pid2 = proj2.id
    recent_doc = _seed_doc(pid2, "md", days_ago=1)  # inside a 30-day window
    recent_job = _seed_job(JobStatus.COMPLETED, days_ago=1)

    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    res = retention.purge()
    assert res["documents"] == 1, ("expected 1 old doc purged", res)
    assert res["segments"] == 2, ("expected 2 old segments purged", res)
    # 'Retention P' has no docs left after the sweep, so its TM (2 rows) is cleared.
    assert res["tm_aged"] == 2, ("empty project's aged TM should be cleared", res)
    assert res["jobs"] == 1, ("expected 1 old terminal job purged", res)
    assert res["files"] == 1, ("expected 1 upload file removed", res)

    with Session(engine) as s:
        assert s.get(Document, old_doc) is None, "old document survived"
        assert s.get(Document, recent_doc) is not None, "recent document was purged"
        assert s.get(Job, old_job) is None, "old job survived"
        assert s.get(Job, recent_job) is not None, "recent job was purged"
        # the emptied project's TM is gone; the recent project's TM is untouched
        assert len(s.exec(select(SegmentTM.id).where(SegmentTM.project_id == pid)).all()) == 0
        assert len(s.exec(select(SegmentTM.id).where(SegmentTM.project_id == pid2)).all()) == 1, (
            "recent project's TM must survive"
        )
    assert not upload_path.exists(), "old upload file survived on disk"
    # the recent doc's upload + TM survive
    assert (config.UPLOADS_DIR / f"{recent_doc}.md").exists(), "recent upload removed"
    print("  purge: emptied project's doc+segments+TM+upload gone; other project survives")

    # =====================================================================
    # (3b) shared project, one aged + one fresh doc: the aged doc is purged but the
    #      project's TM SURVIVES (the fresh sibling still depends on it). This is
    #      the data-loss guard — a per-document TM cascade would wrongly wipe it.
    # =====================================================================
    with Session(engine) as s:
        shared = Project(name="Shared inbox", source_locale="en-US", created_by="x@example.com")
        s.add(shared)
        s.commit()
        s.refresh(shared)
        spid = shared.id
    aged_shared = _seed_doc(spid, "txt", days_ago=100, with_tm=True)  # carries the project's TM
    fresh_shared = _seed_doc(spid, "txt", days_ago=1, with_tm=False)  # inside the window
    before_tm = _counts()["seg_tm"]
    res = retention.purge()
    assert res["documents"] == 1, ("only the aged shared doc should purge", res)
    assert res["tm"] == 0, ("project TM must SURVIVE while a fresh doc remains", res)
    with Session(engine) as s:
        assert s.get(Document, aged_shared) is None, "aged shared doc survived"
        assert s.get(Document, fresh_shared) is not None, "fresh shared doc was wrongly purged"
        surviving = len(s.exec(select(SegmentTM.id).where(SegmentTM.project_id == spid)).all())
        assert surviving == 1, ("shared project's TM must survive the sibling purge", surviving)
    assert _counts()["seg_tm"] == before_tm, "TM row count changed (must not)"
    print("  purge: aged doc purged, shared project TM SURVIVES (fresh sibling remains)")

    # =====================================================================
    # (4) QUEUED / RUNNING jobs are NEVER purged, even when aged
    # =====================================================================
    q_job = _seed_job(JobStatus.QUEUED, days_ago=100)
    r_job = _seed_job(JobStatus.RUNNING, days_ago=100)
    old_done = _seed_job(JobStatus.FAILED, days_ago=100)
    res = retention.purge()
    assert res["jobs"] == 1, ("only the terminal FAILED job should purge", res)
    with Session(engine) as s:
        assert s.get(Job, q_job) is not None, "QUEUED job was purged (must never happen)"
        assert s.get(Job, r_job) is not None, "RUNNING job was purged (must never happen)"
        assert s.get(Job, old_done) is None, "aged terminal job survived"
    print("  purge: QUEUED + RUNNING jobs never deleted; terminal ones are")

    # =====================================================================
    # (5) delete_document reaches TM; delete_project_tm removes both stores
    # =====================================================================
    d2 = _seed_doc(pid, "txt", days_ago=1)
    with Session(engine) as s:
        counts = retention.delete_document(s, d2, cascade_tm=True)
    assert counts["documents"] == 1 and counts["segments"] == 2, counts
    assert counts["tm"] >= 1, ("delete_document must reach TM", counts)
    with Session(engine) as s:
        assert s.get(Document, d2) is None
    print("  delete_document: cascades to segments + project TM")

    # seed fresh TM (both stores) + a source-derived embedding, then
    # delete_project_tm clears ALL THREE — an orphan SegmentEmbedding would leave
    # a source vector behind after an Art. 17 erasure (GDPR finding).
    with Session(engine) as s:
        seg_tm = SegmentTM(
            project_id=pid,
            source_locale="en-US",
            target_locale="de-DE",
            source_text="x",
            target_text="[de] x",
            source_hash="h3",
        )
        s.add(seg_tm)
        s.add(
            FullTextTM(
                project_id=pid,
                source_locale="en-US",
                target_locale="de-DE",
                source_hash="h4",
                source_text="x",
                target_text="[de] x",
            )
        )
        s.commit()
        s.refresh(seg_tm)
        s.add(
            SegmentEmbedding(
                segment_tm_id=seg_tm.id,
                project_id=pid,
                source_locale="en-US",
                target_locale="de-DE",
                model="mock",
                dim=1,
                embedding=b"\x00\x00\x80?",
            )
        )
        s.commit()
    with Session(engine) as s:
        n = retention.delete_project_tm(s, pid)
    assert n >= 3, ("delete_project_tm must remove SegmentTM+FullTextTM+SegmentEmbedding", n)
    with Session(engine) as s:
        seg_left = len(s.exec(select(SegmentTM.id).where(SegmentTM.project_id == pid)).all())
        ft_left = len(s.exec(select(FullTextTM.id).where(FullTextTM.project_id == pid)).all())
        emb_left = len(
            s.exec(select(SegmentEmbedding.id).where(SegmentEmbedding.project_id == pid)).all()
        )
    assert seg_left == 0 and ft_left == 0, ("project TM not fully cleared", seg_left, ft_left)
    assert emb_left == 0, ("SegmentEmbedding survived delete_project_tm (orphan vector)", emb_left)
    print("  delete_project_tm: removes SegmentTM + FullTextTM + SegmentEmbedding for the project")

    # =====================================================================
    # (6) per-department TM isolation (Phase B chunk 3b): TM is project-scoped,
    #     and each department gets its OWN inbox project, so two departments'
    #     inbox TMs never cross-pollinate. Prove tm.lookup honours the boundary.
    # =====================================================================
    from app.services import routing, tm

    with Session(engine) as s:
        inbox_a = routing.ensure_department_inbox(s, "sales", "actor-a@example.com")
        inbox_b = routing.ensure_department_inbox(s, "legal", "actor-b@example.com")
        assert inbox_a.id != inbox_b.id, "distinct departments must get distinct inbox projects"
        # Seed the SAME source string in each department's inbox TM with a
        # DIFFERENT target — a leak would let one department read the other's.
        tm.write_back(
            s, inbox_a.id, "Confidential deal terms", "[SALES] deal terms", "en-US", "fr-FR"
        )
        tm.write_back(
            s, inbox_b.id, "Confidential deal terms", "[LEGAL] deal terms", "en-US", "fr-FR"
        )
        hit_a = tm.lookup(s, inbox_a.id, "Confidential deal terms", "en-US", "fr-FR")
        hit_b = tm.lookup(s, inbox_b.id, "Confidential deal terms", "en-US", "fr-FR")
    assert hit_a and hit_a["target_text"] == "[SALES] deal terms", hit_a
    assert hit_b and hit_b["target_text"] == "[LEGAL] deal terms", hit_b
    assert hit_a["target_text"] != hit_b["target_text"], (
        "department inbox TMs cross-pollinated (project scoping broken)"
    )
    print("  TM isolation: two departments' inbox TMs do not cross-pollinate")

    # =====================================================================
    # (7) THE RUN-HISTORY FK BUG (fix-train 2026-08-31).
    #
    #     `WorkflowRun.document_id` is a NON-NULL FK to document with no cascade, and
    #     delete_document did not delete run rows -- so with SQLite FK enforcement ON
    #     (app/db.py PRAGMA foreign_keys=ON, which this test DB inherits) the
    #     `DELETE FROM document` raised IntegrityError for ANY document that had ever been
    #     translated. Net effect in production: retention aged out untranslated uploads and
    #     NOTHING ELSE, silently, because the raise escaped an unguarded per-document loop
    #     and the whole sweep (jobs + key-image blobs included) died with it.
    #
    #     These tests assert the document row is ACTUALLY GONE, never just "no exception" --
    #     a swallowing per-document guard must not be able to fake a pass.
    # =====================================================================
    # (7a) an aged document WITH a run + step trace + batch purges cleanly
    with Session(engine) as s:
        rp = Project(name="Run project", source_locale="en-US", created_by="x@example.com")
        s.add(rp)
        s.commit()
        s.refresh(rp)
        rpid = rp.id
    run_doc = _seed_doc(rpid, "txt", days_ago=100, with_tm=False)
    ids = _seed_run(run_doc, rpid, batched=True)
    res = retention.purge()
    assert res["documents"] == 1, ("the aged RUN-BEARING doc must purge", res)
    assert res["documents_skipped"] == 0, ("nothing may be skipped/swallowed", res)
    with Session(engine) as s:
        assert s.get(Document, run_doc) is None, "the run-bearing document survived the purge"
        assert s.exec(select(Segment.id).where(Segment.document_id == run_doc)).all() == []
        assert s.get(StepRun, ids["step_run_id"]) is None, "StepRun trace survived"
        assert s.get(WorkflowRun, ids["run_id"]) is None, "WorkflowRun survived"
        assert s.get(WorkflowBatch, ids["batch_id"]) is None, "WorkflowBatch survived"
        assert s.get(Workflow, ids["workflow_id"]) is not None, (
            "the WORKFLOW itself is not the document's to delete -- it is reusable config"
        )
    print("  purge: an aged doc WITH a workflow run + step trace + batch is fully reaped")

    # (7a2) #543: an aged document WITH an in-flight run is SKIPPED (not purged, not counted as
    #       skipped-by-error), and is purged on the next sweep once the run has finished.
    for live_status in (RunStatus.RUNNING, RunStatus.PAUSED):
        live_doc = _seed_doc(rpid, "txt", days_ago=100, with_tm=False)
        live_ids = _seed_run(live_doc, rpid, status=live_status)
        res = retention.purge()
        assert res["documents"] == 0 and res["documents_skipped"] == 0, (live_status, res)
        with Session(engine) as s:
            assert s.get(Document, live_doc) is not None, f"a {live_status} run's document purged"
            assert s.get(WorkflowRun, live_ids["run_id"]) is not None
            live_run = s.get(WorkflowRun, live_ids["run_id"])
            live_run.status = RunStatus.COMPLETED
            s.add(live_run)
            s.commit()
        res = retention.purge()
        assert res["documents"] == 1, ("purged once the run finished", live_status, res)
        with Session(engine) as s:
            assert s.get(Document, live_doc) is None
            assert s.get(WorkflowRun, live_ids["run_id"]) is None
    print("  purge: an aged doc with a RUNNING/PAUSED run is skipped until the run finishes")

    # (7b) the full referrer set: LQA + post-edit + a run-anchored vendor leg + a memory
    #      event + a delivery-empty approval + a term candidate. Ledger/learning rows keep
    #      their own dispositions (see retention._reap_document_children's table).
    full_doc = _seed_doc(rpid, "txt", days_ago=100, with_tm=False)
    ids2 = _seed_run(full_doc, rpid)
    with Session(engine) as s:
        seg_id = s.exec(select(Segment.id).where(Segment.document_id == full_doc)).first()
        vendor = Vendor(name="V-retention")
        gloss = Glossary(name="G-retention")
        s.add(vendor)
        s.add(gloss)
        s.commit()
        s.refresh(vendor)
        s.refresh(gloss)
        leg = VendorDispatch(
            vendor_id=vendor.id,
            workflow_run_id=ids2["run_id"],
            document_id=full_doc,
            target_locale="fr-FR",
            retry_contract={"segments": [{"id": seg_id, "target": "bonjour"}]},
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        leg_id = leg.id
        s.add(LQAResult(document_id=full_doc, segment_id=seg_id, dimension="accuracy"))
        s.add(
            SegmentPostEdit(
                segment_id=seg_id, document_id=full_doc, episode_seq=1, target_locale="fr-FR"
            )
        )
        # A memory EPISODE carrying the document's text: DELETED with the document (#276) --
        # the pre-#276 rule un-scoped it (document_id/segment_id -> NULL) and the exact
        # before/after text then lived on forever in the learning store.
        mev = MemoryEvent(
            project_id=rpid,
            document_id=full_doc,
            segment_id=seg_id,
            workflow_run_id=ids2["run_id"],
            before_text="a",
            after_text="b",
        )
        s.add(mev)
        # A DOCUMENT-mode approval (deleted with its document) and a KEY/project-mode one
        # that merely points at this document's run (must survive, run_id un-scoped).
        s.add(
            DeliveryEmptyApproval(
                project_id=rpid, document_id=full_doc, locale="fr-FR", run_id=ids2["run_id"]
            )
        )
        keep_dea = DeliveryEmptyApproval(
            project_id=rpid, document_id=None, locale="de-DE", run_id=ids2["run_id"]
        )
        s.add(keep_dea)
        # Term-extraction provenance: the accept/drop decision outlives the document.
        cand = TermCandidate(
            glossary_id=gloss.id, source_term="widget", source_document_id=full_doc
        )
        s.add(cand)
        s.commit()
        s.refresh(mev)
        s.refresh(keep_dea)
        s.refresh(cand)
        mev_id, keep_dea_id, cand_id = mev.id, keep_dea.id, cand.id

    res = retention.purge()
    assert res["documents"] == 1 and res["documents_skipped"] == 0, res
    with Session(engine) as s:
        assert s.get(Document, full_doc) is None, "the fully-referenced document survived"
        assert s.get(WorkflowRun, ids2["run_id"]) is None
        assert s.exec(select(LQAResult.id).where(LQAResult.document_id == full_doc)).all() == []
        assert (
            s.exec(select(SegmentPostEdit.id).where(SegmentPostEdit.document_id == full_doc)).all()
            == []
        )
        # The vendor LEDGER row cannot outlive its run: VendorDispatch.workflow_run_id is
        # NOT NULL, so a leg anchored to a deleted run goes with it (content.delete_document
        # has always had these semantics -- retention now shares that one seam). The frozen
        # dispatch package dies with it, which is the erasure outcome the GDPR seam wanted.
        assert s.get(VendorDispatch, leg_id) is None, (
            "a leg anchored to the deleted run must be gone (workflow_run_id is NOT NULL)"
        )
        assert s.get(MemoryEvent, mev_id) is None, (
            "a memory event carrying the document's text is DELETED with it (#276), not un-scoped"
        )
        assert (
            s.exec(
                select(DeliveryEmptyApproval.id).where(
                    DeliveryEmptyApproval.document_id == full_doc
                )
            ).all()
            == []
        ), "the DOCUMENT-mode empty-delivery approval must be reaped with its document"
        kept = s.get(DeliveryEmptyApproval, keep_dea_id)
        assert kept is not None and kept.run_id is None, (
            "a non-document-mode approval is a durable human decision: kept, run_id un-scoped",
            kept,
        )
        cand = s.get(TermCandidate, cand_id)
        assert cand is not None and cand.source_document_id is None, (
            "a term candidate's accept/drop memory outlives its provenance document",
            cand,
        )
    print("  purge: LQA/PE deleted, vendor leg reaped with its run, memory event DELETED (#276);")
    print("         term-candidate + key-mode delivery approval SURVIVE un-scoped")

    # (7c) the Art.17 path (cascade_tm=True) on a run-bearing document no longer raises --
    #      the direct-call seam gdpr erasure reaches, which had no coverage on this shape.
    art17_doc = _seed_doc(rpid, "txt", days_ago=1, with_tm=True)
    ids3 = _seed_run(art17_doc, rpid)
    with Session(engine) as s:
        counts = retention.delete_document(s, art17_doc, cascade_tm=True)
    assert counts["documents"] == 1 and counts["workflow_runs"] == 1, counts
    assert counts["step_runs"] == 1, counts
    with Session(engine) as s:
        assert s.get(Document, art17_doc) is None, "Art.17 erasure left the document behind"
        assert s.get(WorkflowRun, ids3["run_id"]) is None
        assert s.get(StepRun, ids3["step_run_id"]) is None
    print("  delete_document(cascade_tm=True): the Art.17 path works on a run-bearing document")

    # (7d) the content.delete_document wrapper (now pure delegation) keeps its contract on
    #      the same shape: counts dict back, document + run history gone, project TM kept.
    from app.services import content as _content

    wrap_doc = _seed_doc(rpid, "txt", days_ago=1, with_tm=True)
    ids4 = _seed_run(wrap_doc, rpid)
    with Session(engine) as s:
        wcounts = _content.delete_document(s, wrap_doc, actor="a@x.test")
    assert wcounts is not None and wcounts["documents"] == 1, wcounts
    assert wcounts["segments"] == 2 and wcounts["workflow_runs"] == 1, wcounts
    with Session(engine) as s:
        assert s.get(Document, wrap_doc) is None
        assert s.get(WorkflowRun, ids4["run_id"]) is None
        assert s.exec(select(SegmentTM.id).where(SegmentTM.project_id == rpid)).all(), (
            "a single-document delete must NOT wipe the project's shared TM"
        )
        assert _content.delete_document(s, 10_000_000) is None, (
            "a missing document still returns None (the wrapper's own contract)"
        )
    print("  content.delete_document: delegates to the shared seam, contract unchanged")

    # =====================================================================
    # (8) FIX-ROUND regressions (gate findings F2 + F5).
    # =====================================================================
    # (8a) F2 — THE HOSTILE dispatch_id SHAPE. purge_vendor_dispatch_legs' contract used to
    #      say the leg's four dispatch_id referrers were the CALLER's job, but no caller ever
    #      scoped by dispatch_id: they scope by document/segment/project, which covers every
    #      row a production writer makes ONLY because those writers also stamp document/
    #      segment. A row that carries a dispatch_id while belonging to ANOTHER document (or
    #      to none) slips through and makes `DELETE FROM vendordispatch` raise. The helper now
    #      severs them itself. These rows must SURVIVE with dispatch_id NULL -- they are not
    #      the purged document's content, and SegmentPostEdit.document_id is NOT NULL, so a
    #      delete here would destroy a LIVE document's post-edit episode.
    with Session(engine) as s:
        bp = Project(name="Bystander P", source_locale="en-US", created_by="x@example.com")
        s.add(bp)
        s.commit()
        s.refresh(bp)
        bpid = bp.id
    bystander = _seed_doc(bpid, "txt", days_ago=1, with_tm=False)  # INSIDE the window: lives
    hostile_doc = _seed_doc(rpid, "txt", days_ago=100, with_tm=False)  # aged: purged
    ids5 = _seed_run(hostile_doc, rpid)
    with Session(engine) as s:
        by_seg = s.exec(select(Segment.id).where(Segment.document_id == bystander)).first()
        # This section seeds its OWN vendor (N4). Borrowing §7b's row via
        # `select(Vendor.id).first()` coupled 8a to a section it does not otherwise depend on:
        # reorder or delete §7b and this fixture breaks for a reason no one would connect to it.
        hv = Vendor(name="V-hostile")
        s.add(hv)
        s.commit()
        s.refresh(hv)
        leg = VendorDispatch(
            vendor_id=hv.id,
            workflow_run_id=ids5["run_id"],
            document_id=None,
            target_locale="fr-FR",
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        hleg_id = leg.id
        # (i) an LQA finding carrying ONLY the dispatch_id (both nullable FKs NULL)
        orphan_lqa = LQAResult(document_id=None, segment_id=None, dispatch_id=hleg_id)
        # (ii) a post-edit episode on the BYSTANDER document, attributed to this leg
        by_pe = SegmentPostEdit(
            segment_id=by_seg, document_id=bystander, episode_seq=1, dispatch_id=hleg_id
        )
        # (iii) a vendor-originated thread + post carrying the dispatch_id, anchored to the
        #       bystander PROJECT (so no document/segment arm of the reap can see them)
        th = QueryThread(project_id=bpid, dispatch_id=hleg_id, subject="vendor q")
        s.add(orphan_lqa)
        s.add(by_pe)
        s.add(th)
        s.commit()
        s.refresh(orphan_lqa)
        s.refresh(by_pe)
        s.refresh(th)
        post = QueryPost(thread_id=th.id, dispatch_id=hleg_id, body="hi")
        s.add(post)
        s.commit()
        s.refresh(post)
        olqa_id, bype_id, th_id, post_id = orphan_lqa.id, by_pe.id, th.id, post.id

    res = retention.purge()
    assert res["documents"] == 1 and res["documents_skipped"] == 0, (
        "the hostile dispatch_id shape must not FK-block the sweep",
        res,
    )
    with Session(engine) as s:
        assert s.get(Document, hostile_doc) is None, "the aged doc survived the hostile shape"
        assert s.get(VendorDispatch, hleg_id) is None, "the run-anchored leg must be gone"
        assert s.get(Document, bystander) is not None, "the bystander document must be untouched"
        o = s.get(LQAResult, olqa_id)
        assert o is not None and o.dispatch_id is None, (
            "a dispatch-only LQA finding is SEVERED, not deleted",
            o,
        )
        pe = s.get(SegmentPostEdit, bype_id)
        assert pe is not None and pe.dispatch_id is None and pe.document_id == bystander, (
            "another document's post-edit episode must SURVIVE with only the leg link severed",
            pe,
        )
        t, p = s.get(QueryThread, th_id), s.get(QueryPost, post_id)
        assert t is not None and t.dispatch_id is None, ("the vendor thread must survive", t)
        assert p is not None and p.dispatch_id is None, ("...and so must its post", p)
    print("  purge: hostile dispatch_id-only referrers are SEVERED, not deleted; sweep survives")

    # (8b) F5 — content.delete_project step 3b: a project whose document carries a fan-out
    #      WorkflowBatch. `workflowbatch.document_id` is NOT NULL and `workflow_id` FKs the
    #      workflow, so before the fix the batch blocked BOTH the document delete (step 4) and
    #      the project-scoped workflow delete (step 3c) -- the admin project-delete surface was
    #      FK-blocked for any project with a multi-locale fan-out.
    #      The workflow MUST be project-scoped (`scoped=True`) or this proves nothing: an
    #      unscoped workflow never enters `wf_ids`, step 3c never runs, and the batch is
    #      reaped harmlessly by the per-document pass at step 4 whether 3b exists or not.
    with Session(engine) as s:
        batch_p = Project(name="Batch P", source_locale="en-US", created_by="x@example.com")
        s.add(batch_p)
        s.commit()
        s.refresh(batch_p)
        bat_pid = batch_p.id
    bat_doc = _seed_doc(bat_pid, "txt", days_ago=1, with_tm=True)
    ids6 = _seed_run(bat_doc, bat_pid, batched=True, scoped=True)
    with Session(engine) as s:
        _content.delete_project(s, bat_pid, actor="a@x.test")
    with Session(engine) as s:
        assert s.get(Project, bat_pid) is None, "a project with a BATCHED document must delete"
        assert s.get(Document, bat_doc) is None
        assert s.get(WorkflowBatch, ids6["batch_id"]) is None, "the fan-out batch must be reaped"
        assert s.get(WorkflowRun, ids6["run_id"]) is None
        assert s.get(Workflow, ids6["workflow_id"]) is None, (
            "the PROJECT-SCOPED workflow is deleted at step 3c -- the delete the batch blocked"
        )
    print("  content.delete_project: a project with a fan-out BATCH deletes (step 3b)")

    # (8c) F5 — content.delete_project step 2c, CROSS-PROJECT shape. delete_project's run set
    #      spans runs of a PROJECT-SCOPED workflow whose DOCUMENT lives in another project (the
    #      workflow_id arm). A DeliveryEmptyApproval owned by THAT other project can point at
    #      such a run; step 2c's project_id-scoped delete cannot see it, so the run delete
    #      would FK-block. It is UN-SCOPED (never deleted): another project's durable human
    #      release decision, and run_id is documented as informational provenance only.
    with Session(engine) as s:
        owner = Project(name="WF owner P", source_locale="en-US", created_by="x@example.com")
        other = Project(name="Foreign doc P", source_locale="en-US", created_by="x@example.com")
        s.add(owner)
        s.add(other)
        s.commit()
        s.refresh(owner)
        s.refresh(other)
        owner_pid, other_pid = owner.id, other.id
    foreign_doc = _seed_doc(other_pid, "txt", days_ago=1, with_tm=False)
    with Session(engine) as s:
        # a PROJECT-SCOPED workflow owned by `owner`, run over `other`'s document
        wf = Workflow(name="Project-scoped WF", project_id=owner_pid)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        run = WorkflowRun(workflow_id=wf.id, document_id=foreign_doc, target_locale="fr-FR")
        s.add(run)
        s.commit()
        s.refresh(run)
        foreign_dea = DeliveryEmptyApproval(
            project_id=other_pid, document_id=None, locale="fr-FR", run_id=run.id
        )
        s.add(foreign_dea)
        s.commit()
        s.refresh(foreign_dea)
        x_run_id, x_dea_id = run.id, foreign_dea.id
    with Session(engine) as s:
        _content.delete_project(s, owner_pid, actor="a@x.test")
    with Session(engine) as s:
        assert s.get(Project, owner_pid) is None, "the workflow-owning project must delete"
        assert s.get(WorkflowRun, x_run_id) is None, "its workflow's run is deleted with it"
        assert s.get(Document, foreign_doc) is not None, (
            "the OTHER project's document must survive -- only the run was the workflow's"
        )
        assert s.get(Project, other_pid) is not None
        kept = s.get(DeliveryEmptyApproval, x_dea_id)
        assert kept is not None and kept.run_id is None, (
            "the foreign project's delivery approval must SURVIVE with run_id un-scoped",
            kept,
        )
    print("  content.delete_project: cross-project run + foreign delivery approval (step 2c)")

    # =====================================================================
    # (9) #276 (finding r2-data-01): retention residue in a LIVE project.
    #
    #     The reviewer's scenario, verbatim: retention_days=30, one 400-day-old
    #     document plus one new document in the SAME project. Pre-#276 the purge
    #     removed the aged document and its segments and stopped there: the project
    #     kept a live document, so its TM (SegmentTM/FullTextTM + the derived
    #     SegmentEmbedding vectors) survived indefinitely, and the MemoryEvent
    #     carrying the exact source text (document_id already NULLed by the old
    #     un-scope), the MemoryEntry quoting it and the MemorySuggestion citing it
    #     survived every sweep. HECVAT AI-35 / Privacy-79 say TM, embeddings and
    #     learned memories are deleted by the retention period -- so they must go
    #     ROW BY ROW, by their own created_at, even while the project stays live.
    # =====================================================================
    with Session(engine) as s:
        cust = Customer(name="Retention residue cust")
        residue = Project(name="Residue P", source_locale="en-US", created_by="x@example.com")
        s.add(cust)
        s.add(residue)
        s.commit()
        s.refresh(cust)
        s.refresh(residue)
        cust_id, resid_pid = cust.id, residue.id

    AGED = "aged-source-text-r2-data-01"
    aged_doc = _seed_doc(resid_pid, "txt", days_ago=400, with_tm=True)
    fresh_doc = _seed_doc(resid_pid, "txt", days_ago=1, with_tm=False)

    # The aged document's residue, all backdated 400 days: its own TM rows (carrying the
    # exact source text) + a derived embedding, plus the un-scoped learning residue the
    # pre-#276 un-scope left behind (document_id already NULL -- the shape the issue
    # reproduced). The NEW document's TM + memories are fresh and must survive.
    # (Distinct source_hashes: segmenttm's (project, hash, locale) uniqueness means two
    # _seed_doc(with_tm=True) rows in one project collide.)
    with Session(engine) as s:
        s.add(
            SegmentTM(
                project_id=resid_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text=AGED,
                target_text=f"[fr] {AGED}",
                source_hash="aged-h",
            )
        )
        s.add(
            FullTextTM(
                project_id=resid_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_hash="aged-h-ft",
                source_text=AGED,
                target_text=f"[fr] {AGED}",
            )
        )
        s.add(
            SegmentTM(
                project_id=resid_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="fresh-tm-text",
                target_text="[fr] fresh-tm-text",
                source_hash="fresh-h",
            )
        )
        s.add(
            FullTextTM(
                project_id=resid_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_hash="fresh-h-ft",
                source_text="fresh-tm-text",
                target_text="[fr] fresh-tm-text",
            )
        )
        s.commit()
        aged_stm = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == resid_pid, SegmentTM.source_hash == "h1"
            )
        ).first()
        aged_src_stm = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == resid_pid, SegmentTM.source_hash == "aged-h"
            )
        ).first()
        fresh_stm = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == resid_pid, SegmentTM.source_hash == "fresh-h"
            )
        ).first()
        aged_ftm = s.exec(
            select(FullTextTM).where(
                FullTextTM.project_id == resid_pid, FullTextTM.source_hash == "h2"
            )
        ).first()
        aged_src_ftm = s.exec(
            select(FullTextTM).where(
                FullTextTM.project_id == resid_pid, FullTextTM.source_hash == "aged-h-ft"
            )
        ).first()
        fresh_ftm = s.exec(
            select(FullTextTM).where(
                FullTextTM.project_id == resid_pid, FullTextTM.source_hash == "fresh-h-ft"
            )
        ).first()
        s.add(
            SegmentEmbedding(
                segment_tm_id=aged_src_stm.id,
                project_id=resid_pid,
                source_locale="en-US",
                target_locale="fr-FR",
                model="mock",
                dim=1,
                embedding=b"\x00\x00\x80?",
            )
        )
        s.commit()
        aged_emb = s.exec(
            select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id == aged_src_stm.id)
        ).first()
        # The learning residue: an un-scoped event with the exact text, an entry quoting
        # it, a suggestion citing the event, and both citation joins in between.
        aged_ev = MemoryEvent(
            project_id=resid_pid,
            customer_id=cust_id,
            document_id=None,
            before_text=AGED,
            after_text=f"[fr] {AGED}",
        )
        s.add(aged_ev)
        s.commit()
        s.refresh(aged_ev)
        aged_en = MemoryEntry(
            customer_id=cust_id,
            project_id=resid_pid,
            body=f"Belief quoting: {AGED}",
            status="active",
        )
        s.add(aged_en)
        s.commit()
        s.refresh(aged_en)
        wf = Workflow(name="Residue suggestion wf")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        aged_sg = MemorySuggestion(
            kind="enable_recall",
            customer_id=cust_id,
            project_id=resid_pid,
            workflow_id=wf.id,
            rationale=f"Recurring failure pattern: {AGED}",
        )
        s.add(aged_sg)
        s.commit()
        s.refresh(aged_sg)
        s.add(MemoryEvidence(memory_id=aged_en.id, event_id=aged_ev.id))
        s.add(MemorySuggestionEvidence(suggestion_id=aged_sg.id, event_id=aged_ev.id))
        s.commit()
        # The NEW document's TM + memories (fresh, inside the window).
        fresh_ev = MemoryEvent(
            project_id=resid_pid,
            customer_id=cust_id,
            document_id=fresh_doc,
            before_text="fresh text",
            after_text="fresh text v2",
        )
        s.add(fresh_ev)
        fresh_en = MemoryEntry(
            customer_id=cust_id,
            project_id=resid_pid,
            body="fresh belief",
            status="active",
        )
        s.add(fresh_en)
        s.commit()
        s.refresh(fresh_ev)
        s.refresh(fresh_en)
        (
            aged_stm_id,
            fresh_stm_id,
            aged_ftm_id,
            fresh_ftm_id,
            aged_src_stm_id,
            aged_src_ftm_id,
            aged_emb_id,
            aged_ev_id,
            aged_en_id,
            aged_sg_id,
            fresh_ev_id,
            fresh_en_id,
        ) = (
            aged_stm.id,
            fresh_stm.id,
            aged_ftm.id,
            fresh_ftm.id,
            aged_src_stm.id,
            aged_src_ftm.id,
            aged_emb.id,
            aged_ev.id,
            aged_en.id,
            aged_sg.id,
            fresh_ev.id,
            fresh_en.id,
        )
    for _id, _model in (
        (aged_stm_id, SegmentTM),
        (aged_src_stm_id, SegmentTM),
        (aged_ftm_id, FullTextTM),
        (aged_src_ftm_id, FullTextTM),
        (aged_emb_id, SegmentEmbedding),
        (aged_ev_id, MemoryEvent),
        (aged_en_id, MemoryEntry),
        (aged_sg_id, MemorySuggestion),
    ):
        _backdate(_id, _model, 400)

    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    res = retention.purge()
    assert res["documents"] == 1, ("only the 400-day-old doc purges", res)
    assert res["documents_skipped"] == 0, res
    # (9a) row-by-row aging, in a project that STAYS LIVE:
    assert res["tm"] == 0, (
        "the project is NOT empty -- the old project-level clear must not fire",
        res,
    )
    assert res["tm_aged"] == 3, (
        "unreferenced aged TM and its embedding age out; live source matches survive",
        res,
    )
    assert res["memory_events_aged"] == 1, res
    assert res["memory_entries_aged"] == 1, res
    assert res["memory_suggestions_aged"] == 1, res
    assert res["memory_evidence_aged"] == 2, ("both citation joins to the erased rows go", res)
    with Session(engine) as s:
        assert s.get(Document, aged_doc) is None, "the aged document survived"
        assert s.get(Document, fresh_doc) is not None, "the fresh document was purged"
        # no row carrying or quoting the aged text remains anywhere in the project:
        assert s.exec(select(SegmentTM).where(SegmentTM.source_text == AGED)).all() == []
        assert s.exec(select(FullTextTM).where(FullTextTM.source_text == AGED)).all() == []
        assert (
            s.exec(
                select(SegmentEmbedding).where(
                    SegmentEmbedding.segment_tm_id.in_([aged_stm_id, aged_src_stm_id])
                )
            ).all()
            == []
        ), "the aged TM rows' derived vectors must go with them"
        assert s.get(MemoryEvent, aged_ev_id) is None, (
            "the un-scoped event carrying the exact aged text must age out"
        )
        # (no LLMemory add-on in the open core: aged leftover rows are DELETED, not tombstoned)
        assert s.get(MemoryEntry, aged_en_id) is None, (
            "the belief quoting the aged text must age out"
        )
        assert s.get(MemorySuggestion, aged_sg_id) is None, (
            "the suggestion citing the aged event must age out"
        )
        assert s.exec(select(MemoryEvidence.id)).all() == [], (
            "the citation join to the erased rows must go with them"
        )
        assert s.exec(select(MemorySuggestionEvidence.id)).all() == [], (
            "the suggestion citation join to the erased rows must go with them"
        )
        # and NOTHING else was lost: the fresh document's TM + memories survive.
        assert s.get(SegmentTM, fresh_stm_id) is not None, "the fresh doc's SegmentTM was lost"
        assert s.get(FullTextTM, fresh_ftm_id) is not None, "the fresh doc's FullTextTM was lost"
        assert s.get(MemoryEvent, fresh_ev_id) is not None, "the fresh memory event was lost"
        assert s.get(MemoryEntry, fresh_en_id) is not None, "the fresh belief was lost"
    print("  purge: aged TM + memory residue ages out ROW BY ROW in a live project;")
    print("         the fresh document's TM + memories survive (HECVAT AI-35 / Privacy-79)")

    # (9b) the Art. 17 path: delete_document DELETES the document's MemoryEvents (they
    #      carry the before/after text) instead of un-scoping them, and takes the
    #      citation joins that point at them.
    with Session(engine) as s:
        art17_p = Project(name="Art17 residue P", source_locale="en-US", created_by="x@example.com")
        s.add(art17_p)
        s.commit()
        s.refresh(art17_p)
        art17_pid = art17_p.id
    art17_doc = _seed_doc(art17_pid, "txt", days_ago=1, with_tm=False)
    with Session(engine) as s:
        seg_id = s.exec(select(Segment.id).where(Segment.document_id == art17_doc)).first()
        ev = MemoryEvent(
            project_id=art17_pid,
            document_id=art17_doc,
            segment_id=seg_id,
            customer_id=cust_id,
            before_text=AGED,
            after_text=f"[fr] {AGED}",
        )
        s.add(ev)
        s.commit()
        s.refresh(ev)
        join = MemoryEvidence(memory_id=fresh_en_id, event_id=ev.id)
        s.add(join)
        s.commit()
        s.refresh(ev)
        s.refresh(join)
        ev_id, join_id = ev.id, join.id
    with Session(engine) as s:
        counts = retention.delete_document(s, art17_doc, cascade_tm=True)
    assert counts["memory_events_deleted"] == 1, (
        "the Art. 17 path must report the document's events as DELETED",
        counts,
    )
    with Session(engine) as s:
        assert s.get(MemoryEvent, ev_id) is None, (
            "the Art. 17 path must DELETE the event carrying the document's text, not un-scope it"
        )
        assert s.get(MemoryEvidence, join_id) is None, (
            "the citation join to a deleted event must go with it"
        )
        # the belief that depended only on the deleted event is erased with it (plain SQL
        # when the LLMemory add-on is absent), never left holding the document's text.
        assert s.get(MemoryEntry, fresh_en_id) is None, (
            "the unsupported belief must be erased with the document's event"
        )
    print("  delete_document (Art. 17): the document's MemoryEvents are DELETED with it,")
    print("         citation joins included; unrelated fresh rows untouched")

    # (9c) retention OFF: a disabled sweep ages out NOTHING, even for aged residue.
    with Session(engine) as s:
        off_p = Project(name="Off P", source_locale="en-US", created_by="x@example.com")
        s.add(off_p)
        s.commit()
        s.refresh(off_p)
        off_pid = off_p.id
        off_tm = SegmentTM(
            project_id=off_pid,
            source_locale="en-US",
            target_locale="fr-FR",
            source_text=AGED,
            target_text=f"[fr] {AGED}",
            source_hash="off-h",
        )
        off_en = MemoryEntry(customer_id=cust_id, body=f"off belief: {AGED}", status="active")
        s.add(off_tm)
        s.add(off_en)
        s.commit()
        s.refresh(off_tm)
        s.refresh(off_en)
        off_tm_id, off_en_id = off_tm.id, off_en.id
    _backdate(off_tm_id, SegmentTM, 400)
    _backdate(off_en_id, MemoryEntry, 400)
    settings.set_many({"retention_enabled": "0", "retention_days": "30"})
    res = retention.purge()
    assert all(v == 0 for v in res.values()), ("a disabled sweep must report all zeros", res)
    with Session(engine) as s:
        assert s.get(SegmentTM, off_tm_id) is not None, "retention OFF deleted TM"
        assert s.get(MemoryEntry, off_en_id) is not None, "retention OFF deleted a memory"
        assert s.get(SegmentTM, fresh_stm_id) is not None, "retention OFF lost live TM"
    print("  kill switch: retention OFF leaves even aged TM/memory residue untouched")

    # =====================================================================
    # (10) #399 (finding dbdata-07): a FAILED blob delete does not lose its retry
    #      identity -- the key lands in the BlobDeleteOutbox in the SAME transaction
    #      as the row deletes, every purge retries it and reports it pending, and
    #      only a delete that succeeds resolves the ledger.
    # =====================================================================
    class _FlakyBlobStore:
        """A registrable adapter standing in for an open file handle / flaky object
        store: while ``failing`` is set, deletes report FAILED and the blob survives;
        once the flag flips, the same delete REMOVES it. Registered through the real
        port seam (ports.register), so the production path under test --
        delete_document, purge's drain, the counters -- runs unmodified."""

        def __init__(self):
            self.blobs = {}
            self.failing = True

        def put(self, key, data):
            self.blobs[key] = bytes(data)

        def get(self, key):
            return self.blobs[key]

        def delete(self, key):
            return self.delete_status(key) == blobstore.DeleteStatus.REMOVED

        def delete_status(self, key):
            if self.failing:
                return blobstore.DeleteStatus.FAILED
            if key in self.blobs:
                del self.blobs[key]
                return blobstore.DeleteStatus.REMOVED
            return blobstore.DeleteStatus.MISSING

    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    flaky = _FlakyBlobStore()
    ports.register("blobstore", flaky)
    try:
        with Session(engine) as s:
            p399 = Project(name="Outbox P", source_locale="en-US", created_by="x@example.com")
            s.add(p399)
            s.commit()
            s.refresh(p399)
            outbox_pid = p399.id
        stuck_doc = _seed_doc(outbox_pid, "txt", days_ago=1, with_tm=False)
        secret_key = f"{stuck_doc}.txt"
        flaky.put(secret_key, b"PRIVATE-ART17-TEXT")
        with Session(engine) as s:
            counts = retention.delete_document(s, stuck_doc, cascade_tm=False)
        assert counts["file_removed"] == 0 and counts["file_pending"] == 1, counts
        with Session(engine) as s:
            assert s.get(Document, stuck_doc) is None, "the erased document's row must be gone"
            pend = s.exec(select(BlobDeleteOutbox)).all()
            assert [r.blob_key for r in pend] == [secret_key], (
                "the failed blob key must be persisted in the SAME transaction as the row delete",
                pend,
            )
        # First sweep: the retry fails AGAIN -> the erasure is REPORTED pending, the
        # key survives, and the content is honestly NOT yet erased.
        res = retention.purge()
        assert res["blob_deletes_pending"] == 1 and res["blob_deletes_retried"] == 0, res
        with Session(engine) as s:
            pend = s.exec(select(BlobDeleteOutbox)).all()
            assert len(pend) == 1 and pend[0].attempts == 1, (
                "a retried failed delete bumps attempts and keeps the key",
                pend,
            )
        assert flaky.blobs.get(secret_key) == b"PRIVATE-ART17-TEXT", (
            "a failed retry must neither destroy the blob nor falsely claim it erased"
        )
        # MISSING is not FAILED: a healthy store with nothing under the key reports
        # MISSING -- the erase adds NOTHING to the ledger (the old False conflation of
        # missing with failure is exactly what lost the retry identity).
        flaky.failing = False
        no_blob_doc = _seed_doc(outbox_pid, "md", days_ago=1, with_tm=False)
        with Session(engine) as s:
            counts = retention.delete_document(s, no_blob_doc, cascade_tm=False)
        assert counts["file_removed"] == 0 and counts["file_pending"] == 0, (
            "a missing blob is not a failed delete",
            counts,
        )
        with Session(engine) as s:
            assert [r.blob_key for r in s.exec(select(BlobDeleteOutbox)).all()] == [secret_key], (
                "MISSING must not add to the ledger",
            )
        # Storage recovers: the next sweep retries, the blob is really deleted, the
        # ledger resolves, nothing reports pending.
        res = retention.purge()
        assert res["blob_deletes_retried"] == 1 and res["blob_deletes_pending"] == 0, res
        with Session(engine) as s:
            assert s.exec(select(BlobDeleteOutbox)).all() == [], (
                "a resolved retry deletes its ledger row"
            )
        assert secret_key not in flaky.blobs, "the successful retry must actually remove the blob"

        # Explicit erasures retry on scheduler ticks even without age-based retention.
        # A replacement upload may reuse the erased row ID, but never its blob key.
        from app.services.ingest import ingest_document

        for enabled, days in (("0", "30"), ("1", "0")):
            settings.set_many({"retention_enabled": enabled, "retention_days": days})
            flaky.failing = True
            with Session(engine) as s:
                project = s.get(Project, outbox_pid)
                old = ingest_document(s, project, "old.txt", b"private old", "segmented")
                old_id = old.id
                old_key = next(k for k, v in flaky.blobs.items() if v == b"private old")
                retention.delete_document(s, old_id, cascade_tm=False)
                replacement = ingest_document(
                    s, project, "replacement.txt", b"replacement survives", "segmented"
                )
                assert replacement.id == old_id, "fixture must exercise SQLite ID reuse"
                new_key = next(k for k, v in flaky.blobs.items() if v == b"replacement survives")
                assert new_key != old_key, "replacement must not overwrite a pending erasure"
            assert retention.tick()["blob_deletes_pending"] == 1
            flaky.failing = False
            recovered = retention.tick()
            assert recovered["blob_deletes_retried"] == 1, recovered
            assert recovered["documents"] == 0, recovered
            assert old_key not in flaky.blobs
            assert flaky.get(new_key) == b"replacement survives"
            with Session(engine) as s:
                assert s.get(Document, old_id) is not None
                retention.delete_document(s, old_id, cascade_tm=False)

        # A write that succeeds before a later ingest error must keep its cleanup key.
        # (#571: the sentence split now runs BEFORE the blob write, so the failure is
        # injected at the per-segment row build, which still follows it.)
        from unittest.mock import patch

        flaky.failing = True
        with Session(engine) as s:
            with patch("app.services.ingest.extract_inline_tags", side_effect=ValueError("parse")):
                try:
                    ingest_document(
                        s, s.get(Project, outbox_pid), "failed.txt", b"partial", "segmented"
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError("fixture must fail after the blob write")
            pending_key = next(k for k, v in flaky.blobs.items() if v == b"partial")
            assert s.exec(
                select(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == pending_key)
            ).one()
        flaky.failing = False
        assert retention.tick()["blob_deletes_retried"] == 1
        assert pending_key not in flaky.blobs
    finally:
        ports.unregister("blobstore")
    print("  #399 blob-delete outbox: FAILED delete keeps its key (same transaction); retry")
    print("         reports pending until the blob is really gone; MISSING never enters the ledger")

    _review_regressions()
    print("RETENTION OK")


if __name__ == "__main__":
    main()
