"""Verify role + attribution changes, offline.

- the linguist role exists, can tune AI, and is NOT a manager-level privilege
- projects and documents record who created them (created_by)
- the project page surfaces creator + per-document uploader
- the dashboard leads with the running-projects list incl. an Owner column
- the topbar renders a profile dropdown (not a bare sign-out link)
- db migration adds the created_by columns
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
from app.models import Document, Project, Role, Scope, StepType, WorkflowStep
from app.services import security, settings
from tests._csrf import csrf_for, sign_out


def docx_bytes(paras):
    d = Docx()
    for p in paras:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def login(c, who, pw="x"):
    # Auth on: the login POST needs a CSRF token, and a successful login rotates
    # the session -> refresh the client's default X-CSRF-Token header.
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def main() -> None:
    init_db()

    # --- role predicates ---
    assert "linguist" in Role.ALL
    admin = type("U", (), {"role": "admin"})()
    ling = type("U", (), {"role": "linguist"})()
    trans = type("U", (), {"role": "translator"})()
    assert security.can_tune_ai(admin) and security.can_tune_ai(ling)
    assert not security.can_tune_ai(trans)
    assert security.can_write(ling) and not security.can_manage(ling)  # writer, not manager
    print("  linguist: can tune AI + write, but not a manager-level privilege")

    # --- db migration added the columns ---
    from sqlalchemy import text

    with engine.connect() as conn:
        pcols = {r[1] for r in conn.execute(text("PRAGMA table_info(project)"))}
        dcols = {r[1] for r in conn.execute(text("PRAGMA table_info(document)"))}
    assert "created_by" in pcols and "created_by" in dcols, (pcols, dcols)
    print("  created_by columns present on project + document")

    # --- attribution end-to-end (auth ON, logged in) ---
    security.seed_initial()  # admin/admin + linguist/linguist + user1/user1, auth ON
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303

    r = c.post(
        "/projects",
        data={
            "name": "Owned Project",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        proj = s.get(Project, pid)
        assert proj.created_by == "admin", f"project.created_by={proj.created_by!r}"

    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_bytes(["Hello there."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    did = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        doc = s.get(Document, did)
        assert doc.created_by == "admin", f"document.created_by={doc.created_by!r}"
    print("  project + document record their creator (created_by=admin)")

    # surfaced on the project page
    page = c.get(f"/projects/{pid}").text
    assert "Created by" in page and "admin" in page, "creator not shown on project page"
    assert "Uploaded by" in page, "uploader column missing on project page"
    print("  project page shows creator + per-document uploader")

    # --- dashboard: running-projects list with an Owner column, no KPI boxes ---
    home = c.get("/").text
    assert "Running projects" in home and "projects-table" in home
    assert "Owner" in home and "Owned Project" in home
    assert "kpi-num" not in home, "old KPI boxes should be gone"
    print("  dashboard leads with running-projects list incl. Owner")

    # --- topbar has a profile dropdown, not a bare sign-out link ---
    assert 'class="usermenu"' in home and "Account settings" in home and "Sign out" in home
    print("  topbar renders a profile dropdown (account settings + sign out inside)")

    # --- /templates is gated to admin/linguist (a known gotcha, now fixed) ---
    # a plain writer (translator) may use the app but must NOT see or create
    # prompt templates; a linguist (AI/prompt tuning is their home turf) may.
    sign_out(c, follow_redirects=False)
    assert login(c, "user1", "user1").status_code == 303
    assert c.get("/templates").status_code == 403, "translator should be blocked from /templates"
    r = c.post("/templates/prompts", data={"name": "Sneaky", "body": "x"}, follow_redirects=False)
    assert r.status_code == 403, "translator should be blocked from creating prompt templates"

    sign_out(c, follow_redirects=False)
    assert login(c, "linguist", "linguist").status_code == 303
    assert c.get("/templates").status_code == 200, "linguist should be able to view /templates"
    r = c.post(
        "/templates/prompts",
        data={"name": "Linguist Template", "body": "Translate: {text}"},
        follow_redirects=False,
    )
    assert r.status_code == 303, "linguist should be able to create prompt templates"
    assert "Linguist Template" in c.get("/templates").text
    print("  /templates + POST /templates/prompts gated to admin/linguist (translator 403)")

    # --- /workflows CRUD is gated to admin/linguist (linguist-role intent) ---
    # workflow EDITING (create/add-step/delete-step/author/clone) is AI-setup turf
    # -> linguist-gated; but STARTING a run on an existing workflow stays open to any
    # writer (translator), same as the per-segment translate path.
    #
    # Build a workflow as linguist first (also proves linguist CAN edit).
    sign_out(c, follow_redirects=False)
    assert login(c, "linguist", "linguist").status_code == 303
    r = c.post(
        "/workflows", data={"name": "Ling WF", "scope": Scope.GLOBAL}, follow_redirects=False
    )
    assert r.status_code == 303, "linguist should be able to create a workflow"
    wf_id = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(
        f"/workflows/{wf_id}/steps",
        data={
            "name": "Translate",
            "step_type": StepType.LLM_AGENT,
            "prompt_text": "Translate: {text}",
            "chunk_policy": "auto",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, "linguist should be able to add a step"
    r = c.post(
        f"/workflows/{wf_id}/author",
        data={"description": "translate faithfully"},
        follow_redirects=False,
    )
    assert r.status_code == 200, "linguist should be able to use the prompt author"
    # The no-refresh JSON author route shares the SAME _ai_block gate as the page.
    r = c.post(
        f"/workflows/{wf_id}/author.json",
        data={"description": "translate faithfully"},
        follow_redirects=False,
    )
    assert r.status_code == 200 and r.json()["ok"] is True, (
        "linguist should be able to use the JSON prompt author"
    )
    r = c.post(f"/workflows/{wf_id}/clone", follow_redirects=False)
    assert r.status_code == 303, "linguist should be able to clone a workflow"
    print("  linguist can create/add-step/author/clone workflows")

    # translator: 403 on every EDITING route, but can still START a run.
    sign_out(c, follow_redirects=False)
    assert login(c, "user1", "user1").status_code == 303
    assert (
        c.post(
            "/workflows", data={"name": "Nope", "scope": Scope.GLOBAL}, follow_redirects=False
        ).status_code
        == 403
    ), "translator blocked from create"
    assert (
        c.post(
            f"/workflows/{wf_id}/steps",
            data={"name": "X", "step_type": StepType.LLM_AGENT},
            follow_redirects=False,
        ).status_code
        == 403
    ), "translator blocked from add-step"
    with Session(engine) as s:
        a_step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).first()
    assert (
        c.post(f"/workflows/{wf_id}/steps/{a_step.id}/delete", follow_redirects=False).status_code
        == 403
    ), "translator blocked from delete-step"
    assert (
        c.post(
            f"/workflows/{wf_id}/author", data={"description": "x"}, follow_redirects=False
        ).status_code
        == 403
    ), "translator blocked from author"
    assert (
        c.post(
            f"/workflows/{wf_id}/author.json", data={"description": "x"}, follow_redirects=False
        ).status_code
        == 403
    ), "translator blocked from JSON author"
    assert c.post(f"/workflows/{wf_id}/clone", follow_redirects=False).status_code == 403, (
        "translator blocked from clone"
    )
    print("  translator 403 on create/add-step/delete-step/author/(json author)/clone")

    # translator CAN start a run on the existing workflow (write-role, not AI-setup).
    # Give user1 project visibility (they didn't create admin's project); the point of
    # this assertion is the ROLE gate, not the orthogonal project-scoping gate.
    from app.models import ProjectMember, User

    with Session(engine) as s:
        u1 = s.exec(select(User).where(User.email == "user1")).first()
        s.add(ProjectMember(project_id=pid, user_id=u1.id, added_by="admin"))
        s.commit()
    r = c.post(
        f"/documents/{did}/run",
        data={"workflow_id": str(wf_id), "locale": "fr-FR"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/runs/" in r.headers["location"], (
        f"translator should be able to START a run: {r.status_code}"
    )
    print("  translator can still START a run on an existing workflow")

    # --- POST /documents/{id}/apply-tm requires a write role, mirroring
    # document_rename's pattern exactly (maintainer-approved fix): before this, the
    # route had only _visibility_block, so a VIEWER session (read-only by
    # definition) could apply TM from the browser. A viewer with project
    # visibility must now 403; a writer (translator) must still work.
    with Session(engine) as s:
        viewer = security.create_user(
            "viewer1", "Viewer One", Role.VIEWER, "viewer1", enforce_policy=False
        )
        s.add(ProjectMember(project_id=pid, user_id=viewer.id, added_by="admin"))
        s.commit()

    sign_out(c, follow_redirects=False)
    assert login(c, "viewer1", "viewer1").status_code == 303
    r = c.post(f"/documents/{did}/apply-tm", data={"locale": "fr-FR"}, follow_redirects=False)
    assert r.status_code == 403, (
        f"viewer's apply-tm must be refused (write role required): {r.status_code}"
    )
    print("  viewer 403 on POST /documents/{id}/apply-tm (write role required)")

    sign_out(c, follow_redirects=False)
    assert login(c, "user1", "user1").status_code == 303
    r = c.post(f"/documents/{did}/apply-tm", data={"locale": "fr-FR"}, follow_redirects=False)
    assert r.status_code == 303 and "tm_applied=" in r.headers["location"], (
        f"translator (writer) should be able to apply-tm: {r.status_code}"
    )
    print("  translator (writer) can still POST /documents/{id}/apply-tm")

    sign_out(c, follow_redirects=False)
    assert login(c, "admin", "admin").status_code == 303
    settings.set_many({"security_auth_required": "0"})
    print("ROLES OK")


if __name__ == "__main__":
    main()
