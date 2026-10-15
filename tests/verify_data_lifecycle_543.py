"""Issue #543: retention vs in-flight runs, the spend ledger vs the paid provider,
project delete blob ordering, and finished jobqueue rows.

1. retention.purge never deletes an aged document that has a live (pending/queued/running/paused)
   WorkflowRun; a finished run does not protect it.
2. A spend-ledger write failure after a successful paid call neither re-calls the provider nor
   loses the answer.
3. delete_project removes KeyImage blobs only AFTER the final commit: a failing commit leaves the
   rows AND the blobs; a clean delete removes both and drains the outbox.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR; retention is enabled only inside this throwaway
data dir). ASCII-only prints. Final line: DATA LIFECYCLE 543 OK.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_data_lifecycle_543_")

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    BlobDeleteOutbox,
    Document,
    KeyImage,
    Project,
    RunStatus,
    StringKey,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.ports import blobstore
from app.services import providers as _prov
from app.services import retention, settings
from app.services import spend as spend_svc
from app.services.providers import Provider, ProviderSpec, register_provider, run_completion

init_db()
settings.set_many({"retention_enabled": "0", "retention_days": "30", "spend_cap_usd": "0"})


def _aged_doc_with_run(status):
    with Session(engine) as s:
        p = Project(name="Ret" + status, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(
            project_id=p.id,
            filename="aged.txt",
            fmt="txt",
            created_at=datetime.utcnow() - timedelta(days=400),
        )
        s.add(d)
        wf = Workflow(name="wf" + status)
        s.add(wf)
        s.commit()
        s.refresh(d)
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, name="MT"))
        s.add(
            WorkflowRun(workflow_id=wf.id, document_id=d.id, target_locale="fr-FR", status=status)
        )
        s.commit()
        return d.id


def _exists(doc_id):
    with Session(engine) as s:
        return s.get(Document, doc_id) is not None


def test_retention_skips_inflight_runs():
    live = [
        _aged_doc_with_run(st)
        for st in (RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED)
    ]
    done = _aged_doc_with_run(RunStatus.COMPLETED)
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    try:
        counts = retention.purge()
    finally:
        settings.set_many({"retention_enabled": "0"})
    assert all(_exists(d) for d in live), "a document with a live run must not be purged"
    assert not _exists(done), "a document whose run finished is purged as before"
    assert counts["documents"] >= 1 and counts["documents_skipped"] == 0, counts
    with Session(engine) as s:
        runs = s.exec(select(WorkflowRun).where(WorkflowRun.document_id.in_(live))).all()
        assert len(runs) == len(live), "the live runs must still exist"
    print("  [1] retention skips documents with in-flight runs, purges finished ones OK")


class _PaidStub(Provider):
    name = "claude"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        _PaidStub.calls += 1
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "[fr] hi"


def test_ledger_failure_does_not_recall_provider():
    register_provider("claude", _PaidStub())
    _PaidStub.calls = 0
    real = spend_svc.record_usage

    def _locked(*a, **k):
        raise OperationalError("UPDATE spendledger", {}, Exception("database is locked"))

    try:
        with (
            patch.object(spend_svc, "record_usage", side_effect=_locked) as rec,
            patch.object(_prov.log, "exception") as logged,
        ):
            res = run_completion(
                ProviderSpec(provider="claude", retry_max=3),
                "Translate: hi",
                task="translate",
                payload={"text": "hi", "target": "fr"},
            )
            assert rec.call_count >= 1
            # The unbooked paid call is logged with enough to reconcile it by hand.
            assert logged.call_count == 1, logged.call_args_list
            _msg = logged.call_args[0][0] % logged.call_args[0][1:]
            assert "NOT booked" in _msg and "provider=claude" in _msg and "usage=" in _msg, _msg
            assert "estimated_cost_usd=" in _msg and "model=" in _msg, _msg
        assert res["error"] is None and res["text"] == "[fr] hi", res
        assert _PaidStub.calls == 1, ("the paid provider was called again", _PaidStub.calls)
        # and the normal path still books spend
        before = spend_svc.spent_this_period()
        res = run_completion(
            ProviderSpec(provider="claude"),
            "Translate: hi",
            task="translate",
            payload={"text": "hi", "target": "fr"},
        )
        assert res["error"] is None and spend_svc.spent_this_period() > before
        assert real is spend_svc.record_usage
    finally:
        _prov._OVERRIDES.pop("claude", None)
    print("  [2] a ledger write failure never re-calls the paid provider OK")


def _key_project_with_image(name, blob_key):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        k = StringKey(project_id=p.id, name="k1")
        s.add(k)
        s.commit()
        s.refresh(k)
        s.add(KeyImage(key_id=k.id, project_id=p.id, blob_key=blob_key, ready=True))
        s.commit()
        pid = p.id
    blobstore.get().put(blob_key, b"pngbytes")
    return pid


def _blob_exists(key):
    try:
        return bool(blobstore.get().get(key))
    except Exception:
        return False


def test_delete_project_blobs_after_commit():
    from app.services import content
    from app.services import gdpr as _gdpr

    # (a) the final step fails -> the KeyImage rows roll back WITH their blobs intact.
    bk_a = "keyimage_" + "a" * 32 + ".png"
    pid = _key_project_with_image("DelFail", bk_a)
    with (
        Session(engine) as s,
        patch.object(_gdpr, "mark_export_artifacts_for_project", side_effect=RuntimeError("x")),
    ):
        try:
            content.delete_project(s, pid, actor="t")
        except RuntimeError:
            s.rollback()
        else:
            raise AssertionError("the injected failure was swallowed")
    with Session(engine) as s:
        assert s.exec(select(KeyImage).where(KeyImage.project_id == pid)).first() is not None, (
            "the KeyImage row must survive a failed delete"
        )
    assert _blob_exists(bk_a), "the blob must survive a delete that did not commit"

    # (b) a clean delete removes the blob after the commit and leaves no outbox row.
    bk_b = "keyimage_" + "b" * 32 + ".png"
    pid_b = _key_project_with_image("DelOk", bk_b)
    with Session(engine) as s:
        counts = content.delete_project(s, pid_b, actor="t")
    assert counts is not None
    assert not _blob_exists(bk_b), "the blob is removed once the delete committed"
    with Session(engine) as s:
        assert s.get(Project, pid_b) is None
        assert (
            s.exec(select(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == bk_b)).first()
            is None
        )

    # (c) a blob whose delete FAILS after the commit stays in the outbox for the next sweep.
    bk_c = "keyimage_" + "c" * 32 + ".png"
    pid_c = _key_project_with_image("DelPending", bk_c)
    with (
        Session(engine) as s,
        patch.object(retention, "_delete_blob_key", return_value=blobstore.DeleteStatus.FAILED),
    ):
        counts = content.delete_project(s, pid_c, actor="t")
    assert counts["file_pending"] >= 1, counts
    with Session(engine) as s:
        assert s.get(Project, pid_c) is None
        assert s.exec(select(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == bk_c)).first(), (
            "a failed post-commit blob delete must stay in the outbox"
        )
    print("  [3] delete_project deletes KeyImage blobs after the commit, outbox on failure OK")


if __name__ == "__main__":
    failures = []
    for t in (
        test_retention_skips_inflight_runs,
        test_ledger_failure_does_not_recall_provider,
        test_delete_project_blobs_after_commit,
    ):
        try:
            t()
        except Exception as exc:
            failures.append((t.__name__, str(exc)))
    assert not failures, failures
    print("DATA LIFECYCLE 543 OK")
