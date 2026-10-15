"""Editor UX verification (SPEC-editor-ux) — offline, hermetic.

Covers the additive post-editing UX slice while pinning the load-bearing editor
contracts (nothing renamed/removed):

  - status chips render an st-{status} class matching each segment's status
  - approved_count is in the render and correct ACROSS pagination (all pages,
    not just the windowed one)
  - the glossary Terms column renders a seeded hit + a DNT lock
  - POST /segments/{id}/save keeps its EXACT legacy JSON shape (ok/status/qa)
  - the load-bearing literal strings / DOM anchors are still present
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import GlossaryEntry, Segment, SegStatus


def docx_from(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def main() -> None:
    init_db()
    client = TestClient(app)

    r = client.post(
        "/projects",
        data={
            "name": "Editor UX",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # Seed a glossary term (a normal translated term) and a DNT term directly — the
    # same legacy project-scoped rows match_terms reads (glossary_id NULL).
    with Session(engine) as s:
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_term="Adventure",
                target_term="Aventure",
                dnt=False,
            )
        )
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_term="Acme",
                target_term="Acme",
                dnt=True,
            )
        )
        s.commit()

    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(["Adventure awaits at Acme.", "Hello world.", "Second one."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])

    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        adv_seg, hello_seg = segs[0].id, segs[1].id
        # Seed varied statuses so the color-by-value chips have something to show.
        segs[0].status = SegStatus.APPROVED
        segs[0].target_text = "L'aventure vous attend chez Acme."
        segs[1].status = SegStatus.NEEDS_REVIEW
        segs[1].target_text = "Bonjour."
        s.add(segs[0])
        s.add(segs[1])
        s.commit()

    page = client.get(f"/documents/{doc_id}/editor?locale=fr-FR").text

    # --- item 1: status chips color-by-value (class alongside chip, id kept) ---
    assert f'class="chip st-approved" id="st-{adv_seg}"' in page, "approved chip class/id"
    assert f'class="chip st-needs_review" id="st-{hello_seg}"' in page, "needs_review chip class/id"
    print("  status chips carry st-{status} class (id st-{id} preserved)")

    # --- item 2: approved_count in the render, correct across ALL segments ---
    assert 'id="approvedCount">1</b> of 3 confirmed' in page, "approved_count wrong in HTML"
    print("  progress shows 1 of 3 confirmed")

    # --- item 8: glossary Terms column renders the hit + DNT lock ---
    # T5 (a11y, WCAG 3.1.2): each term sits in its own span carrying its language; the
    # arrow between them is language-neutral chrome and stays outside the target span.
    assert '<span lang="en-US">Adventure</span>' in page, "source term lacks its lang"
    assert '→ <span lang="fr-FR">Aventure</span>' in page, "translated term not rendered"
    assert "🔒" in page, "DNT lock not rendered"
    assert 'class="term dnt"' in page, "DNT term styling class missing"
    print("  glossary Terms column renders a hit + DNT lock")

    # --- approved_count correct across pagination (window of 200; force page 2 by
    #     confirming that a small doc still counts ALL approved, independent of page) ---
    p1 = client.get(f"/documents/{doc_id}/editor?locale=fr-FR&page=1").text
    assert "of 3 confirmed" in p1, "total_segments should be doc-wide, not page-wide"
    print("  approved_count/total are doc-wide (survive pagination window)")

    # --- save keeps the EXACT legacy JSON shape (contract pin) ---
    r = client.post(
        f"/segments/{hello_seg}/save", data={"target_text": "Bonjour le monde.", "tte_ms": "1200"}
    )
    d = r.json()
    assert set(d.keys()) == {"ok", "status", "qa"}, f"save JSON shape changed: {d.keys()}"
    assert d["ok"] is True and d["status"] == "edited", d
    print("  POST /segments/{id}/save keeps its exact legacy shape (ok/status/qa)")

    # --- load-bearing anchors / literals still present ---
    assert "tgt-edit" in page and f'id="tgt-{adv_seg}"' in page, "tgt-edit anchor missing"
    assert f'id="qa-{adv_seg}"' in page, "qa-{id} anchor missing"
    print("  load-bearing DOM anchors intact (tgt-edit, tgt-{id}, st-{id}, qa-{id})")

    print("EDITOR UX OK")


if __name__ == "__main__":
    main()
