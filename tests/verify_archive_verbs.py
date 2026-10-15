"""Archive verbs for PROJECTS + WORKFLOWS -- residual sweep (test-residue archive gap).

Mirrors the Customer.archived_at precedent: an operator can RETIRE a project / workflow
(hide it from default listings + pickers, block it from launching new runs) WITHOUT
deleting it; the data is preserved and it is reversible. This suite proves the SERVICE +
the load-bearing RUN-BLOCK (the four surfaces are thin callers of these seams):

  A  archive/unarchive PROJECT: stamps/clears archived_at; idempotent-safe (clean error
     on a repeat); org-guarded (cross-org id -> None). project_active_only hides it.
  B  archive/unarchive WORKFLOW: same, WorkflowArchiveError on a repeat; workflow_active_only.
  C  RUN BLOCK -- an archived PROJECT refuses a new run at start_run AND run_workflow AND
     the batch _validate seam; unarchive re-enables it. In-flight is not this path.
  D  RUN BLOCK -- an archived WORKFLOW refuses a new run the same three ways.
  E  DISPATCH -- resolve_variant never swaps IN an archived sibling variant; it falls back
     to the requested (active) workflow.
  F  PRESERVED + REVERSIBLE -- archiving does not touch documents/segments/steps; a run
     succeeds again after unarchive (no data loss).

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider); ASCII-only.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_archive_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Connection,
    Document,
    Project,
    Scope,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Workflow,
    WorkflowGroup,
    WorkflowStep,
)
from app.services import (
    apikeys,
    batch_runs,
    content,
    orchestrator,
    security,
    settings,
)
from app.services import customers as customer_service
from app.services import workflows as wf_service
from app.services.connectors import store as conn_store
from app.web.queries import _applicable_workflows

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _mk_project(s):
    p = Project(name="arch-%d" % _seq(), source_locale="en-US", default_provider="mock")  # noqa: UP031
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _mk_doc(s, project):
    doc = Document(project_id=project.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
    s.add(doc)
    s.commit()
    s.refresh(doc)
    s.add(
        Segment(
            document_id=doc.id,
            target_locale="es-ES",
            order_index=0,
            source_text="Hello.",
            status=SegStatus.NEW,
        )
    )
    s.commit()
    return doc


def _mk_workflow(s, **kw):
    wf = Workflow(name="wf-%d" % _seq(), scope="global", **kw)  # noqa: UP031
    s.add(wf)
    s.commit()
    s.refresh(wf)
    s.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            provider="mock",
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate: {text}",
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
            key="translate",
        )
    )
    s.commit()
    return wf


def test_A_project_archive_service():
    with Session(engine) as s:
        p = _mk_project(s)
        pid = p.id
        content.archive_project(s, pid)
        assert s.get(Project, pid).archived_at is not None
        # idempotent-safe: a repeat archive is a clean error, not a re-stamp
        try:
            content.archive_project(s, pid)
            assert False, "expected ProjectArchiveError"
        except content.ProjectArchiveError:
            pass
        # org guard: a cross-org id resolves to None (no mutation)
        assert content.archive_project(s, pid, org_id=999) is None
        # active_only hides it
        active_ids = {r for r in s.exec(content.project_active_only(select(Project.id))).all()}  # noqa: C416
        assert pid not in active_ids
        assert not content.project_is_active(s.get(Project, pid))
        content.unarchive_project(s, pid)
        assert s.get(Project, pid).archived_at is None
        try:
            content.unarchive_project(s, pid)
            assert False, "expected ProjectArchiveError on double-unarchive"
        except content.ProjectArchiveError:
            pass
    print("A ok: project archive/unarchive service (idempotent-safe, org-guarded, active_only)")


def test_B_workflow_archive_service():
    with Session(engine) as s:
        wf = _mk_workflow(s)
        wid = wf.id
        wf_service.archive_workflow(s, wid)
        assert s.get(Workflow, wid).archived_at is not None
        try:
            wf_service.archive_workflow(s, wid)
            assert False, "expected WorkflowArchiveError"
        except wf_service.WorkflowArchiveError:
            pass
        active_ids = {r for r in s.exec(wf_service.workflow_active_only(select(Workflow.id))).all()}  # noqa: C416
        assert wid not in active_ids
        wf_service.unarchive_workflow(s, wid)
        assert s.get(Workflow, wid).archived_at is None
    print("B ok: workflow archive/unarchive service (idempotent-safe, active_only)")


def test_C_archived_project_blocks_runs():
    with Session(engine) as s:
        p = _mk_project(s)
        doc = _mk_doc(s, p)
        wf = _mk_workflow(s)
        content.archive_project(s, p.id)
        # start_run (async launcher) refuses
        try:
            orchestrator.start_run(s, wf.id, doc.id, "es-ES")
            assert False, "start_run should refuse an archived project"
        except content.ProjectArchivedError:
            pass
        # run_workflow (sync launcher) refuses
        try:
            orchestrator.run_workflow(s, wf.id, doc.id, "es-ES")
            assert False, "run_workflow should refuse an archived project"
        except content.ProjectArchivedError:
            pass
        # batch _validate refuses (surfaced as BatchError on all batch surfaces)
        try:
            batch_runs.start_batch(
                s, workflow_id=wf.id, doc_id=doc.id, locales=["es-ES"], synchronous=True
            )
            assert False, "start_batch should refuse an archived project"
        except batch_runs.BatchError as exc:
            assert "archived" in str(exc)
        # unarchive re-enables (a run now succeeds)
        content.unarchive_project(s, p.id)
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "es-ES")
        assert summary.get("status") == "completed", summary
    print("C ok: archived project blocks start_run/run_workflow/batch; unarchive re-enables")


def test_D_archived_workflow_blocks_runs():
    with Session(engine) as s:
        p = _mk_project(s)
        doc = _mk_doc(s, p)
        wf = _mk_workflow(s)
        wf_service.archive_workflow(s, wf.id)
        try:
            orchestrator.start_run(s, wf.id, doc.id, "es-ES")
            assert False, "start_run should refuse an archived workflow"
        except wf_service.WorkflowArchivedError:
            pass
        try:
            batch_runs.start_batch(
                s, workflow_id=wf.id, doc_id=doc.id, locales=["es-ES"], synchronous=True
            )
            assert False, "start_batch should refuse an archived workflow"
        except batch_runs.BatchError as exc:
            assert "archived" in str(exc)
        wf_service.unarchive_workflow(s, wf.id)
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "es-ES")
        assert summary.get("status") == "completed", summary
    print("D ok: archived workflow blocks new runs; unarchive re-enables")


def test_E_resolve_variant_excludes_archived_sibling():
    with Session(engine) as s:
        grp = WorkflowGroup(name="grp-%d" % _seq(), scope=Scope.GLOBAL)  # noqa: UP031
        s.add(grp)
        s.commit()
        s.refresh(grp)
        # requested (universal/default) variant + an exact-pair sibling that is ARCHIVED
        base = _mk_workflow(s, group_id=grp.id, is_default=True)
        sib = _mk_workflow(s, group_id=grp.id, source_locale="en-US", target_locale="es-ES")
        # while active, dispatch swaps in the exact-pair sibling
        assert wf_service.resolve_variant(s, base.id, "es-ES", "en-US") == sib.id
        # archived -> never dispatched into; falls back to the requested (active) base
        wf_service.archive_workflow(s, sib.id)
        assert wf_service.resolve_variant(s, base.id, "es-ES", "en-US") == base.id
    print("E ok: resolve_variant never swaps in an archived sibling variant")


def test_F_preserved_and_reversible():
    with Session(engine) as s:
        p = _mk_project(s)
        doc = _mk_doc(s, p)
        seg_before = len(s.exec(select(Segment).where(Segment.document_id == doc.id)).all())
        content.archive_project(s, p.id)
        # documents + segments untouched by archive
        assert s.get(Document, doc.id) is not None
        seg_after = len(s.exec(select(Segment).where(Segment.document_id == doc.id)).all())
        assert seg_before == seg_after == 1
        content.unarchive_project(s, p.id)
    print("F ok: archive preserves documents/segments; fully reversible")


def test_G_picker_exclusion():
    # Archived workflows are excluded from the run/dispatch PICKERS (M5), through the
    # shipped predicate seam (M4).
    with Session(engine) as s:
        p = _mk_project(s)
        active = _mk_workflow(s)
        retired = _mk_workflow(s)
        wf_service.archive_workflow(s, retired.id)
        s.commit()
        # _applicable_workflows (document + keys run form)
        offered = {w.id for w in _applicable_workflows(s, p)}
        assert active.id in offered and retired.id not in offered, offered
        # customers.customer_workflows (project run picker); include_archived reveals it
        pick = {w.id for w in customer_service.customer_workflows(s, None)}
        assert active.id in pick and retired.id not in pick, pick
        allrows = {
            w.id for w in customer_service.customer_workflows(s, None, include_archived=True)
        }
        assert retired.id in allrows, "include_archived must reveal archived"
    print("G ok: archived workflows excluded from run/dispatch pickers (escape hatch works)")


def test_H_web_route_and_csrf():
    # Web archive/unarchive routes work end-to-end with the login wall ON, AND the project
    # page's archive form carries the CSRF token (H1 -- a missing include made a dead button).
    from fastapi.testclient import TestClient

    from app.main import app
    from tests._csrf import csrf_for

    security.seed_initial()  # admin/admin, auth ON
    settings.set_many({"multi_org_enabled": "0"})
    settings.invalidate_cache()
    with Session(engine) as s:
        p = _mk_project(s)
        pid = p.id
    c = TestClient(app)
    csrf_for(c)
    assert (
        c.post(
            "/auth/login",
            data={"email": "admin", "password": "admin", "next": "/"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    csrf_for(c)
    # H1: the rendered project page's archive form includes the CSRF hidden input.
    page = c.get(f"/projects/{pid}").text
    assert f'action="/projects/{pid}/archive"' in page, "archive form missing from page"
    import re

    form = re.search(r'action="/projects/%d/archive".*?</form>' % pid, page, re.S)  # noqa: UP031
    assert form and 'name="csrf_token"' in form.group(0), (
        "H1: archive form is missing its CSRF token -> dead button on auth-on installs"
    )
    # Route works with a valid session + CSRF.
    r = c.post(f"/projects/{pid}/archive", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        assert s.get(Project, pid).archived_at is not None
    r = c.post(f"/projects/{pid}/unarchive", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        assert s.get(Project, pid).archived_at is None
    # R4: a missing project id is a plain 404 (like the workflow twin), not a 303.
    r = c.post("/projects/999999/archive", follow_redirects=False)
    assert r.status_code == 404, ("R4: missing id must 404", r.status_code)
    print("H ok: web archive/unarchive route works auth-on; CSRF (H1); missing id 404s (R4)")


def test_I_api_archived_filter():
    from fastapi.testclient import TestClient

    from app.main import app

    admin = security.create_user("arch-api@x.test", "ArchApi", "admin", "arch-pw-12345")
    _row, raw = apikeys.create_key(admin.id, "arch", scopes="api docs")
    hdr = {"X-Loc-Token": raw}
    with Session(engine) as s:
        act_id = _mk_project(s).id
        arch = _mk_project(s)
        arch_id = arch.id
        content.archive_project(s, arch_id)
    c = TestClient(app)
    default_ids = {p["id"] for p in c.get("/api/projects", headers=hdr).json()["projects"]}
    assert act_id in default_ids and arch_id not in default_ids, "default must hide archived"
    only_ids = {
        p["id"] for p in c.get("/api/projects?archived=only", headers=hdr).json()["projects"]
    }
    assert arch_id in only_ids and act_id not in only_ids, "archived=only"
    all_ids = {p["id"] for p in c.get("/api/projects?archived=all", headers=hdr).json()["projects"]}
    assert act_id in all_ids and arch_id in all_ids, "archived=all shows both"
    print("I ok: GET /api/projects hides archived by default; ?archived=only|all escape hatch")


def test_J_cli_verbs():
    with Session(engine) as s:
        p = _mk_project(s)
        pid = p.id
        wf = _mk_workflow(s)
        wid = wf.id
    assert cli_main(["project", "archive", str(pid)]) == 0
    with Session(engine) as s:
        assert s.get(Project, pid).archived_at is not None
    assert cli_main(["project", "unarchive", str(pid)]) == 0
    with Session(engine) as s:
        assert s.get(Project, pid).archived_at is None
    assert cli_main(["workflow", "archive", str(wid)]) == 0
    with Session(engine) as s:
        assert s.get(Workflow, wid).archived_at is not None
    print("J ok: CLI project/workflow archive+unarchive verbs")


def test_K_connector_ingest_refuses_archived():
    # R1: a DIRECT <kind>_translate / pull / translate_now call on a connection whose SYNC
    # PROJECT is archived refuses cleanly -- no paid MT / external write. Guards live at
    # each entrypoint (not just translate_now), so a CLI/api path that bypasses translate_now
    # still refuses. No network: the guard fires before build_fresh().
    with Session(engine) as s:
        p = _mk_project(s)
        content.archive_project(s, p.id)
        conn = Connection(
            kind="jira", name="arch-conn", project_id=p.id, config={"target_locales": "de-DE"}
        )
        s.add(conn)
        s.commit()
        s.refresh(conn)
        cid = conn.id
    # pull refuses (any kind)
    ok, msg, doc = conn_store.pull_connection(cid)
    assert ok is True and doc == 0 and "archived" in msg.lower(), (ok, msg, doc)
    # a direct <kind>_translate refuses (jira here) -- the entry guard, not translate_now
    ok, msg, summ = conn_store.jira_translate(cid)
    assert ok is True and summ.get("skipped_archived") and "archived" in msg.lower(), (
        ok,
        msg,
        summ,
    )
    # translate_now refuses too
    ok, msg, summ = conn_store.translate_now(cid)
    assert ok is True and summ.get("skipped_archived"), (ok, msg, summ)
    print("K ok (R1): direct connector pull/translate/translate_now refuse an archived project")


def main():
    init_db()
    test_A_project_archive_service()
    test_B_workflow_archive_service()
    test_C_archived_project_blocks_runs()
    test_D_archived_workflow_blocks_runs()
    test_E_resolve_variant_excludes_archived_sibling()
    test_F_preserved_and_reversible()
    test_G_picker_exclusion()
    test_K_connector_ingest_refuses_archived()
    # Surface tests below turn the login wall ON (seed_initial). test_H runs FIRST among
    # them, while no users exist yet, so seed_initial actually creates the admin (it is a
    # no-op once any user exists -- test_I mints its own user + key).
    test_H_web_route_and_csrf()
    test_I_api_archived_filter()
    test_J_cli_verbs()
    print("verify_archive_verbs OK")


if __name__ == "__main__":
    main()
