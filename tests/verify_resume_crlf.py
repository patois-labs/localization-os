"""Offline regression for browser line endings in human-review resume seeds."""

import json
import os
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_resume_crlf_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Document, Segment, SegmentPostEdit, WorkflowRun
from app.services import apikeys, content, orchestrator, security, settings
from app.services.workflows_part4 import create_workflow
from app.services.workflows_part5 import add_step

EDITS = ["Abra la puerta.\nAhora.", "  Guarde su trabajo.\t", "Bienvenido a casa. cafe\u0301"]


def paused_fixture():
    with Session(engine) as session:
        project = content.project_create(
            session,
            "Resume CRLF",
            source_locale="en-US",
            target_locales="es-ES",
            default_provider="mock",
        )
        doc = Document(project_id=project.id, filename="review.txt", fmt="txt", mode="segmented")
        session.add(doc)
        session.commit()
        segments = [
            Segment(document_id=doc.id, source_text=text, target_locale="es-ES", order_index=i)
            for i, text in enumerate(("Open the door.", "Save your work.", "Welcome home."))
        ]
        session.add_all(segments)
        session.commit()
        ids = [seg.id for seg in segments]
        wf = create_workflow(session, f"Review {doc.id}")
        add_step(
            session,
            wf.id,
            name="MT",
            provider="mock",
            model="mock-review",
            prompt_text="Translate {text}",
        )
        add_step(session, wf.id, name="Review", step_type="human_review")
        result = orchestrator.run_workflow(session, wf.id, doc.id, "es-ES")
        assert result["status"] == "paused", result
        view = orchestrator.paused_review(session, result["run_id"])
        assert view["segment_count"] == 3
        return result["run_id"], ids, [session.get(Segment, sid).target_text for sid in ids]


def main():
    init_db()
    settings.set_many(
        {
            "security_auth_required": "0",
            "multi_org_enabled": "0",
            "project_scoping": "0",
        }
    )
    settings.invalidate_cache()
    admin = security.create_user("review@example.test", "Reviewer", "admin", "offline-review-136")
    _, raw = apikeys.create_key(admin.id, "review", scopes="api docs")
    headers = {"X-Loc-Token": raw}
    records, errors = {}, []
    client = TestClient(app)
    for door, ending, bad in (
        ("web", "CRLF", False),
        ("api", "CRLF", False),
        ("web", "CR", False),
        ("web", "LF", False),
        ("api", "LF", False),
        ("web", "CRLF", True),
        ("cli-seam", "CRLF", False),
        ("structured", "CRLF", False),
    ):
        label = f"{door}-{ending}" + ("-bad-count" if bad else "")
        rid, ids, before = paused_fixture()
        newline = {"CRLF": "\r\n", "CR": "\r", "LF": "\n"}[ending]
        expected = EDITS if door != "structured" else [t.replace("\n", newline) for t in EDITS]
        payload = orchestrator.SEG_JOINER.join(EDITS[:2] if bad else EDITS).replace("\n", newline)
        events, finished = [], threading.Event()
        original = orchestrator.run_workflow_stream

        def observe(*args, original=original, events=events, finished=finished, **kwargs):
            try:
                for event in original(*args, **kwargs):
                    events.append(event)
                    yield event
            finally:
                finished.set()

        with patch_pieces("app.services.orchestrator", "run_workflow_stream", observe):
            if door in ("cli-seam", "structured"):
                with Session(engine) as session:
                    try:
                        kw = (
                            {"edited_segments": expected}
                            if door == "structured"
                            else {"edited_text": payload}
                        )
                        seed = orchestrator.prepare_resume_seed(session, rid, **kw)
                        result = orchestrator.resume_workflow(session, rid, "es-ES", seed)
                        code = 200
                        response = result
                    except orchestrator.ResumeMismatch as exc:
                        code, response = 400, str(exc)
            elif door == "web":
                result = client.post(
                    f"/runs/{rid}/resume",
                    data={"locale": "es-ES", "edited_text": payload},
                    follow_redirects=False,
                )
                code, response = result.status_code, result.text
            else:
                result = client.post(
                    f"/api/runs/{rid}/resume",
                    headers=headers,
                    json={"locale": "es-ES", "edited_text": payload},
                )
                code, response = result.status_code, result.text
            if code < 400:
                assert finished.wait(20), f"{label}: resume worker timed out"
        with Session(engine) as session:
            run = session.get(WorkflowRun, rid)
            texts = [session.get(Segment, sid).target_text for sid in ids]
            post_edits = session.exec(
                select(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(ids))
            ).all()
            rejected = [e for e in events if e["type"] == "resume_rejected"]
            record = {
                "status": run.status,
                "texts": texts,
                "utf8": [t.encode("utf-8").hex() for t in texts],
                "post_edits": len(post_edits),
                "http": code,
                "rejected": rejected,
                "response": response,
            }
            records[label] = record
            print(label + " " + json.dumps(record, ensure_ascii=True))
            try:
                if bad:
                    assert run.status == "paused" and len(rejected) == 1
                    assert "count 2 != expected 3" in rejected[0]["error"]
                    assert texts == before and not post_edits
                    assert run.current_step_index == 1 and run.finished_at is None
                else:
                    assert run.status == "completed" and texts == expected
                    assert len(post_edits) == 3
                    assert {r.segment_id for r in post_edits} == set(ids)
                    assert not rejected
                    assert record["utf8"] == [t.encode("utf-8").hex() for t in expected]
            except AssertionError:
                line = (
                    f"RED {label}: status={run.status} "
                    f"post_edits={len(post_edits)} http={code} "
                    f"events={[e['type'] for e in events]}"
                )
                errors.append(line)
                print(line)
    if path := os.environ.get("RESUME_CRLF_EVIDENCE"):
        Path(path).write_text(
            json.dumps(records, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
    assert not errors, "\n".join(errors)
    print("RESUME CRLF OK")


if __name__ == "__main__":
    main()
