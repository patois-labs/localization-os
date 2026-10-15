"""Issue #383: source-derived glossary and Lab copies obey document erasure."""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_document_erasure_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    EvalCandidate,
    EvalDataset,
    EvalDatasetItem,
    EvalRun,
    EvalRunItem,
    EvalScore,
    Glossary,
    GlossaryEntry,
    GlossaryTerm,
    Project,
    QualityGoldItem,
    QualityGoldLabel,
    Segment,
    TermCandidate,
)
from app.services import glossary_module, retention, settings, term_extraction


def seed():
    settings.set_many({"retention_enabled": "0"})
    with Session(engine) as s:
        p = Project(name="Erasure", source_locale="en-US")
        g = Glossary(name="Erasure")
        s.add_all([p, g])
        s.commit()
        d = Document(project_id=p.id, filename="private.txt", fmt="txt")
        s.add(d)
        s.commit()
        s.add(
            Segment(
                document_id=d.id,
                target_locale="fr-FR",
                source_text="PRIVATE source",
                target_text="PRIVATE target",
            )
        )
        ids = []
        for status in ("pending", "accepted", "dropped"):
            c = TermCandidate(
                glossary_id=g.id,
                source_document_id=d.id,
                source_term=status,
                context_snippet="PRIVATE record " + status,
                source_locale="en-US",
                target_locale="fr-FR",
            )
            s.add(c)
            s.commit()
            ids.append(c.id)
            if status == "accepted":
                entry = term_extraction.accept(s, g.id, c.id)
                entry_id, term_id = entry.id, entry.term_id
                # A rename must not defeat erasure of an accepted example.
                entry.source_term = "renamed"
                entry.meta_overrides = {"usage_example": c.context_snippet, "notes": "keep"}
                s.add(entry)
            elif status == "dropped":
                term_extraction.drop(s, g.id, c.id)
        s.commit()
        glossary_module.add_term(
            s,
            g.id,
            "Independent",
            source_locale="en-US",
            target_locale="fr-FR",
            meta={"usage_example": "authored example"},
        )
        did = d.id
    # The Evaluations Lab is a paid add-on, but its tables stay in the schema: an install
    # upgraded from the full edition can still hold snapshots, and privacy erasure and
    # retention must remove them. Plant the snapshot rows directly.
    with Session(engine) as s:
        doc = s.get(Document, did)
        ds = EvalDataset(
            name="Snapshot",
            source_locale="en-US",
            target_locale="fr-FR",
            origin="document",
            source_document_id=did,
            source_project_id=doc.project_id,
            item_count=1,
        )
        ctl = EvalDataset(name="Unrelated", item_count=1)
        s.add_all([ds, ctl])
        s.commit()
        s.add_all(
            [
                EvalDatasetItem(
                    dataset_id=ds.id,
                    item_index=0,
                    source_text="PRIVATE source",
                    reference_text="PRIVATE target",
                ),
                EvalDatasetItem(dataset_id=ctl.id, item_index=0, source_text="keep"),
            ]
        )
        s.commit()
        dsid, control = ds.id, ctl.id
    with Session(engine) as s:
        item = s.exec(select(EvalDatasetItem).where(EvalDatasetItem.dataset_id == dsid)).one()
        candidate = EvalCandidate(name="Configuration")
        s.add(candidate)
        s.commit()
        run = EvalRun(dataset_id=dsid, candidate_id=candidate.id, status="done", error="PRIVATE")
        gold = QualityGoldItem(dataset_item_id=item.id, hypothesis_text="PRIVATE hypothesis")
        s.add_all([run, gold])
        s.commit()
        ri = EvalRunItem(run_id=run.id, dataset_item_id=item.id, output_text="PRIVATE output")
        s.add(ri)
        s.commit()
        score = EvalScore(run_id=run.id, run_item_id=ri.id, detail={"text": "PRIVATE"})
        label = QualityGoldLabel(gold_item_id=gold.id, span_text="PRIVATE", note="PRIVATE")
        s.add_all([score, label])
        s.commit()
        artifacts = [
            (EvalDataset, dsid),
            (EvalDatasetItem, item.id),
            (EvalRun, run.id),
            (EvalRunItem, ri.id),
            (EvalScore, score.id),
            (QualityGoldItem, gold.id),
            (QualityGoldLabel, label.id),
        ]
    return did, ids, entry_id, term_id, artifacts, control


def check_terms(fixture):
    _, ids, eid, tid, _, _ = fixture
    residue = []
    with Session(engine) as s:
        for cid, status in zip(ids, ("pending", "accepted", "dropped"), strict=True):
            c = s.get(TermCandidate, cid)
            assert c.status == status
            if c.source_document_id is not None or c.context_snippet:
                residue.append(status + " candidate context")
        entry = s.get(GlossaryEntry, eid)
        for field in ("meta", "meta_overrides"):
            if (getattr(entry, field) or {}).get("usage_example"):
                residue.append(field)
        if s.get(GlossaryTerm, tid).context_example:
            residue.append("canonical term context")
        independent = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == entry.glossary_id,
                GlossaryEntry.source_term == "Independent",
            )
        ).one()
        assert independent.meta["usage_example"] == "authored example"
        assert entry.meta_overrides["notes"] == "keep"
    assert not residue, residue


def check_lab(fixture):
    with Session(engine) as s:
        residue = [model.__name__ for model, rid in fixture[4] if s.get(model, rid) is not None]
        assert s.get(EvalDataset, fixture[5]) is not None
    assert not residue, residue


def ordinary():
    f = seed()
    with Session(engine) as s:
        counts = retention.delete_document(s, f[0], cascade_tm=False)
    check_terms(f)
    with Session(engine) as s:
        assert s.get(EvalDatasetItem, f[4][1][1]).reference_text == "PRIVATE target"
    assert counts.get("lab_datasets_preserved") == 1


def privacy():
    f = seed()
    with Session(engine) as s:
        counts = retention.delete_document(s, f[0])
    check_lab(f)
    check_terms(f)
    assert counts.get("lab_datasets_erased") == 1


def aged():
    f = seed()
    with Session(engine) as s:
        d = s.get(Document, f[0])
        d.created_at = datetime.utcnow() - timedelta(days=400)
        s.add(d)
        s.commit()
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    counts = retention.purge()
    assert counts["documents"] == 1 and counts["documents_skipped"] == 0, counts
    check_terms(f)
    check_lab(f)
    # Earlier ordinary-delete fixtures may share a reused SQLite ID; backdating
    # this synthetic document also puts those snapshots within its lifetime.
    assert counts.get("lab_datasets_erased") >= 1


def after_ordinary():
    f = seed()
    with Session(engine) as s:
        retention.delete_document(s, f[0], cascade_tm=False)
        retention.delete_document(s, f[0])
    check_lab(f)


def rollback():
    f = seed()
    with (
        Session(engine) as s,
        patch.object(retention, "_reap_document_children", side_effect=RuntimeError("injected")),
    ):
        try:
            retention.delete_document(s, f[0])
        except RuntimeError:
            s.rollback()
        else:
            raise AssertionError("failure was swallowed")
    with Session(engine) as s:
        assert s.get(Document, f[0]) is not None
        assert all(s.get(model, rid) is not None for model, rid in f[4])


def reused_id():
    f = seed()
    with Session(engine) as s:
        ds = s.get(EvalDataset, f[4][0][1])
        ds.created_at = s.get(Document, f[0]).created_at - timedelta(days=1)
        s.add(ds)
        s.commit()
        counts = retention.delete_document(s, f[0])
        assert counts.get("lab_datasets_erased") == 0
    with Session(engine) as s:
        assert all(s.get(model, rid) is not None for model, rid in f[4])


def moved_document():
    f = seed()
    with Session(engine) as s:
        p = Project(name="Moved", source_locale="en-US")
        s.add(p)
        s.commit()
        d = s.get(Document, f[0])
        d.project_id = p.id
        s.add(d)
        s.commit()
        retention.delete_document(s, f[0])
    check_lab(f)


def orphan_retention():
    f = seed()
    young = seed()
    with Session(engine) as s:
        ds = s.get(EvalDataset, f[4][0][1])
        ds.created_at = datetime.utcnow() - timedelta(days=400)
        s.add(ds)
        s.commit()
        retention.delete_document(s, f[0], cascade_tm=False)
        retention.delete_document(s, young[0], cascade_tm=False)
    settings.set_many({"retention_enabled": "0", "retention_days": "30"})
    assert retention.purge()["lab_datasets_erased"] == 0
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    counts = retention.purge()
    assert counts["lab_datasets_erased"] >= 1, counts
    check_lab(f)
    with Session(engine) as s:
        assert all(s.get(model, rid) is not None for model, rid in young[4])


def operator_delete():
    from app.kernel import content

    f = seed()
    with Session(engine) as s:
        counts = content.delete_document(s, f[0], privacy_erasure=True)
    assert counts["lab_datasets_erased"] == 1
    check_lab(f)
    f = seed()
    with Session(engine) as s:
        counts = content.delete_document(s, f[0])
    assert "1 Lab snapshot(s) still hold this document's text" in counts["warning"]


def surfaces():
    import argparse
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from app.cli import cmd_doc
    from app.main import app
    from app.models import Role
    from app.services import apikeys, security

    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    client = TestClient(app)
    user = security.create_user(
        "erase@example.test", "Erasure admin", Role.ADMIN, "fixture-pass-123"
    )
    _, raw = apikeys.create_key(user.id, "erasure-test", scopes="api docs")
    for privacy_mode in (False, True):
        f = seed()
        response = client.post(
            f"/api/documents/{f[0]}/delete",
            json={"confirm": "private.txt", "privacy_erasure": privacy_mode},
            headers={"X-Loc-Token": raw},
        )
        assert response.status_code == 200, response.text
        if privacy_mode:
            check_lab(f)
        else:
            assert "1 Lab snapshot(s)" in response.json()["warning"]
        f = seed()
        response = client.post(
            f"/documents/{f[0]}/delete",
            data={"privacy_erasure": "true"} if privacy_mode else {},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        if privacy_mode:
            check_lab(f)
        else:
            assert "lab_datasets_preserved=1" in response.headers["location"]
        f = seed()
        parser = argparse.ArgumentParser()
        cmd_doc._register_delete(parser.add_subparsers())
        argv = ["delete", str(f[0]), "--yes"]
        if privacy_mode:
            argv.append("--privacy-erasure")
        args = parser.parse_args(argv)
        emit = Mock()
        assert cmd_doc._doc_delete(args, emit) == 0
        if privacy_mode:
            check_lab(f)
        else:
            assert "1 Lab snapshot(s)" in emit.result.call_args.args[0]


def _project_of(doc_id):
    with Session(engine) as s:
        return s.get(Document, doc_id).project_id


def project_delete():
    """Whole-project delete reports preserved snapshots and honors privacy erasure."""
    from app.kernel import content

    f = seed()
    with Session(engine) as s:
        counts = content.delete_project(s, _project_of(f[0]))
    assert counts["lab_datasets_preserved"] == 1, counts
    assert counts["lab_datasets_erased"] == 0, counts
    assert "1 Lab snapshot(s) still hold this project's document text" in counts["warning"]
    with Session(engine) as s:
        assert s.get(EvalDataset, f[4][0][1]) is not None
    f = seed()
    with Session(engine) as s:
        counts = content.delete_project(s, _project_of(f[0]), privacy_erasure=True)
    assert counts["lab_datasets_erased"] == 1, counts
    assert counts["lab_datasets_preserved"] == 0 and "warning" not in counts, counts
    check_lab(f)
    check_terms(f)


def project_surfaces():
    """Web checkbox, API boolean, local and remote CLI flag all reach project erasure."""
    import io
    import json
    from contextlib import redirect_stdout
    from types import SimpleNamespace
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from app.cli import _remote_impl_part2 as remote
    from app.cli.__main__ import main as cli_main
    from app.main import app
    from app.models import Role
    from app.services import apikeys, security

    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    client = TestClient(app)
    user = security.create_user(
        "project-erase@example.test", "Project erasure admin", Role.ADMIN, "fixture-pass-123"
    )
    _, raw = apikeys.create_key(user.id, "project-erasure-test", scopes="api docs")
    page = client.get(f"/projects/{_project_of(seed()[0])}/delete").text
    assert 'name="privacy_erasure"' in page, "project delete page has no erasure checkbox"
    for privacy_mode in (False, True):
        f = seed()
        body = {"confirm": "Erasure"}
        if privacy_mode:
            body["privacy_erasure"] = True
        response = client.post(
            f"/api/projects/{_project_of(f[0])}/delete", json=body, headers={"X-Loc-Token": raw}
        )
        assert response.status_code == 200, response.text
        if privacy_mode:
            assert response.json()["cascade"]["lab_datasets_erased"] == 1, response.text
            check_lab(f)
        else:
            assert "1 Lab snapshot(s)" in response.json()["warning"], response.text
        f = seed()
        form = {"confirm_name": "Erasure"}
        if privacy_mode:
            form["privacy_erasure"] = "true"
        response = client.post(
            f"/projects/{_project_of(f[0])}/delete", data=form, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        if privacy_mode:
            assert "lab_datasets_preserved" not in response.headers["location"]
            check_lab(f)
        else:
            assert "lab_datasets_preserved=1" in response.headers["location"]
            banner = client.get(response.headers["location"]).text
            assert "1 Lab snapshot(s) still hold this project&#39;s document text" in banner or (
                "1 Lab snapshot(s) still hold this project's document text" in banner
            ), "projects page shows no preserved-snapshot banner"
        f = seed()
        argv = ["--json", "project", "delete", str(_project_of(f[0])), "--force"]
        if privacy_mode:
            argv.append("--privacy-erasure")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert cli_main(argv) == 0, buf.getvalue()
        payload = json.loads(buf.getvalue().strip().splitlines()[-1])
        if privacy_mode:
            assert payload["cascade"]["lab_datasets_erased"] == 1, payload
            check_lab(f)
        else:
            assert payload["cascade"]["lab_datasets_preserved"] == 1, payload
        remote_client = Mock()
        remote_client.post.side_effect = [
            {"project_name": "remote", "cascade": {"documents": 1}},
            {"cascade": {"lab_datasets_preserved": 0 if privacy_mode else 1}, "warning": "W"},
        ]
        emit = Mock()
        ns = SimpleNamespace(project_id=42, force=True, privacy_erasure=privacy_mode)
        with patch.object(remote, "_client", return_value=remote_client):
            assert remote.project_delete(ns, emit) == 0
        sent = remote_client.post.call_args_list[-1].kwargs["json"]
        assert sent.get("privacy_erasure", False) is privacy_mode, sent


def missing():
    with Session(engine) as s, patch.object(s, "commit", wraps=s.commit) as commit:
        counts = retention.delete_document(s, 999999)
        assert counts == {
            "documents": 0,
            "lab_datasets_erased": 0,
            "lab_datasets_preserved": 0,
        }
        commit.assert_not_called()


if __name__ == "__main__":
    init_db()
    failures = []
    for test in (
        ordinary,
        privacy,
        aged,
        after_ordinary,
        rollback,
        reused_id,
        missing,
        moved_document,
        orphan_retention,
        operator_delete,
        surfaces,
        project_delete,
        project_surfaces,
    ):
        try:
            test()
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
    assert not failures, failures
    print("DOCUMENT ERASURE OK")
