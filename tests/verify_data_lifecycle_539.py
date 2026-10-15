"""Issue #539: data exports vs expiry, erasure and failed uploads.

1. artifact_servable refuses an expired run (the sweep may not have reaped it yet).
2. erase_user purges the exports the subject requested (any scope), not other users' exports.
3. A privacy-erasure document delete removes only that document's TM rows; ordinary delete
   keeps all.
4. The rollback of a failed upload does not cancel other users' staged exports.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only prints.
Final line: DATA LIFECYCLE 539 OK.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_data_lifecycle_539_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Customer,
    DataExportRun,
    Document,
    FullTextTM,
    Project,
    Role,
    Segment,
    SegmentTM,
    TransMode,
    User,
)
from app.services import data_export as dx
from app.services import gdpr, settings

init_db()
settings.set_many(
    {
        "security_auth_required": "1",
        "multi_org_enabled": "0",
        "data_export_enabled": "1",
        "encrypt_uploads": "0",
        "encrypt_db": "0",
        "data_export_allow_plaintext_staging": "0",
        "blobstore_s3_enabled": "0",
        "data_export_retention_hours": "72",
    }
)


def _status(run_uuid):
    with Session(engine) as s:
        r = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        return r.status if r else None


def _stage(scope_kind="workspace", scope_ref=None):
    r = dx.run_export(user=None, scope_kind=scope_kind, scope_ref=scope_ref, actor="cli")
    assert r.status in ("ok", "partial"), r.status
    assert (config.EXPORTS_DIR / r.artifact_key).exists()
    return r


def _seed_project(name):
    with Session(engine) as s:
        c = Customer(name="Cust" + name)
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(name=name, customer_id=c.id, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename=name + ".txt", fmt="txt")
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(Segment(document_id=d.id, target_locale="fr-FR", source_text=name + " src"))
        s.add(
            SegmentTM(
                project_id=p.id,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text=name + " src",
                target_text=name + " tgt",
                source_hash="h" + name,
            )
        )
        s.add(
            FullTextTM(
                project_id=p.id,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text=name + " src",
                target_text=name + " tgt",
                source_hash="f" + name,
            )
        )
        s.commit()
        return c.id, p.id, d.id


def _tm_rows(pid):
    with Session(engine) as s:
        return len(s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()) + len(
            s.exec(select(FullTextTM).where(FullTextTM.project_id == pid)).all()
        )


def test_expired_not_servable():
    r = _stage()
    assert dx.artifact_servable(r.run_uuid), "precondition: a fresh run is servable"
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == r.run_uuid)).first()
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(row)
        s.commit()
    assert not dx.artifact_servable(r.run_uuid), "an expired run must not be served"
    print("  [1] expired artifact is not servable OK")


def test_erase_user_purges_only_their_own_exports():
    cid, pid, _ = _seed_project("EraseExp")
    with Session(engine) as s:
        a = User(email="adm539@x.io", role=Role.ADMIN, active=True, password_hash="x")
        b = User(email="subj539@x.io", role=Role.TRANSLATOR, active=True, password_hash="x")
        c = User(email="other539@x.io", role=Role.ADMIN, active=True, password_hash="x")
        s.add(a)
        s.add(b)
        s.add(c)
        s.commit()
        s.refresh(b)
        uid = b.id
    # The subject's own project and customer exports, plus another user's in the same org.
    mine_p = dx.run_export(user=None, scope_kind="project", scope_ref=pid, actor="subj539@x.io")
    mine_c = dx.run_export(user=None, scope_kind="customer", scope_ref=cid, actor="subj539@x.io")
    other_p = dx.run_export(user=None, scope_kind="project", scope_ref=pid, actor="other539@x.io")
    other_c = dx.run_export(user=None, scope_kind="customer", scope_ref=cid, actor="other539@x.io")
    for r in (mine_p, mine_c, other_p, other_c):
        assert r.status in ("ok", "partial"), r.status
    ok, msg = gdpr.erase_user(uid, actor="adm539@x.io")
    assert ok, msg
    assert _status(mine_p.run_uuid) == "purged", "the subject's own project export must be purged"
    assert _status(mine_c.run_uuid) == "purged", "the subject's own customer export must be purged"
    assert dx.artifact_servable(other_p.run_uuid), "another user's project export must survive"
    assert dx.artifact_servable(other_c.run_uuid), "another user's customer export must survive"
    print("  [2] erase_user purges only the subject's own exports OK")


def _tm_texts(pid):
    with Session(engine) as s:
        st = {r.source_text for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid))}
        ft = {r.source_text for r in s.exec(select(FullTextTM).where(FullTextTM.project_id == pid))}
    return st, ft


def test_privacy_erasure_removes_only_that_documents_tm():
    from app.models import SegmentEmbedding
    from app.services import content
    from app.services.tm_common import hash_text

    _, p1, d1 = _seed_project("TmKeep")
    # An ordinary delete keeps all shared TM.
    with Session(engine) as s:
        content.delete_document(s, d1, actor="t")
    assert _tm_rows(p1) == 2, "an ordinary delete keeps the shared project TM"
    # Two documents in ONE project: erase one, the other's TM rows survive.
    _, p2, d2 = _seed_project("TmErase")
    with Session(engine) as s:
        d3 = Document(project_id=p2, filename="second.txt", fmt="txt")
        s.add(d3)
        s.commit()
        s.refresh(d3)
        d3_id = d3.id
        s.add(Segment(document_id=d3_id, target_locale="fr-FR", source_text="Second src"))
        s.add(
            SegmentTM(
                project_id=p2,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="Second src",
                target_text="Second tgt",
                source_hash=hash_text("Second src", "en-US", "fr-FR"),
            )
        )
        s.add(
            FullTextTM(
                project_id=p2,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="Second src",
                target_text="Second tgt",
                source_hash=hash_text("Second src", "en-US", "fr-FR"),
            )
        )
        row = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == p2, SegmentTM.source_text == "TmErase src"
            )
        ).first()
        s.add(
            SegmentEmbedding(
                segment_tm_id=row.id,
                project_id=p2,
                source_locale="en-US",
                target_locale="fr-FR",
                model="m",
                dim=1,
                embedding=bytes(4),
            )
        )
        s.commit()
    assert _tm_rows(p2) == 4
    with Session(engine) as s:
        content.delete_document(s, d2, actor="t", privacy_erasure=True)
    st, ft = _tm_texts(p2)
    assert st == {"Second src"} and ft == {"Second src"}, (
        "only the erased document's TM rows go; the other document's survive",
        st,
        ft,
    )
    with Session(engine) as s:
        assert not s.exec(select(SegmentEmbedding).where(SegmentEmbedding.project_id == p2)).all()
    print("  [3] privacy-erasure document delete removes only that document's TM OK")


def test_failed_upload_rollback_keeps_other_exports():
    from app.ports import blobstore
    from app.services import ingest

    _, pid, _ = _seed_project("Rollback")
    other = _stage("workspace")
    selfrun = _stage("self")

    class _Boom:
        def put(self, *a, **k):
            raise RuntimeError("injected upload failure")

        def delete(self, *a, **k):
            return True

    with Session(engine) as s:
        project = s.get(Project, pid)
        with patch.object(blobstore, "get", return_value=_Boom()):
            try:
                ingest.ingest_document(s, project, "bad.txt", b"hello world", TransMode.SEGMENTED)
            except RuntimeError:
                pass
            else:
                raise AssertionError("the injected failure was swallowed")
        assert s.exec(select(Document).where(Document.filename == "bad.txt")).first() is None
    assert _status(other.run_uuid) == "ok" and _status(selfrun.run_uuid) == "ok", (
        "a failed upload must not purge other staged exports",
        _status(other.run_uuid),
        _status(selfrun.run_uuid),
    )
    assert dx.artifact_servable(other.run_uuid)
    print("  [4] failed-upload rollback leaves staged exports alone OK")


if __name__ == "__main__":
    failures = []
    for t in (
        test_expired_not_servable,
        test_erase_user_purges_only_their_own_exports,
        test_privacy_erasure_removes_only_that_documents_tm,
        test_failed_upload_rollback_keeps_other_exports,
    ):
        try:
            t()
        except AssertionError as exc:
            failures.append((t.__name__, str(exc)))
    assert not failures, failures
    print("DATA LIFECYCLE 539 OK")
