"""Remote membership regression for security review 219 and 230. Offline only."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_project_scope_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Job,
    Project,
    ProjectType,
    Role,
    Segment,
    StringKey,
    TapAction,
    TapRun,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import access, apikeys, content, security, settings, tap_runs
from app.web._routes import walk_routes
from scripts.project_scope_routes import inventory, missing_callers

PASSWORD = "Offline-membership-fixture-123!"  # pregate: allow-secret test fixture only
ACTION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
TAP_RUN_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


def main():
    init_db()
    settings.set_many({"security_auth_required": "1"})
    admin = security.create_user("owner@example.test", "Owner", Role.ADMIN, PASSWORD)
    user = security.create_user("reader@example.test", "Reader", Role.TRANSLATOR, PASSWORD)
    with Session(engine) as session:
        project = content.project_create(
            session,
            "Confidential project",
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by=admin.email,
        )
        pid = project.id
        doc = Document(project_id=pid, filename="secret.txt", fmt="txt", mode="segmented")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        did = doc.id
        seg = Segment(
            document_id=did,
            source_text="CONFIDENTIAL source",
            target_text="Cible",
            target_locale="fr-FR",
            order_index=0,
        )
        session.add(seg)
        session.commit()
        session.refresh(seg)
        sid = seg.id
        assert not access.can_view_project(session, user, project)
        wf = Workflow(name="Private workflow", project_id=pid)
        key_project = Project(
            name="Private keys",
            project_type=ProjectType.KEY_BASED,
            created_by=admin.email,
        )
        session.add(key_project)
        session.commit()
        session.refresh(key_project)
        key_pid = key_project.id
        key = StringKey(project_id=key_pid, name="private.key")
        job = Job(
            kind="translate_document",
            payload={"document_id": did},
            created_by=user.email,
            status="completed",
        )
        session.add_all([wf, key, job])
        session.commit()
        for row in (wf, key, job):
            session.refresh(row)
        run = WorkflowRun(workflow_id=wf.id, document_id=did, status="completed")
        batch = WorkflowBatch(workflow_id=wf.id, document_id=did, status="completed")
        session.add_all([run, batch])
        session.commit()
        for row in (run, batch):
            session.refresh(row)
        references = {
            "pid": pid,
            "doc_id": did,
            "sid": sid,
            "run_id": run.id,
            "batch_id": batch.id,
            "key_id": key.id,
            "jid": job.id,
            "wid": wf.id,
        }
        session.add(TapAction(id=ACTION_ID, project_id=pid, document_id=did))
        session.commit()
        session.add(TapRun(id=TAP_RUN_ID, action_id=ACTION_ID, target_locale="fr-FR"))
        session.commit()
    headers = {"X-Loc-Token": apikeys.create_key(user.id, "regression", scopes="api docs keys")[1]}
    client = TestClient(app)
    failures = []
    for method, path, body in (
        ("GET", f"/api/projects/{pid}", None),
        ("GET", f"/api/projects/{pid}/documents", None),
        ("GET", f"/api/documents/{did}/segments", None),
        ("GET", f"/api/projects/{pid}/tm/export?source=en-US&target=fr-FR", None),
        ("GET", f"/api/projects/{pid}/glossary", None),
        ("POST", f"/api/projects/{pid}/rename", {"name": "intrusion"}),
        ("POST", f"/api/segments/{sid}/approve", {}),
        ("POST", f"/api/workflows/{references['wid']}/run", {"doc_id": did}),
        ("POST", f"/api/workflows/{references['wid']}/run-batch", {"doc_id": did}),
        (
            "POST",
            "/api/jobs/run",
            {"kind": "translate_document", "payload": {"document_id": did, "locale": "fr-FR"}},
        ),
        ("GET", f"/api/prompt-tap/actions/{ACTION_ID}/summary", None),
    ):
        response = client.request(method, path, headers=headers, json=body)
        if response.status_code != 404:
            # #261 (F10): the Ledger /api is admin-only, so a non-admin gets the
            # role 403 for any id, existing or not (no existence oracle).
            if path.startswith("/api/prompt-tap/") and response.status_code == 403:
                continue
            failures.append(f"{method} {path}: expected 404, got {response.status_code}")
    rows = client.get("/api/projects", headers=headers).json()
    if "Confidential project" in str(rows) or "intrusion" in str(rows):
        failures.append("project list leaked a non-member project")
    assert not failures, "\n".join(failures)
    sweep = []
    roots = (
        "/api/projects/{",
        "/api/documents/{",
        "/api/segments/{",
        "/api/runs/{",
        "/api/batches/{",
        "/api/keys/{",
        "/api/jobs/{",
    )
    for route in walk_routes(app):
        pattern = getattr(route, "path", "")
        if not pattern.startswith(roots):
            continue
        values = dict(references)
        if "/keys" in pattern or pattern.endswith("/changes"):
            values["pid"] = key_pid
        path = re.sub(r"\{([^}]+)\}", lambda m, v=values: str(v.get(m[1], 999999)), pattern)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            response = client.request(
                method,
                path,
                headers=headers,
                json={"locale": "fr-FR", "targets": ["fr-FR"]},
                params={"locale": "fr-FR", "source": "en-US", "target": "fr-FR"},
            )
            if response.status_code != 404:
                sweep.append(f"{method} {pattern} -> {response.status_code}")
    assert not sweep, json.dumps(sweep)
    with Session(engine) as session:
        assert session.get(Project, pid).name == "Confidential project"
    for path in ("/api/jobs?include_runs=1", "/api/workflows"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert "Private workflow" not in response.text and ACTION_ID not in response.text
        if path.startswith("/api/jobs"):
            assert not response.json()["jobs"]
    # Exercise the cached Ledger index with real DB provenance, no capture files or providers.
    fake_index = {"runs": {TAP_RUN_ID: {"run_id": TAP_RUN_ID, "first_ts": "2026-01-01"}}}
    # #261 (F10): the Ledger /api is admin-only; a non-admin is refused outright.
    assert client.get("/api/prompt-tap/actions", headers=headers).status_code == 403
    with patch.object(tap_runs, "_get_index", return_value=fake_index):
        assert client.get("/api/prompt-tap/runs", headers=headers).status_code == 403
    assert client.get(f"/api/prompt-tap/runs/{TAP_RUN_ID}", headers=headers).status_code == 403
    check_web(user, pid)
    check_remote_clients(client, headers, pid)
    # Membership grant and revoke take effect immediately on already-minted credentials.
    with Session(engine) as session:
        assert access.add_member(session, pid, user.email)[0]
        assert access.add_member(session, key_pid, user.email)[0]
    for path in (
        f"/api/projects/{pid}",
        f"/api/projects/{key_pid}/keys",
        f"/api/jobs/{references['jid']}",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.status_code, response.text)
    with Session(engine) as session:
        access.remove_member(session, pid, user.id)
        access.remove_member(session, key_pid, user.id)
    assert client.get(f"/api/projects/{pid}", headers=headers).status_code == 404
    for role in (Role.VIEWER, Role.REVIEWER, Role.ADMIN, Role.MANAGER, Role.LINGUIST):
        caller = security.create_user(f"{role}@example.test", role, role, PASSWORD)
        h = {"X-Loc-Token": apikeys.create_key(caller.id, "matrix", scopes="api docs")[1]}
        expected = 404 if role in (Role.VIEWER, Role.REVIEWER) else 200
        assert client.get(f"/api/projects/{pid}", headers=h).status_code == expected
    settings.set_many({"project_scoping": "0"})
    assert client.get(f"/api/projects/{pid}", headers=headers).status_code == 200
    settings.set_many({"project_scoping": "1", "security_auth_required": "0"})
    assert client.get(f"/api/projects/{pid}", headers=headers).status_code == 200
    settings.set_many({"security_auth_required": "1"})
    routes = inventory(app)
    assert not missing_callers(Path(__file__).resolve().parents[1])
    assert len(routes) > 100, f"Route enumeration incomplete: {len(routes)}"
    exceptions = json.loads(Path(__file__).with_name("project_scope_exceptions.json").read_text())
    exempt_routes = dict(exceptions["routes"])
    assert len(exempt_routes) == len(exceptions["routes"]), "Duplicate route exemptions"
    uncovered = []
    used_exceptions = set()
    for path, methods, _, guarded in routes:
        for method in methods:
            if not guarded:
                reason = exempt_routes.get(f"{method} {path}")
                used_exceptions.add(f"{method} {path}")
                if not reason or not exceptions["reasons"].get(reason):
                    uncovered.append(f"{method} {path}")
    assert not uncovered, "Routes needing a visibility guard or rationale:\n" + "\n".join(uncovered)
    assert not set(exempt_routes) - used_exceptions, "Remove obsolete route exemptions"
    print("verify_project_scoping OK")


def check_web(user, pid):
    from tests._csrf import csrf_for

    client = TestClient(app)
    csrf_for(client)
    response = client.post(
        "/auth/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    csrf_for(client)
    for method, path, data in (("GET", f"/projects/{pid}", None),):
        response = client.request(method, path, data=data, follow_redirects=False)
        expected = 403 if path.startswith("/projects/") else 404
        assert response.status_code == expected, (path, response.status_code, response.text[:100])
    # The paid Advanced UI is not part of the core tree: the page is simply absent.
    assert client.get("/advanced").status_code == 404


def check_remote_clients(client, headers, pid):
    import httpx

    from app.cli._common import CliError
    from app.cli._remote import RemoteClient

    def dispatch(request):
        response = client.request(
            request.method, request.url.raw_path.decode(), headers=headers, content=request.content
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    remote = RemoteClient(
        "http://127.0.0.1", headers["X-Loc-Token"], transport=httpx.MockTransport(dispatch)
    )
    assert not remote.get("/api/projects")["projects"]
    try:
        remote.get(f"/api/projects/{pid}")
    except CliError as exc:
        assert "not found" in str(exc).lower()
    else:
        raise AssertionError("Remote CLI exposed a non-member project")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "mcp-server"))
    import server as mcp_server

    def transport_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://membership.invalid",
            headers=headers,
        )

    async def check_tools():
        with (
            patch.object(mcp_server, "_client", transport_client),
            patch.object(mcp_server, "TOKEN", headers["X-Loc-Token"]),
        ):
            rows = await mcp_server.list_projects()
            assert "Confidential project" not in rows
            for function, kwargs in (
                (mcp_server.list_projects, {"view": "project", "project_id": pid}),
            ):
                try:
                    await function(**kwargs)
                except mcp_server.ToolError as exc:
                    assert "not found" in str(exc).lower(), str(exc)
                else:
                    raise AssertionError(f"MCP tool {function.__name__} exposed a private project")

    asyncio.run(check_tools())


if __name__ == "__main__":
    main()
