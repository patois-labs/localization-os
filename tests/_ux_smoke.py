"""Offline UX fixtures. Import application modules only after fresh_fixture().

Documented in: DESIGN.md. Later branches extend seed() and page_list().
Run this helper in a fresh process because application config is import-cached.
"""

import sys
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

from tests._ux_pages import RECORDED_RUN_ID
from tests._ux_seed_auth import seed_auth_pages
from tests._ux_seed_runs import seed_review_runs


class ShellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.main = self.nav = self.toggle = self.stylesheet = 0
        self.standalone = self.h1 = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.main += tag == "main" and a.get("id") == "main-content"
        self.nav += tag == "nav" and "side-nav" in a.get("class", "").split()
        # one "Match my computer" choice in the account menu per page (the toggle moved there)
        self.toggle += tag == "button" and a.get("data-theme-set") == "system"
        self.stylesheet += tag == "link" and a.get("href", "").startswith("/static/style.css")
        self.standalone += tag == "main" and "authcard" in a.get("class", "").split()
        self.h1 += tag == "h1"


def assert_shell(html):
    dom = ShellParser()
    dom.feed(html)
    assert (dom.main, dom.nav, dom.toggle, dom.stylesheet) == (1, 1, 1, 1)


_DROPPED_HEADERS = ("content-length", "content-encoding", "transfer-encoding")


def install_offline_route(
    target,
    client,
    *,
    documents=None,
    overrides=None,
    stubs=None,
    blocked=None,
    strict=False,
    follow_redirects=True,
    forward_headers=True,
):
    """Serve http://ux.local from the in-process TestClient and abort everything else.

    One shared handler for every UX browser suite (a Playwright page or context).
    Only GET requests to ux.local reach the app; any other request is aborted.

    documents: {path or path?query: html} served as 200 text/html instead of the app.
    overrides: {path: bytes} replaces the app's body for that GET path.
    stubs: {path: json-able} answers a POST to that path with 200 JSON, nothing sent.
    blocked: list that collects the URL of every aborted request.
    strict: with `documents`, only /static/ and the documents are served; others abort.
    follow_redirects: passed to the TestClient GET.
    forward_headers: send the app's response headers (minus length and encoding ones);
        when False only its content type is forwarded.
    Returns the handler, so a caller can also register it on a second context.
    """
    from urllib.parse import urlsplit

    # Keep the caller's dicts: a suite may fill `overrides` after installing the route.
    documents = {} if documents is None else documents
    overrides = {} if overrides is None else overrides
    stubs = {} if stubs is None else stubs

    def route(r):
        url = urlsplit(r.request.url)
        if url.netloc == "ux.local" and r.request.method == "POST" and url.path in stubs:
            r.fulfill(status=200, json=stubs[url.path])
            return
        if url.netloc != "ux.local" or r.request.method != "GET":
            if blocked is not None:
                blocked.append(r.request.url)
            r.abort()
            return
        path = url.path + ("?" + url.query if url.query else "")
        body = documents.get(path, documents.get(url.path))
        if body is not None:
            r.fulfill(status=200, content_type="text/html", body=body)
            return
        if strict and not url.path.startswith("/static/"):
            r.abort()
            return
        response = client.get(path, follow_redirects=follow_redirects)
        fulfilled = {
            "status": response.status_code,
            "body": overrides.get(url.path, response.content),
        }
        if forward_headers:
            fulfilled["headers"] = {
                k: v for k, v in response.headers.items() if k.lower() not in _DROPPED_HEADERS
            }
        else:
            fulfilled["content_type"] = response.headers.get("content-type", "text/plain")
        r.fulfill(**fulfilled)

    target.route("**/*", route)
    return route


def seed(client, connections=False, *, auth=False):
    """Seed through existing forms, without starting workers or translation.

    connections=True (opt-in, admin-connections pages) adds disconnected connector and
    vendor records. auth=True (opt-in, admin-auth-account pages) captures real auth
    responses, then restores the shared auth-off fixture.

    The per-area seeds live in tests/_ux_seed_*.py. They share state only through `ids` and
    the database, and the ORDER below is part of the fixture: later areas read earlier rows.
    """
    from tests._ux_seed_connections import seed_connections
    from tests._ux_seed_core import make_post, seed_core
    from tests._ux_seed_documents import seed_documents, seed_review_text, seed_tm_entry
    from tests._ux_seed_guidance import seed_guidance

    post = make_post(client)
    ids = seed_core(post)
    project, document, workflow = ids["project"], ids["document"], ids["workflow"]
    # language-assets-guidance fixtures (offline, no model calls)
    ids.update(seed_guidance(post, workflow))
    # documents/delivery fixtures never enqueue jobs or call a paid provider.
    ids.update(seed_documents(post, project, document, workflow))
    ids["tm_entry"] = seed_tm_entry(project)
    # real offline auth flows, including POST-only screens.
    ids["auth_pages"] = seed_auth_pages(client) if auth else {}
    # offline review surfaces, including FULL_TEXT recovery.
    ids["review_full_document"] = seed_review_text(post, project, document)
    ids["review_key_project"] = ids["key_project"]
    # shared offline seeds.
    if not connections:
        # Other branches' contract suites (dashboard, customer) assert the base fixture: opt in.
        return ids
    ids.update(seed_connections(post, workflow, document))
    return ids


def page_list(ids):
    """Stable names for --pages; one representative per major page family.

    The per-area groups live in tests/_ux_pages.py; the order below is the order of the dict.
    """
    from tests._ux_pages import (
        admin_pages,
        auth_pages,
        guidance_pages,
        projects_pages,
        review_editor_pages,
        shell_pages,
        tm_glossary_pages,
        workflow_pages,
    )

    pages = {
        # all owned pages, including POST-only previews.
        **projects_pages(ids),
        **review_editor_pages(ids),
        **tm_glossary_pages(ids),
        **workflow_pages(ids),
        **admin_pages(ids),
        **shell_pages(ids),
        **guidance_pages(ids),
        **auth_pages(ids),
    }
    # review-runs
    if "run" in ids:
        pages.update(
            {
                "run": f"/runs/{ids['run']}",
                "run-step-diff": f"/runs/{ids['run']}/step-diff",
                "batch": f"/batches/{ids['batch']}",
                "my-work": "/my-work",
                "queries": "/queries",
                "query-thread": f"/queries/{ids['query']}",
            }
        )
    if "connection" in ids:
        # only with seed(connections=True) (see seed()).
        pages.update(
            {
                "connectors-catalog": "/connectors/catalog",
                "connectors-manage": "/connectors/manage",
                "connector-type": "/connectors/airtable",
                "connection": f"/connectors/connection/{ids['connection']}",
                "vendors": "/vendors",
                "vendor-detail": f"/vendors/{ids['vendor']}",
                "vendor-work": f"/vendors/{ids['vendor_work']}",
            }
        )
    return pages


def render_pages(client, pages):
    rendered = {}
    for name, path in pages.items():
        # admin-auth-account
        if isinstance(path, str) and path.startswith("/__ux-fixtures/auth/"):
            html = client.ux_auth_pages[name]
            dom = ShellParser()
            dom.feed(html)
            assert dom.h1 == 1, name
            if dom.standalone:
                assert (dom.standalone, dom.stylesheet) == (1, 1), name
            else:
                assert_shell(html)
            rendered[name] = html
            continue
        # workflows POST-rendered review, offline and never saved
        if name == "workflow-author-review":
            response = client.post(
                "/workflows/author/confirm",
                data={
                    "name": "",
                    "description": "Review the launch copy.",
                    "step_count": "1",
                    "s0_name": "Review copy",
                    "s0_step_type": "human_review",
                    "s0_key": "review",
                    "s0_on_pass": "missing-step",
                },
                follow_redirects=False,
            )
            assert response.status_code == 200, response.status_code
            assert "nothing was saved" in response.text
            assert_shell(response.text)
            rendered[name] = response.text
            continue
        if name == "workflow-run-graph":
            # A recorded run is inserted directly, as in verify_run_view_graph; never executed.
            from sqlmodel import Session, select

            from app.db import engine
            from app.models import Document, RunStatus, Workflow, WorkflowRun

            with Session(engine) as session:
                if session.get(WorkflowRun, RECORDED_RUN_ID) is None:
                    workflow = session.exec(select(Workflow).order_by(Workflow.id)).first()
                    document = session.exec(select(Document).order_by(Document.id)).first()
                    session.add(
                        WorkflowRun(
                            id=RECORDED_RUN_ID,
                            workflow_id=workflow.id,
                            document_id=document.id,
                            target_locale="fr-FR",
                            status=RunStatus.COMPLETED,
                        )
                    )
                    session.commit()
                run = session.get(WorkflowRun, RECORDED_RUN_ID)
                assert run is not None and run.status == RunStatus.COMPLETED
        if name in ("setup", "setup-error"):
            # First-run setup renders only while the install has no users; the opt-in
            # auth seed creates real users, so render both pages as a fresh install would.
            from unittest.mock import patch

            from app.services import security

            with patch.object(security, "has_users", return_value=False):
                if name == "setup":
                    response = client.get(path, follow_redirects=False)
                else:
                    response = client.post(
                        "/auth/setup",
                        data={
                            "name": "Jane Admin",
                            "email": "jane@example.test",
                            "password": "x",
                        },
                    )
            assert response.status_code == (200 if name == "setup" else 400), (
                name,
                response.status_code,
            )
            if name == "setup":
                dom = ShellParser()
                dom.feed(response.text)
                assert (dom.standalone, dom.h1) == (1, 1)
            rendered[name] = response.text
            continue
        if name.startswith("dashboard-"):
            from app.web.deps import templates

            context = dict(client.get("/").context)
            if name == "dashboard-empty":
                context.update(rows=[], recent_runs=[], onboarding=None)
                context["summary"] = dict.fromkeys(context["summary"], 0)
            elif name == "dashboard-onboarding":
                from unittest.mock import patch

                from sqlmodel import Session

                from app.db import engine
                from app.models import Role, User
                from app.services import onboarding, security

                # Same predicates and seeded records as production; auth-off's
                # SYSTEM identity is replaced only while computing this fixture.
                user = User(id=999, email="ux-admin@example.test", role=Role.ADMIN)
                with (
                    Session(engine) as session,
                    patch.object(security, "auth_required", return_value=True),
                    patch.object(onboarding, "provider_is_live", return_value=False),
                ):
                    context["onboarding"] = onboarding.dashboard_panel(session, user)
                assert context["onboarding"]["done_count"] == 3
            elif name == "dashboard-attention":
                context["summary"] = {**context["summary"], "paused": 2, "my_open_queries": 1}
                context["recent_runs"] = [
                    {
                        "id": 1,
                        "workflow": "Atlas review",
                        "document": "getting-started.txt",
                        "status": "paused",
                    }
                ]
            rendered[name] = templates.get_template("dashboard.html").render(context)
            assert_shell(rendered[name])
            continue
        if name == "components":
            # Synthetic offline document, never registered as an application route.
            from app.web.deps import templates

            source = (Path(__file__).parent / "fixtures/ux_components.html").read_text(
                encoding="utf-8"
            )
            rendered[name] = templates.env.from_string(source).render()
            continue
        # submit only offline preview forms, never confirms.
        if isinstance(path, tuple):
            response = client.post(path[0], data=path[1], follow_redirects=False)
        elif name in ("context-ab", "style-ab"):
            response = client.post(
                path,
                data={"locale": "fr-FR", "provider": "mock", "max_segments": "2"},
                follow_redirects=False,
            )
        # language-assets-tm-glossary POST-only suggestion page
        elif name == "glossary-suggest":
            response = client.post(path, follow_redirects=False)
        else:
            response = client.get(path, follow_redirects=False)
        assert response.status_code == (404 if name == "error" else 200), (
            name,
            path,
            response.status_code,
            response.headers.get("location"),
        )
        if name not in ("error", "setup"):
            assert_shell(response.text)
        else:
            dom = ShellParser()
            dom.feed(response.text)
            assert (dom.standalone, dom.h1) == (1, 1)
        rendered[name] = response.text
    return rendered


@contextmanager
def fresh_fixture(review_runs=False, connections=False, *, auth=False):
    if "app.config" in sys.modules or "app.db" in sys.modules:
        raise RuntimeError("UX fixtures require a fresh process before application imports")
    from tests._bootstrap import _cleanup, init_env

    data_dir = init_env("loctms_ux_")
    try:
        from fastapi.testclient import TestClient

        from app import config
        from app.db import init_db
        from app.main import app
        from app.services import settings

        assert Path(config.DATA_DIR).resolve() == Path(data_dir).resolve()
        init_db()
        settings.set_many({"key_projects_enabled": "1"})
        settings.invalidate_cache()
        # No lifespan: background schedulers must not run for screenshots.
        client = TestClient(app)
        try:
            ids = seed(client, connections=connections, auth=auth)
            # admin-auth-account
            client.ux_auth_pages = ids["auth_pages"]
            if review_runs:  # review-runs fixture
                ids.update(seed_review_runs(client, ids))
            yield client, page_list(ids), data_dir
        finally:
            client.close()
    finally:
        _cleanup(data_dir)


if __name__ == "__main__":
    with fresh_fixture(review_runs=True, connections=True, auth=True) as (
        client,
        pages,
        data_dir,
    ):
        rendered = render_pages(client, pages)
        assert "Atlas product launch" in rendered["projects"]
        assert "Welcome to Atlas" in rendered["editor"]
        print(f"UX SMOKE OK: {len(rendered)} seeded pages with base shell")
