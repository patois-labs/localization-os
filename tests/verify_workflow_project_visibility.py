"""Issue #372: web workflow reads respect the same project ring as the API."""

import os
import re
import sys
from functools import partial

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_workflow_visibility_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Project,
    PromptTemplate,
    Role,
    Scope,
    Workflow,
    WorkflowGroup,
    WorkflowStep,
)
from app.services import access, apikeys, security, settings
from tests._csrf import csrf_for


def login(user, phrase):
    client = TestClient(app)
    csrf_for(client)
    response = client.post(
        "/auth/login", data={"email": user, "password": phrase}, follow_redirects=False
    )
    assert response.status_code == 303
    csrf_for(client)
    return client


def main():
    init_db()
    security.seed_initial()
    with Session(engine) as s:
        project = Project(name="Private project", created_by="admin")
        group = WorkflowGroup(name="Shared group")
        customer = Customer(name="Visible customer")
        prompt = PromptTemplate(name="Shared prompt", body="Translate {text}")
        s.add(prompt)
        s.add(customer)
        s.add(project)
        s.add(group)
        s.commit()
        s.refresh(project)
        s.refresh(group)
        s.refresh(customer)
        s.refresh(prompt)
        prompt_id = prompt.id
        cid = customer.id
        pid, gid = project.id, group.id
        hidden = []
        # project_id is authoritative even on legacy rows marked GLOBAL.
        for scope, group_id in [(Scope.PROJECT, None), (Scope.GLOBAL, None), (Scope.PROJECT, gid)]:
            wf = Workflow(
                name=f"Confidential workflow {len(hidden)}",
                scope=scope,
                project_id=pid,
                group_id=group_id,
            )
            s.add(wf)
            s.commit()
            s.refresh(wf)
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    name="Private step",
                    prompt_text="PRIVATE-PROMPT-372",
                    prompt_template_id=prompt_id,
                )
            )
            hidden.append((wf.id, wf.name))
        public = Workflow(name="Public workflow", scope=Scope.GLOBAL)
        s.add(public)
        s.commit()
        s.refresh(public)
        public_id = public.id

    from app.kernel import content as content_svc
    from app.models import WorkflowRun
    from app.services import keys as keys_svc

    with Session(engine) as s:
        kproj = content_svc.project_create(
            s,
            "Keys project",
            source_locale="en-US",
            target_locales="de-DE",
            creator=None,
            default_provider="mock",
            project_type="key_based",
        )
        keys_svc.create_key(s, kproj, "greeting", namespace="common", base_value="hello", actor="x")
        kid = kproj.id

    def run_count(wid):
        with Session(engine) as s:
            return len(s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).all())

    failures = []

    def check(ok, label):
        if not ok:
            failures.append(label)

    for role in (Role.VIEWER, Role.TRANSLATOR, Role.REVIEWER):
        phrase = "visibility-fixture-372"
        user = security.create_user(f"{role}@example.test", role, role, phrase)
        c = login(user.email, phrase)
        _, raw = apikeys.create_key(user.id, "visibility", scopes="docs")
        api = TestClient(app, headers={"X-Loc-Token": raw})
        check(c.get(f"/projects/{pid}").status_code == 403, f"{role}: project denied")
        check(api.get(f"/api/projects/{pid}").status_code == 404, f"{role}: API project denied")
        for wid, _name in hidden:
            for suffix in ("", "/plan"):
                response = c.get(f"/workflows/{wid}{suffix}")
                missing = c.get(f"/workflows/999999{suffix}")
                check(response.status_code == 404, f"{role}: web {wid}{suffix} denied")
                # Each response has a fresh CSP nonce; compare all other markup.
                check(
                    re.sub(r'nonce="[^"]*"', 'nonce=""', response.text)
                    == re.sub(r'nonce="[^"]*"', 'nonce=""', missing.text),
                    f"{role}: uniform 404 {wid}{suffix}",
                )
                check("PRIVATE-PROMPT-372" not in response.text, f"{role}: prompt hidden")
            check(api.get(f"/api/workflows/{wid}").status_code == 404, f"{role}: API detail")
        for path in (
            "/workflows",
            "/workflows?archived=all",
            "/workflows/building-blocks",
            f"/customers/{cid}",
            f"/workflow-groups/{gid}",
            "/api/workflows",
            f"/api/workflow-groups/{gid}",
        ):
            response = (api if path.startswith("/api/") else c).get(path)
            check(
                response.status_code == (403 if path == "/workflows/building-blocks" else 200),
                f"{role}: list {path}: {response.status_code}",
            )
            for wid, name in hidden:
                check(name not in response.text, f"{role}: hidden name {wid} on {path}")
                if not path.startswith("/api/"):
                    check(
                        f'href="/workflows/{wid}"' not in response.text,
                        f"{role}: hidden link {wid} on {path}",
                    )
        from app.web.queries import _applicable_workflows

        with Session(engine) as s:
            offered = _applicable_workflows(s, Project(id=999999))
            check(
                not {w.id for w in offered}.intersection(wid for wid, _ in hidden),
                f"{role}: run picker",
            )
        response = c.get("/workflows")
        group_row = next(row for row in response.context["rows"] if row["kind"] == "group")
        check(group_row["variants"] == 0 and group_row["steps"] == 0, f"{role}: group rollup")
        response = api.get("/api/workflow-groups")
        check(
            next(g for g in response.json()["groups"] if g["id"] == gid)["variants"] == 0,
            f"{role}: API group count",
        )
        check(c.get(f"/workflows/{public_id}").status_code == 200, f"{role}: global still visible")
        # The remote CLI transport must observe the same filtered API payloads.
        import httpx

        from app.cli._common import CliError
        from app.cli._remote import RemoteClient

        def dispatch(request, api=api):
            response = api.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers={"Content-Type": "application/json"},
            )
            return httpx.Response(response.status_code, content=response.content)

        remote = RemoteClient("http://127.0.0.1", raw, transport=httpx.MockTransport(dispatch))
        check(not remote.get(f"/api/workflow-groups/{gid}")["variants"], f"{role}: CLI group")
        try:
            remote.get(f"/api/workflows/{hidden[0][0]}")
        except CliError as exc:
            check("not found" in str(exc).lower(), f"{role}: CLI 404")
        else:
            check(False, f"{role}: CLI detail denied")
        # Keys-hub launch (API, remote CLI and MCP all POST this route): the user is a
        # member of key project K but NOT of private project P. A hidden workflow id
        # must be the SAME 404 as a made-up id, and must launch nothing.
        with Session(engine) as s:
            access.add_member(s, kid, user.email, "admin")
        launch = f"/api/projects/{kid}/keys/translate"
        _, kraw = apikeys.create_key(user.id, "keys-launch", scopes="keys")
        kapi = TestClient(app, headers={"X-Loc-Token": kraw})
        kremote = RemoteClient(
            "http://127.0.0.1", kraw, transport=httpx.MockTransport(partial(dispatch, api=kapi))
        )
        for wid, _name in hidden:
            before = run_count(wid)
            response = kapi.post(launch, json={"workflow_id": wid, "locale": "de-DE"})
            missing = kapi.post(launch, json={"workflow_id": 999999, "locale": "de-DE"})
            # A viewer is refused by the write gate (403) before any workflow lookup.
            want = 403 if role == Role.VIEWER else 404
            check(
                response.status_code == want, f"{role}: keys launch {wid}: {response.status_code}"
            )
            check(response.content == missing.content, f"{role}: keys launch uniform {wid}")
            check(run_count(wid) == before, f"{role}: keys launch created a run {wid}")
        try:
            kremote.post(launch, {"workflow_id": hidden[0][0], "locale": "de-DE"})
        except CliError as exc:
            check(
                ("does not allow" if role == Role.VIEWER else "not found") in str(exc).lower(),
                f"{role}: CLI keys launch: {exc}",
            )
        else:
            check(False, f"{role}: CLI keys launch denied")
        with Session(engine) as s:
            access.add_member(s, pid, user.email, "admin")
        before = run_count(public_id)
        response = kapi.post(launch, json={"workflow_id": public_id, "locale": "de-DE"})
        # A visible (global) workflow still launches; a viewer is refused by the write gate.
        want = 403 if role == Role.VIEWER else 200
        check(response.status_code == want, f"{role}: keys launch global: {response.status_code}")
        check((run_count(public_id) > before) == (want == 200), f"{role}: keys launch global ran")
        response = kapi.post(launch, json={"workflow_id": hidden[0][0], "locale": "de-DE"})
        check(response.status_code != 404, f"{role}: member keys launch: {response.status_code}")
        for wid, _name in hidden:
            check(c.get(f"/workflows/{wid}").status_code == 200, f"{role}: member web")
            check(api.get(f"/api/workflows/{wid}").status_code == 200, f"{role}: member API")
        with Session(engine) as s:
            access.remove_member(s, pid, user.id)
        check(c.get(f"/workflows/{hidden[0][0]}").status_code == 404, f"{role}: revocation")
        settings.set_many({"project_scoping": "0"})
        check(c.get(f"/workflows/{hidden[0][0]}").status_code == 200, f"{role}: scoping off")
        settings.set_many({"project_scoping": "1"})

    linguist = login("linguist", "linguist")
    check(linguist.get(f"/workflows/{hidden[0][0]}").status_code == 200, "internal linguist")
    bound = security.create_user(
        "bound@example.test", "Bound linguist", Role.LINGUIST, phrase, customer_id=cid
    )
    bound_client = login(bound.email, phrase)
    blocks = bound_client.get("/workflows/building-blocks")
    check(blocks.status_code == 200, "bound linguist building blocks")
    check(all(name not in blocks.text for _, name in hidden), "building block project ring")
    check(blocks.context["bindings"][prompt_id]["workflows"] == 0, "template binding count")
    check(blocks.context["bindings"][prompt_id]["steps"] == 0, "template step count")
    check(blocks.context["bindings"][prompt_id]["hidden"] == 0, "template hidden count")
    settings.set_many({"security_auth_required": "0"})
    check(TestClient(app).get(f"/workflows/{hidden[0][0]}").status_code == 200, "auth off")
    assert not failures, "\n".join(failures)
    print("WORKFLOW PROJECT VISIBILITY OK")


if __name__ == "__main__":
    main()
