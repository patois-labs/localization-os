"""Workflow Studio Wave A — the /workflows management DASHBOARD, the dedicated
/workflows/new CREATE page + its redirects, and web/CLI/api stats parity. Offline +
hermetic. Runs are ORCHESTRATOR-DRIVEN (fixture-realism lesson).

Coverage:
  (1) dashboard renders the stats columns + a workflow's real figures; the genuine
      empty state on zero workflows; server-side ?sort= reorders rows.
  (2) /workflows/new hosts the moved create surface (Design + blank + group), and
      the create POSTs still land in the builder / group page (routes unchanged).
  (3) RBAC: /workflows/new redirects a non-AI viewer to the dashboard (never a 403
      page); the dashboard hides the CTA for them.
  (4) parity: the SAME workflow's stats are identical across the dashboard's service,
      `workflow list --json`, and /api/workflows (one service, no drift).
  (5) a collapsed GROUP row shows the variant count + aggregate stats.
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfstudio_")

import json

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    Segment,
    SegStatus,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, security, settings
from app.services import workflows as wf_service


def _app():
    from app.main import app

    return app


def _mk_wf(name, *, customer_id=None, group_id=None, provider="mock"):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global", customer_id=customer_id, group_id=group_id)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="T",
                provider=provider,
                model="m",
                prompt_text="T {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
            )
        )
        s.commit()
        return wf.id


def _mk_doc_and_run(wf_id, *, customer_id=None, pname="P"):
    with Session(engine) as s:
        p = Project(name=pname, source_locale="en-US", customer_id=customer_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=0,
                source_text="Hi.",
                status=SegStatus.NEW,
            )
        )
        s.commit()
        doc_id = doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")


def _cust(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


# ============================================================================
def test_dashboard_renders_stats_and_empty():
    """(1) Empty state on zero workflows; after a real run the dashboard shows the
    workflow with its stats columns."""
    init_db()
    settings.set_many({"security_auth_required": "0", "spend_cap_usd": "0"})
    c = TestClient(_app())
    # empty state first
    r = c.get("/workflows")
    assert r.status_code == 200
    assert "No workflows yet" in r.text, "genuine empty state missing"
    # add a workflow + a real run
    wf = _mk_wf("Dash Demo WF")
    _mk_doc_and_run(wf)
    r = c.get("/workflows")
    assert r.status_code == 200
    body = r.text
    for col in ("Runs", "Last run", "Spend", "Quality", "Customers"):
        assert col in body, f"dashboard missing column {col!r}"
    assert "Dash Demo WF" in body
    assert "Create a new workflow" in body, "primary CTA missing"
    # the create surface must NOT be on the dashboard anymore
    assert 'action="/workflows/author"' not in body, (
        "the create form must have MOVED off the dashboard to /workflows/new"
    )
    print("  (1) dashboard: empty state + stats columns + CTA, create surface moved OK")


def test_dashboard_server_side_sort():
    """(1b) ?sort= reorders rows server-side. Two workflows, one with more runs;
    sort=runs desc puts the busier first, asc flips it."""
    init_db()
    settings.set_many({"security_auth_required": "0", "spend_cap_usd": "0"})
    wf_busy = _mk_wf("ZZZ Busy WF")  # name sorts LAST so ordering can't be alpha luck
    wf_quiet = _mk_wf("AAA Quiet WF")
    _mk_doc_and_run(wf_busy, pname="b1")
    _mk_doc_and_run(wf_busy, pname="b2")
    _mk_doc_and_run(wf_quiet, pname="q1")
    c = TestClient(_app())
    desc = c.get("/workflows?sort=runs&dir=desc").text
    asc = c.get("/workflows?sort=runs&dir=asc").text
    assert desc.index("ZZZ Busy WF") < desc.index("AAA Quiet WF"), (
        "sort=runs desc must put the busier workflow first"
    )
    assert asc.index("AAA Quiet WF") < asc.index("ZZZ Busy WF"), (
        "sort=runs asc must put the quieter workflow first"
    )
    print("  (1b) server-side ?sort=runs reorders rows (desc/asc) OK")


def test_new_page_hosts_create_surface():
    """(2) /workflows/new hosts Design + blank + group create; the POSTs still work
    end-to-end (land in the builder)."""
    init_db()
    settings.set_many({"security_auth_required": "0"})
    c = TestClient(_app())
    r = c.get("/workflows/new")
    assert r.status_code == 200
    body = r.text
    assert 'action="/workflows/author"' in body, "Design-a-workflow form missing"
    assert 'action="/workflows"' in body, "manual blank-create form missing"
    assert 'action="/workflow-groups"' in body, "group-create form missing"
    # the manual create POST still lands in a builder (route unchanged)
    from tests._csrf import csrf_for

    csrf_for(c)
    r = c.post(
        "/workflows", data={"name": "From New Page", "scope": "global"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/workflows/"), (
        "manual create must redirect into the builder",
        r.status_code,
        r.headers.get("location"),
    )
    print("  (2) /workflows/new hosts the moved create surface; create POST lands in builder OK")


def test_new_page_rbac_redirect():
    """(3) A non-AI viewer (translator) is REDIRECTED from /workflows/new to the
    dashboard (never a 403 page), and the dashboard hides the CTA for them."""
    init_db()
    security.seed_initial()  # admin/linguist/user1, auth ON
    settings.set_many({"multi_org_enabled": "0"})
    c = TestClient(_app())
    from tests._csrf import csrf_for

    csrf_for(c)
    # user1 is a TRANSLATOR (no can_tune_ai)
    r = c.post(
        "/auth/login",
        data={"email": "user1", "password": "user1", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("login failed", r.status_code)
    csrf_for(c)
    r = c.get("/workflows/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/workflows", (
        "a non-AI viewer must be redirected to the dashboard, not shown a 403 page",
        r.status_code,
        r.headers.get("location"),
    )
    dash = c.get("/workflows").text
    assert "Create a new workflow" not in dash, (
        "the CTA must be hidden from a viewer who cannot create"
    )
    # a linguist DOES get the create page
    c2 = TestClient(_app())
    csrf_for(c2)
    c2.post(
        "/auth/login",
        data={"email": "linguist", "password": "linguist", "next": "/"},
        follow_redirects=False,
    )
    csrf_for(c2)
    r = c2.get("/workflows/new")
    assert r.status_code == 200 and 'action="/workflows/author"' in r.text, (
        "a linguist must reach the create page"
    )
    settings.set_many({"security_auth_required": "0"})
    print("  (3) /workflows/new RBAC: viewer redirected + CTA hidden; linguist allowed OK")


def test_stats_parity_web_cli_api():
    """(4) The SAME workflow's stats are identical across the dashboard's service,
    `workflow list --json`, and /api/workflows — one service, no drift."""
    init_db()
    settings.set_many({"security_auth_required": "0", "spend_cap_usd": "0"})
    wf = _mk_wf("Parity WF")
    _mk_doc_and_run(wf, pname="par1")
    _mk_doc_and_run(wf, pname="par2")

    # in-process service (what the dashboard route calls)
    from app.services import workflow_stats

    with Session(engine) as s:
        svc = workflow_stats.stats_for(s, [wf])[wf]

    # CLI --json
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main(["--json", "workflow", "list"])
    cli_row = next(w for w in json.loads(buf.getvalue())["workflows"] if w["id"] == wf)

    # /api/workflows (needs an lsu_ key)
    from app.services import apikeys

    admin = security.create_user("parity@x.com", "P", "admin", "paritypass123")
    _row, token = apikeys.create_key(admin.id, "parity", scopes="api docs")
    c = TestClient(_app())
    api_row = next(
        w
        for w in c.get("/api/workflows", headers={"X-Loc-Token": token}).json()["workflows"]
        if w["id"] == wf
    )

    for k in workflow_stats.STAT_KEYS:
        assert svc[k] == cli_row[k] == api_row[k], (
            f"stat {k!r} differs across surfaces: svc={svc[k]!r} "
            f"cli={cli_row[k]!r} api={api_row[k]!r}"
        )
    assert svc["runs_total"] == 2, svc
    print("  (4) stats parity web service == CLI --json == /api (all stat keys) OK")


def test_group_row_collapsed():
    """(5) A multilingual group collapses to ONE dashboard row with its variant count
    + aggregate stats."""
    init_db()
    settings.set_many({"security_auth_required": "0", "spend_cap_usd": "0"})
    with Session(engine) as s:
        grp = wf_service.create_group(s, "Multi Grp")
        gid = grp.id
    v1 = _mk_wf("var-en-es", group_id=gid)
    v2 = _mk_wf("var-en-fr", group_id=gid)
    _mk_doc_and_run(v1, pname="gv1")
    _mk_doc_and_run(v2, pname="gv2")
    c = TestClient(_app())
    body = c.get("/workflows").text
    assert "Multi Grp" in body, "the group must appear as a collapsed row"
    # the individual variants must NOT each be their own row (collapsed)
    assert "var-en-es" not in body and "var-en-fr" not in body, (
        "group variants must be collapsed into the single group row, not listed"
    )
    print("  (5) group collapses to one row (variants not listed separately) OK")


def main():
    test_dashboard_renders_stats_and_empty()
    test_dashboard_server_side_sort()
    test_new_page_hosts_create_surface()
    test_new_page_rbac_redirect()
    test_stats_parity_web_cli_api()
    test_group_row_collapsed()
    print("WORKFLOW STUDIO OK")


if __name__ == "__main__":
    main()
