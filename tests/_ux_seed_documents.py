"""Offline UX seed: documents and delivery fixtures, translation memory, review text.

Documented in: DESIGN.md. Application modules are imported inside the functions, after
fresh_fixture() has pointed the app at its disposable data directory.
"""


def seed_documents(post, project, document, workflow):
    """Jev inspection engine, key-based project, image, export/job/run rows."""
    # offline inspection fixture, no provider calls.
    from app.services import engines

    judge = engines.create_profile(
        "Atlas quality judge for multilingual product documentation",
        "jev_local",
        base_url="http://127.0.0.1:8077",
        enabled=False,
    )
    # Branch 05: create/confirm hosts, still entirely in the disposable instance.
    from app.services import segmentation_profiles, settings

    segmentation_profiles.create_profile("Atlas sentences")
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    # Documents/delivery fixtures never enqueue jobs or call a paid provider.
    key_project = post(
        "/projects",
        {
            "name": "Atlas interface",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
            "customer_id": "1",
            "project_type": "key_based",
        },
    )
    keys = key_project
    post(f"/projects/{key_project}/keys/add", {"name": "welcome", "base_value": "Welcome to Atlas"})
    import io
    import json
    from unittest.mock import patch

    from PIL import Image, ImageDraw
    from PIL.PngImagePlugin import PngInfo

    canvas = Image.new("RGB", (480, 160), "#edf0fc")
    ImageDraw.Draw(canvas).text((24, 65), "Welcome to Atlas", fill="#17191e", font_size=28)
    metadata = PngInfo()
    metadata.add_text(
        "loc_tms_regions",
        json.dumps([{"text": "Welcome to Atlas", "bbox": [24, 60, 420, 48], "confidence": 0.98}]),
    )
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", pnginfo=metadata)
    with patch.dict("os.environ", {"LOC_TMS_IMAGE_DETECTOR": "mock"}):
        image = post(
            "/images/upload",
            {"project_id": project},
            {"file": ("welcome.png", buffer.getvalue(), "image/png")},
        )
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import DataExportRun, Document, Job, WorkflowRun

    with Session(engine) as session:
        session.add(
            DataExportRun(
                run_uuid="ux-export",
                scope_kind="self",
                status="ok",
                entity_counts={"documents": 2},
                artifact_bytes=2048,
            )
        )
        session.add(
            Job(
                kind="translate_document",
                status="running",
                payload={"document_id": int(document), "locale": "fr-FR"},
                progress_done=1,
                progress_total=2,
            )
        )
        key_doc = session.exec(select(Document).where(Document.project_id == int(keys))).first()
        session.add(
            WorkflowRun(
                workflow_id=int(workflow),
                document_id=key_doc.id,
                target_locale="fr-FR",
                status="running",
            )
        )
        session.commit()
    from app.models import StringKey

    with Session(engine) as session:
        key = session.exec(
            select(StringKey).where(StringKey.project_id == int(key_project))
        ).first()
        key_id = key.id
    return {
        "judge": judge.id,
        "keys": keys,
        "image": image,
        "key_project": key_project,
        "key": key_id,
    }


def seed_tm_entry(project):
    """One translation-memory entry for the TM pages."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import SegmentTM
    from app.services.tm_common import hash_text

    with Session(engine) as session:
        entry = SegmentTM(
            project_id=int(project),
            source_locale="en-US",
            target_locale="fr-FR",
            source_text="Welcome to Atlas.",
            target_text="Bienvenue dans Atlas.",
            source_hash=hash_text("Welcome to Atlas.", "en-US", "fr-FR"),
            use_count=3,
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        tm_entry = entry.id
    return tm_entry


def seed_review_text(post, project, document):
    """A FULL_TEXT document, plus translated de-DE segments, TM and a glossary term."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import SegmentTM
    from app.services.tm_common import hash_text

    # de-DE carries the translated content, so fr-FR stays removable (project-remove-locale).
    full_text = b"Welcome to Atlas.\n\nStart your first project."
    full_document = post(
        f"/projects/{project}/documents",
        {"mode": "full_text"},
        {"file": ("paragraph-review.txt", full_text, "text/plain")},
    )
    from app.models import GlossaryEntry, Segment

    with Session(engine) as session:
        segments = session.exec(
            select(Segment).where(
                Segment.document_id == int(document), Segment.target_locale == "de-DE"
            )
        )
        for segment in segments.all():
            first = segment.order_index == 0
            if first:
                segment.source_text = "Welcome to Atlas 3."
            segment.target_text = (
                "Willkommen bei Atlas." if first else "Starten Sie Ihr erstes Projekt."
            )
            segment.status = "needs_review"
            session.add(segment)
            session.add(
                SegmentTM(
                    project_id=int(project),
                    source_locale="en-US",
                    target_locale="de-DE",
                    source_text=segment.source_text,
                    target_text=segment.target_text,
                    source_hash=hash_text(segment.source_text, "en-US", "de-DE"),
                )
            )
        session.add(
            GlossaryEntry(
                project_id=int(project),
                source_locale="en-US",
                target_locale="de-DE",
                source_term="Atlas",
                target_term="Atlas",
                dnt=True,
            )
        )
        session.commit()
    return full_document
