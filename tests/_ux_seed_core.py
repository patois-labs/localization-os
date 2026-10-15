"""Offline UX seed: customers, the shared project, workflows, routing, advanced run.

Documented in: DESIGN.md. Application modules are imported inside the functions, after
fresh_fixture() has pointed the app at its disposable data directory.
"""


def make_post(client):
    """The form-POST helper every seed uses: expects a 303, returns the new row's id segment."""

    def post(path, data, files=None):
        response = client.post(path, data=data, files=files, follow_redirects=False)
        assert response.status_code == 303, (path, response.status_code, response.text[:200])
        return response.headers["location"].split("?")[0].rstrip("/").split("/")[-1]

    return post


def seed_core(post):
    """Customer, project, document, glossary, workflows, routing and budgets."""
    post("/customers", {"name": "Northstar Labs"})
    project = post(
        "/projects",
        {
            "name": "Atlas product launch",
            "source_locale": "en-US",
            "target_locales": ["fr-FR", "de-DE"],
            "default_provider": "claude",
            "customer_id": "1",
        },
    )
    document = post(
        f"/projects/{project}/documents",
        {"mode": "segmented"},
        {
            "file": (
                "getting-started.txt",
                b"Welcome to Atlas.\n\nStart your first project.",
                "text/plain",
            )
        },
    )
    glossary = post("/glossaries", {"name": "Atlas terminology", "customer_id": "1"})
    post(f"/glossaries/{glossary}/pairs", {"source_locale": "en-US", "target_locale": "fr-FR"})
    post(
        f"/glossaries/{glossary}/terms/add",
        {
            "source_term": "project",
            "target_term": "projet",
            "source_locale": "en-US",
            "target_locale": "fr-FR",
        },
    )
    workflow = post("/workflows", {"name": "Atlas review"})
    post(f"/workflows/{workflow}/steps", {"name": "Human review", "step_type": "human_review"})
    # existing forms only, no engine calls or workers.
    post(f"/workflows/{workflow}/steps", {"name": "Final review", "step_type": "human_review"})
    group = post("/workflow-groups", {"name": "Atlas multilingual review"})
    post(
        f"/workflow-groups/{group}/add",
        {
            "workflow_id": workflow,
            "source_locale": "en-US",
            "target_locale": "fr-FR",
            "make_default": "1",
        },
    )
    empty_workflow = post("/workflows", {"name": "Untitled pipeline"})
    post(
        "/templates/prompts",
        {"name": "Atlas tone", "body": "Keep {text} concise in {target_locale}."},
    )
    # The rule names no project: a rule that references a project blocks its deletion,
    # which would turn the shared project-delete fixture into its "Cannot delete" state.
    post(
        "/routing",
        {
            "priority": "100",
            "match_ext": "txt",
            "department": "Marketing",
            "project_id": "",
            "provider": "",
        },
    )
    post("/routing/budgets", {"department": "Marketing", "monthly_token_limit": "100000"})
    post("/routing/project-budgets", {"project_id": project, "monthly_usd_limit": "250"})
    return {
        "project": project,
        "document": document,
        "glossary": glossary,
        "workflow": workflow,
        "workflow_group": group,
        "empty_workflow": empty_workflow,
    }
