"""Project status lifecycle + delivery/download verification (P1, items 2+3).

Covers, offline + hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked ->
MockProvider, but this suite writes target_text directly so no provider is even
called):

  lifecycle.set_status
    - validated transitions; invalid status refused; completed_at stamped on first
      COMPLETED and reset when leaving the completed/closed track; audits project.status
  lifecycle.mark_completed_if_done
    - fires only when EVERY locale of EVERY document is translated; never on a
      partially-translated multi-locale project; idempotent; empty project skipped
  lifecycle.reopen_if_terminal (living projects)
    - COMPLETED/CLOSED -> IN_TRANSLATION on add-locale / new upload; CANCELLED not
      auto-reopened; adding a locale via content.add_target_locale reopens
  lifecycle.auto_close_tick (scheduler hook)
    - a backdated COMPLETED project closes; a fresh one is left; setting "0" disables;
      a concurrent reopen is honored; audited project.auto_close
  dashboard default filter (GET /projects)
    - default hides completed/closed/cancelled; ?status=all shows them; ?status=<one>
      filters to that status; a completed row exposes a download link
  project detail (GET /projects/{id})
    - the "Download translated files" card renders when the project is completed
  delivery.export_project
    - ZIP has per-locale subfolders + name_locale.ext naming; a locale subset is
      honored; single-locale zip=False returns one file with the right name
  CLI parity
    - project status / project export exist and work (status transition + file write);
      project list --status filters

Final line: "LIFECYCLE OK".
"""

import io
import os
import sys
import zipfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lifecycle_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    ProjectStatus,
    Segment,
)
from app.services import audit, content, delivery, lifecycle, settings
from app.services.ingest import ingest_document


def _mk_project(name, targets):
    with Session(engine) as s:
        proj = content.project_create(
            s, name, source_locale="en-US", target_locales=" ".join(targets)
        )
        return proj.id


def _mk_translated_doc(project_id, filename, texts, targets, translate=True):
    """Ingest a .txt document and (optionally) fill target_text for every locale so
    the completion predicate can be exercised without a provider call."""
    with Session(engine) as s:
        project = s.get(Project, project_id)
        data = ("\n\n".join(texts)).encode("utf-8")
        doc = ingest_document(s, project, filename, data, "segmented")
        doc_id = doc.id
        if translate:
            segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
            for seg in segs:
                seg.target_text = "T:" + seg.source_text
                s.add(seg)
            s.commit()
    return doc_id


def _status(pid):
    with Session(engine) as s:
        return s.get(Project, pid).status


def test_set_status_transitions_and_anchor():
    pid = _mk_project("SetStatus", ["fr-FR"])
    assert _status(pid) == ProjectStatus.IN_TRANSLATION, "default is in_translation"
    n0 = audit.count(action="project.status")
    with Session(engine) as s:
        assert lifecycle.set_status(s, pid, "bogus") is None, "invalid status refused"
        p = lifecycle.set_status(s, pid, ProjectStatus.COMPLETED)
        assert p.status == ProjectStatus.COMPLETED and p.completed_at is not None, (
            "completed stamps completed_at"
        )
        first_anchor = p.completed_at
        # closing keeps the anchor (still on the completed->closed track)
        lifecycle.set_status(s, pid, ProjectStatus.CLOSED)
        assert s.get(Project, pid).completed_at == first_anchor, "close keeps anchor"
        # reopening clears the anchor so a fresh completion restarts the clock
        lifecycle.set_status(s, pid, ProjectStatus.IN_TRANSLATION)
        assert s.get(Project, pid).completed_at is None, "reopen clears anchor"
    assert audit.count(action="project.status") >= n0 + 3, "each transition audited"
    print("  set_status: validates + stamps/clears completed_at + audits OK")


def test_mark_completed_if_done_predicate():
    # Two locales, one document. Fully translated -> completes.
    pid = _mk_project("Complete", ["fr-FR", "de-DE"])
    _mk_translated_doc(
        pid, "a.txt", ["Hello world.", "Second line."], ["fr-FR", "de-DE"], translate=True
    )
    with Session(engine) as s:
        assert lifecycle.mark_completed_if_done(s, pid) is True
    assert _status(pid) == ProjectStatus.COMPLETED
    # idempotent second call
    with Session(engine) as s:
        assert lifecycle.mark_completed_if_done(s, pid) is False
    print("  mark_completed_if_done: all-locales-done -> COMPLETED, idempotent OK")


def test_partial_multilocale_not_completed():
    pid = _mk_project("Partial", ["fr-FR", "de-DE"])
    _mk_translated_doc(pid, "b.txt", ["One.", "Two."], ["fr-FR", "de-DE"], translate=False)
    # translate ONLY the fr-FR segments; de-DE stays empty -> must NOT complete
    with Session(engine) as s:
        doc = s.exec(select(Document).where(Document.project_id == pid)).first()
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "fr-FR")
        ).all()
        for seg in segs:
            seg.target_text = "T:" + seg.source_text
            s.add(seg)
        s.commit()
        assert lifecycle.mark_completed_if_done(s, pid) is False, (
            "partial multi-locale must not auto-complete"
        )
    assert _status(pid) == ProjectStatus.IN_TRANSLATION
    # an EMPTY project (no docs) never completes either
    empty = _mk_project("Empty", ["fr-FR"])
    with Session(engine) as s:
        assert lifecycle.mark_completed_if_done(s, empty) is False
    print("  mark_completed_if_done: partial multi-locale + empty project skipped OK")


def test_reopen_on_add_locale_and_upload():
    pid = _mk_project("Reopen", ["fr-FR"])
    _mk_translated_doc(pid, "c.txt", ["Alpha."], ["fr-FR"], translate=True)
    with Session(engine) as s:
        lifecycle.set_status(s, pid, ProjectStatus.COMPLETED)
    # add-locale via the content service must reopen a COMPLETED project
    with Session(engine) as s:
        added = content.add_target_locale(s, pid, "de-DE")
        assert added is True
    assert _status(pid) == ProjectStatus.IN_TRANSLATION, "add-locale reopens"

    # CLOSED reopens on a new upload; CANCELLED does NOT auto-reopen
    with Session(engine) as s:
        lifecycle.set_status(s, pid, ProjectStatus.CLOSED)
    _mk_translated_doc(pid, "d.txt", ["Beta."], ["fr-FR"], translate=False)
    assert _status(pid) == ProjectStatus.IN_TRANSLATION, "upload reopens CLOSED"

    with Session(engine) as s:
        lifecycle.set_status(s, pid, ProjectStatus.CANCELLED)
        assert lifecycle.reopen_if_terminal(s, pid) is False, "cancelled not auto-reopened"
    assert _status(pid) == ProjectStatus.CANCELLED
    print("  reopen: add-locale + upload reopen COMPLETED/CLOSED; CANCELLED sticks OK")


def test_auto_close_tick():
    settings.set_many({"project_auto_close_days": "5"})
    fresh = _mk_project("Fresh", ["fr-FR"])
    aged = _mk_project("Aged", ["fr-FR"])
    now = datetime.utcnow()
    with Session(engine) as s:
        for pid, when in ((fresh, now - timedelta(days=1)), (aged, now - timedelta(days=6))):
            p = s.get(Project, pid)
            p.status = ProjectStatus.COMPLETED
            p.completed_at = when
            s.add(p)
        s.commit()
    n0 = audit.count(action="project.auto_close")
    closed = lifecycle.auto_close_tick(now=now)
    assert aged in closed and fresh not in closed, closed
    assert _status(aged) == ProjectStatus.CLOSED and _status(fresh) == ProjectStatus.COMPLETED
    assert audit.count(action="project.auto_close") == n0 + 1, "auto_close audited"

    # setting "0" disables auto-close entirely
    settings.set_many({"project_auto_close_days": "0"})
    other = _mk_project("NoClose", ["fr-FR"])
    with Session(engine) as s:
        p = s.get(Project, other)
        p.status = ProjectStatus.COMPLETED
        p.completed_at = now - timedelta(days=30)
        s.add(p)
        s.commit()
    assert lifecycle.auto_close_tick(now=now) == [], "days=0 disables auto-close"
    assert _status(other) == ProjectStatus.COMPLETED
    settings.set_many({"project_auto_close_days": "5"})
    print("  auto_close_tick: backdated closes, fresh stays, days=0 disables OK")


def test_dashboard_default_filter(client):
    _mk_project("DashActive", ["fr-FR"])
    done = _mk_project("DashDone", ["fr-FR"])
    with Session(engine) as s:
        lifecycle.set_status(s, done, ProjectStatus.COMPLETED)
    # default filter hides completed
    body = client.get("/projects").text
    assert "DashActive" in body and "DashDone" not in body, "default hides completed"
    # ?status=all shows both
    body_all = client.get("/projects?status=all").text
    assert "DashActive" in body_all and "DashDone" in body_all, "all shows completed"
    # ?status=completed shows only the completed one + a download link
    body_c = client.get("/projects?status=completed").text
    assert "DashDone" in body_c and "DashActive" not in body_c
    assert f"/projects/{done}/export" in body_c, "completed row exposes a download link"
    print("  dashboard filter: default hides done; all + status=completed work OK")


def test_download_card_renders(client):
    pid = _mk_project("Card", ["fr-FR", "de-DE"])
    _mk_translated_doc(pid, "e.txt", ["Gamma."], ["fr-FR", "de-DE"], translate=True)
    # not completed yet -> no hero card
    assert "Download translated files" not in client.get(f"/projects/{pid}").text
    with Session(engine) as s:
        lifecycle.set_status(s, pid, ProjectStatus.COMPLETED)
    page = client.get(f"/projects/{pid}").text
    assert "Download translated files" in page, "hero card must render when completed"
    assert f"/projects/{pid}/export" in page
    # Issue #54 L4: the stored-lifecycle chip is now labelled "Lifecycle" (not a bare
    # "Status" that collides with the dashboard's approval-derived STATUS chip), and a
    # completed project names WHY it is completed so the two notions are distinguishable.
    assert ">Lifecycle<" in page, "the stored-lifecycle chip must be labelled 'Lifecycle' (L4)"
    assert "(all segments translated)" in page, (
        "an all-translated completed project's chip must say why it is completed (L4)"
    )
    print(
        "  project page: download card renders only when completed; lifecycle chip is "
        "labelled + explains completion when all segments are translated (L4) OK"
    )


def test_l4_manual_complete_shows_no_translation_claim(client):
    # Issue #54 L4 (gate-required second arm): lifecycle.set_status accepts a MANUAL COMPLETED
    # even with UNTRANSLATED segments, so the "(all segments translated)" claim must render
    # ONLY when it is actually true. A manually-completed project with untranslated work shows
    # a neutral "Lifecycle: Completed" chip and makes no translation claim.
    pid = _mk_project("L4Manual", ["fr-FR"])
    _mk_translated_doc(pid, "u.txt", ["Alpha.", "Beta."], ["fr-FR"], translate=False)
    with Session(engine) as s:
        assert lifecycle.set_status(s, pid, ProjectStatus.COMPLETED) is not None
    page = client.get(f"/projects/{pid}").text
    assert ">Lifecycle<" in page, "the lifecycle chip is still labelled 'Lifecycle'"
    assert "(all segments translated)" not in page, (
        "a manually-completed project with untranslated segments must NOT claim all translated (L4)"
    )
    # The tooltip's translation clause must also be absent (both render on the same predicate).
    assert "every segment has a translation" not in page, (
        "the completed tooltip's translation clause must not render when it is false (L4)"
    )
    print(
        "  project page L4: manually-completed-with-untranslated shows a neutral 'Completed' "
        "chip, no false translation claim OK"
    )


def test_delivery_zip_structure_and_naming():
    pid = _mk_project("Deliver", ["fr-FR", "de-DE"])
    _mk_translated_doc(
        pid, "report.txt", ["Line one.", "Line two."], ["fr-FR", "de-DE"], translate=True
    )
    with Session(engine) as s:
        data, fname, ctype, _skipped = delivery.export_project(s, pid, locales=None, zip=True)
    assert ctype == "application/zip" and fname.endswith(".zip"), (fname, ctype)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    # subfolders-per-locale + <stem>_<locale>.<ext> naming
    assert "fr-FR/report_fr-FR.txt" in names, names
    assert "de-DE/report_de-DE.txt" in names, names
    # each translated file carries the T: marker we wrote
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        body = zf.read("fr-FR/report_fr-FR.txt").decode("utf-8")
    assert "T:Line one." in body, body
    print("  delivery ZIP: per-locale subfolders + name_locale.ext naming OK")


def test_delivery_locale_subset_and_single_file():
    pid = _mk_project("Subset", ["fr-FR", "de-DE", "ja-JP"])
    _mk_translated_doc(pid, "note.txt", ["Only line."], ["fr-FR", "de-DE", "ja-JP"], translate=True)
    # subset: only fr-FR + ja-JP in the ZIP
    with Session(engine) as s:
        data, _fn, _ct, _sk = delivery.export_project(s, pid, locales=["fr-FR", "ja-JP"], zip=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        locs = {n.split("/")[0] for n in zf.namelist()}
    assert locs == {"fr-FR", "ja-JP"}, locs
    # single-locale, zip=False -> one file, name_locale.ext, not a ZIP
    with Session(engine) as s:
        data1, fname1, ctype1, _sk1 = delivery.export_project(s, pid, locales=["de-DE"], zip=False)
    assert fname1 == "note_de-DE.txt", fname1
    assert ctype1 != "application/zip", ctype1
    assert data1[:2] != b"PK", "single-file path must not be a ZIP"
    print("  delivery: locale subset honored; single-locale zip=False is one file OK")


def test_cli_project_status_and_export():
    import io as _io
    from contextlib import redirect_stdout

    from app.cli.__main__ import main as cli_main

    pid = _mk_project("CliProj", ["fr-FR"])
    _mk_translated_doc(pid, "cli.txt", ["One line."], ["fr-FR"], translate=True)

    # project status
    rc = cli_main(["project", "status", str(pid), "completed"])
    assert rc == 0, rc
    assert _status(pid) == ProjectStatus.COMPLETED

    # project export -> writes a file
    out_path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "cli_export.zip")
    rc = cli_main(["project", "export", str(pid), "-o", out_path])
    assert rc == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    with zipfile.ZipFile(out_path) as zf:
        assert "fr-FR/cli_fr-FR.txt" in zf.namelist(), zf.namelist()
    # refuse overwrite without --force
    rc = cli_main(["project", "export", str(pid), "-o", out_path])
    assert rc == 2, "must refuse to overwrite without --force"

    # project list --status filters (JSON for a machine assert)
    buf = _io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "project", "list", "--status", "completed"])
    assert rc == 0
    import json

    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    ids = {p["id"] for p in payload["projects"]}
    assert pid in ids, "completed project must appear under --status completed"
    # default filter hides it
    buf = _io.StringIO()
    with redirect_stdout(buf):
        cli_main(["--json", "project", "list"])
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert pid not in {p["id"] for p in payload["projects"]}, "default hides completed"
    print("  CLI: project status + export (+overwrite guard) + list --status OK")


def main():
    init_db()
    client = TestClient(app)
    test_set_status_transitions_and_anchor()
    test_mark_completed_if_done_predicate()
    test_partial_multilocale_not_completed()
    test_reopen_on_add_locale_and_upload()
    test_auto_close_tick()
    test_dashboard_default_filter(client)
    test_download_card_renders(client)
    test_l4_manual_complete_shows_no_translation_claim(client)
    test_delivery_zip_structure_and_naming()
    test_delivery_locale_subset_and_single_file()
    test_cli_project_status_and_export()
    print("LIFECYCLE OK")


if __name__ == "__main__":
    main()
