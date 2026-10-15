"""Project membership + visibility scoping verification (offline).

- admins/managers/linguists see every project; translators see only projects
  they created or were added to (ProjectMember)
- /projects list, project detail, document detail/editor/export all enforce it
- membership add/remove routes work, are idempotent, and are gated
- project_scoping=0 disables scoping; auth OFF (SYSTEM) is always a no-op
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_member_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Role, RunStatus, StepRun, WorkflowRun
from app.services import access, security, settings
from tests._csrf import csrf_for


def login(c, who, pw):
    # Auth on: the login POST needs a CSRF token, and a successful login rotates
    # the session -> refresh the client's default X-CSRF-Token header.
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def make_project(c, name, provider="mock"):
    r = c.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": "",
            "default_provider": provider,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    return int(r.headers["location"].rsplit("/", 1)[1])


def wait_run(run_id, until=(RunStatus.COMPLETED, RunStatus.FAILED), timeout=30):
    """Poll the WorkflowRun row until it reaches one of `until` before asserting on
    its SSE stream. orchestrator.start_run / resume_run_async are fire-and-forget:
    they kick execution onto a background daemon thread and return immediately, so
    reading /runs/stream while that thread is still writing StepRun/WorkflowRun rows
    races it -- a poll-based observer can end before the run reaches its terminal or
    paused state, truncating the stream (missing 'done'/'paused'). Mirrors
    verify_jobs.py's wait_job: wait for the background thread to settle, then assert.
    Does NOT weaken assertions -- observe_run replays every recorded run_started/
    step/paused/done event for a settled run exactly as it emits them live."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            last = run.status if run else None
        if last in until:
            return last
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {until} in {timeout}s (last={last})")


def main() -> None:
    init_db()
    assert security.seed_initial() is True
    assert settings.get("security_auth_required") == "1"
    assert settings.get("project_scoping") == "1"
    user2 = security.create_user("user2@x.com", "User Two", Role.TRANSLATOR, "user2-password-99")
    print("  seeded accounts + translator user2, login wall ON")

    # --- admin creates two projects and a document in project 2 ---
    a = TestClient(app)
    assert login(a, "admin", "admin").status_code == 303
    p1 = make_project(a, "Proj Alpha One")
    p2 = make_project(a, "Proj Beta Two")
    r = a.post(
        f"/projects/{p2}/documents",
        data={"mode": "segmented"},
        files={"file": ("hello.txt", b"Hello world. This is a test.", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    doc2 = int(r.headers["location"].rsplit("/", 1)[1].split("?")[0])
    print(f"  admin created project {p1} + {p2} and doc {doc2}")

    # --- translator sees ZERO projects ---
    t = TestClient(app)
    assert login(t, "user2@x.com", "user2-password-99").status_code == 303
    body = t.get("/projects").text
    assert "Proj Alpha One" not in body and "Proj Beta Two" not in body
    assert t.get(f"/projects/{p1}").status_code == 403
    assert t.get(f"/projects/{p2}").status_code == 403
    with Session(engine) as s:
        assert access.visible_project_ids(s, user2) == []
    print("  translator starts with zero visible projects")

    # translator cannot manage members either
    assert t.post(f"/projects/{p1}/members", data={"email": "user2@x.com"}).status_code == 403

    # --- admin adds the translator as a member of project 1 (idempotent) ---
    assert (
        a.post(
            f"/projects/{p1}/members", data={"email": "user2@x.com"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        a.post(
            f"/projects/{p1}/members", data={"email": "user2@x.com"}, follow_redirects=False
        ).status_code
        == 303
    )
    with Session(engine) as s:
        members = access.list_members(s, p1)
        assert len(members) == 1 and members[0][1].email == "user2@x.com"
        assert access.visible_project_ids(s, user2) == [p1]
        ok, msg = access.add_member(s, p1, "ghost@nowhere.io", "admin")
        assert ok is False and "ghost@nowhere.io" in msg
    body = a.get(f"/projects/{p1}").text
    assert "user2@x.com" in body and "Members" in body
    print("  admin adds user2 to project 1 (idempotent; unknown email rejected)")

    # --- translator now sees project 1 but still 403s on project 2 ---
    body = t.get("/projects").text
    assert "Proj Alpha One" in body and "Proj Beta Two" not in body
    assert t.get(f"/projects/{p1}").status_code == 200
    assert t.get(f"/projects/{p2}").status_code == 403
    assert t.get(f"/documents/{doc2}").status_code == 403
    assert t.get(f"/documents/{doc2}/editor").status_code == 403
    assert t.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 403
    assert t.get(f"/documents/{doc2}/lqa").status_code == 403
    assert t.post(f"/documents/{doc2}/translate", data={"locale": "fr-FR"}).status_code == 403
    assert t.get(f"/concordance?project_id={p2}&q=x&source=en-US&target=fr-FR").status_code == 403
    print("  member sees project 1; project 2 detail/editor/export/lqa all 403")

    # --- creator sees their own project ---
    p3 = make_project(t, "User Two Own Proj", provider="claude")  # Mock is admin-only (#577)
    assert t.get(f"/projects/{p3}").status_code == 200
    body = t.get("/projects").text
    assert "User Two Own Proj" in body
    with Session(engine) as s:
        assert access.visible_project_ids(s, user2) == sorted([p1, p3])
    print("  creator sees their own project")

    # --- linguist (privileged role) sees everything ---
    lc = TestClient(app)
    assert login(lc, "linguist", "linguist").status_code == 303
    body = lc.get("/projects").text
    assert "Proj Alpha One" in body and "Proj Beta Two" in body
    assert lc.get(f"/projects/{p2}").status_code == 200
    assert lc.get(f"/documents/{doc2}").status_code == 200
    print("  linguist sees all projects")

    # --- workflow runs: POST starts, GET stream only observes, access enforced ---
    # admin builds a 1-step workflow and starts a run on doc2 (in project 2)
    r = a.post("/workflows", data={"name": "Run WF", "scope": "global"}, follow_redirects=False)
    wf_id = int(r.headers["location"].rsplit("/", 1)[1])
    a.post(
        f"/workflows/{wf_id}/steps",
        data={
            "name": "T",
            "step_type": "llm_agent",
            "provider": "mock",
            "chunk_policy": "auto",
            "prompt_text": "Translate: {text}",
        },
    )

    # (2) a GET for an unstarted workflow executes NOTHING — there is no run to
    #     stream, and no StepRun/WorkflowRun rows appear.
    with Session(engine) as s:
        runs_before = len(s.exec(select(WorkflowRun)).all())
    assert a.get("/runs/stream?run_id=999999").status_code == 404
    with Session(engine) as s:
        assert len(s.exec(select(WorkflowRun)).all()) == runs_before
    print("  GET stream for a nonexistent run 404s and executes nothing")

    # (3) full lifecycle: POST start -> redirect -> observer streams to completion
    r = a.post(
        f"/documents/{doc2}/run",
        data={"workflow_id": wf_id, "locale": "fr-FR"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    run_id = int(r.headers["location"].split("/runs/", 1)[1].split("?")[0])
    wait_run(run_id)  # let the background orchestrator thread finish before observing
    body = a.get(f"/runs/stream?run_id={run_id}").text
    assert (
        '"type": "run_started"' in body and '"type": "step"' in body and '"type": "done"' in body
    ), body[:300]
    # (1) re-opening the stream re-attaches to the SAME run (no re-execution)
    with Session(engine) as s:
        n1 = len(s.exec(select(StepRun).where(StepRun.workflow_run_id == run_id)).all())
        runs_after_start = len(s.exec(select(WorkflowRun)).all())
    a.get(f"/runs/stream?run_id={run_id}")
    with Session(engine) as s:
        n2 = len(s.exec(select(StepRun).where(StepRun.workflow_run_id == run_id)).all())
        assert len(s.exec(select(WorkflowRun)).all()) == runs_after_start
    assert n1 == n2, (n1, n2)
    print("  POST-start -> observer streams to completion; re-stream never re-executes")

    # (5) access scoping: translator user2 is NOT a member of project 2 -> both
    #     starting a run and observing its stream are blocked (403/404).
    assert (
        t.post(f"/documents/{doc2}/run", data={"workflow_id": wf_id, "locale": "fr-FR"}).status_code
        == 403
    )
    assert t.get(f"/runs/stream?run_id={run_id}").status_code == 403
    assert t.get(f"/runs/{run_id}").status_code == 403
    with Session(engine) as s:
        assert len(s.exec(select(WorkflowRun)).all()) == runs_after_start
    print("  non-member cannot start or observe a run in project 2 (no execution)")

    # (4) resume: build a workflow that pauses at a human_review step, run it,
    #     then resume through the POST route to completion.
    r = a.post("/workflows", data={"name": "Pause WF", "scope": "global"}, follow_redirects=False)
    wf2 = int(r.headers["location"].rsplit("/", 1)[1])
    a.post(
        f"/workflows/{wf2}/steps",
        data={
            "name": "T",
            "step_type": "llm_agent",
            "provider": "mock",
            "chunk_policy": "auto",
            "prompt_text": "Translate: {text}",
        },
    )
    a.post(f"/workflows/{wf2}/steps", data={"name": "HR", "step_type": "human_review"})
    a.post(
        f"/workflows/{wf2}/steps",
        data={
            "name": "F",
            "step_type": "llm_agent",
            "provider": "mock",
            "chunk_policy": "auto",
            "prompt_text": "Finalize: {text}",
        },
    )
    r = a.post(
        f"/documents/{doc2}/run",
        data={"workflow_id": wf2, "locale": "fr-FR"},
        follow_redirects=False,
    )
    prun = int(r.headers["location"].split("/runs/", 1)[1].split("?")[0])
    wait_run(prun, until=(RunStatus.PAUSED, RunStatus.FAILED))  # settle at the human step
    pbody = a.get(f"/runs/stream?run_id={prun}").text
    assert '"type": "paused"' in pbody, pbody[:300]
    # doc2 is SEGMENTED (2 sentences) — the resume blob must keep the SEG_JOINER
    # delimiter for each segment; a wrong delimiter count now fails the run (F4).
    from app.services.orchestrator import SEG_JOINER

    edited_blob = SEG_JOINER.join(["HUMAN EDITED ONE", "HUMAN EDITED TWO"])
    r = a.post(
        f"/runs/{prun}/resume",
        data={"locale": "fr-FR", "edited_text": edited_blob},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    wait_run(prun)  # let the resumed background thread run to completion before observing
    rbody = a.get(f"/runs/stream?run_id={prun}").text
    assert '"type": "done"' in rbody, rbody[:300]
    with Session(engine) as s:
        assert s.get(WorkflowRun, prun).status == "completed"
    print("  human_review run pauses, then resumes via POST to completion")

    # --- removing the membership revokes visibility ---
    assert (
        a.post(f"/projects/{p1}/members/{user2.id}/remove", follow_redirects=False).status_code
        == 303
    )
    assert t.get(f"/projects/{p1}").status_code == 403
    print("  removing the member revokes access to project 1")

    # --- project_scoping=0 disables scoping entirely ---
    settings.set_many({"project_scoping": "0"})
    assert t.get(f"/projects/{p2}").status_code == 200
    assert t.get(f"/documents/{doc2}").status_code == 200
    settings.set_many({"project_scoping": "1"})
    assert t.get(f"/projects/{p2}").status_code == 403
    print("  project_scoping=0 turns scoping off; =1 restores it")

    # --- auth OFF: SYSTEM operator is never restricted ---
    settings.set_many({"security_auth_required": "0"})
    assert access.can_see_all(security.SYSTEM) is True
    with Session(engine) as s:
        assert access.visible_project_ids(s, security.SYSTEM) is None
    anon = TestClient(app)
    assert anon.get(f"/projects/{p2}").status_code == 200
    # ?status=all: project 2 was auto-completed by its workflow run (P1 lifecycle),
    # so it's hidden from the DEFAULT dashboard filter; the "scoping is a no-op"
    # intent here is about VISIBILITY, so query the unfiltered (all-status) list.
    body = anon.get("/projects?status=all").text
    assert "Proj Alpha One" in body and "Proj Beta Two" in body
    assert anon.get(f"/documents/{doc2}").status_code == 200
    print("  auth off: scoping is a complete no-op")

    print("MEMBERSHIP OK")


if __name__ == "__main__":
    main()
