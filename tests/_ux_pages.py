"""Offline UX fixtures: the named page list, grouped by area. Order is part of the contract.

`page_list` in tests/_ux_smoke.py concatenates these groups in this order; suites that iterate
the dict (screenshots, render_pages) see the same names in the same order as before the split.
"""

# ux/workflows: id of the recorded (finished) run the run-graph fixture shows.
RECORDED_RUN_ID = 900


def projects_pages(ids):
    """Dashboard, projects, customers, documents, images, jobs, exports."""
    return {
        "dashboard": "/",
        "projects": "/projects",
        "projects-empty": "/projects?q=ux-fixture-no-match",
        "project": f"/projects/{ids['project']}",
        # Branch 05: every owned template and the dialog on both list/detail hosts.
        "customers": "/customers",
        "customer": "/customers/1",
        "new-project": "/projects#new",
        "new-project-customer": "/customers/1#new",
        "project-languages": f"/projects/{ids['project']}#project-languages",
        "project-memory": f"/projects/{ids['project']}#project-memory",
        "project-glossary": f"/projects/{ids['project']}#project-glossary",
        "project-settings": f"/projects/{ids['project']}#project-settings",
        "project-remove-locale": f"/projects/{ids['project']}/remove-locale/fr-FR",
        "project-delete": f"/projects/{ids['project']}/delete",
        "project-key": f"/projects/{ids['key_project']}",
        "project-key-delete": f"/projects/{ids['key_project']}/keys/{ids['key']}/delete",
        "document": f"/documents/{ids['document']}?locale=fr-FR",
        "project-keys": f"/projects/{ids['keys']}/keys",
        "images": "/images?customer=1",
        "image-detail": f"/images/{ids['image']}",
        "jobs": "/jobs",
        "exports": "/exports",
        "export-run": "/exports/ux-export",
        "context-ab": f"/documents/{ids['document']}/context-ab",
        "style-ab": f"/documents/{ids['document']}/style-ab",
        "editor": f"/documents/{ids['document']}/editor",
    }


def review_editor_pages(ids):
    """Review editor, LQA, paragraph recovery and delivery empties."""
    return {
        # review-editor
        "editor-de": f"/documents/{ids['document']}/editor?locale=de-DE",
        "lqa": f"/documents/{ids['document']}/lqa?locale=de-DE",
        "lqa-empty": f"/documents/{ids['review_full_document']}/lqa?locale=fr-FR",
        "paragraphs": f"/documents/{ids['review_full_document']}/paragraphs?locale=fr-FR",
        "paragraphs-unavailable": f"/documents/{ids['document']}/paragraphs?locale=de-DE",
        "delivery-empties": f"/documents/{ids['review_full_document']}/empties?locale=fr-FR",
        "delivery-empties-clear": f"/documents/{ids['document']}/empties?locale=de-DE",
        "delivery-empties-keys": f"/projects/{ids['review_key_project']}/keys/empties?locale=fr-FR",
    }


def tm_glossary_pages(ids):
    """Translation memory and glossary pages."""
    return {
        "glossary": f"/glossaries/{ids['glossary']}",
        # language-assets-tm-glossary pages
        "tm": "/tm?customer=1",
        "tm-picker": "/tm",
        "tm-empty": "/tm?customer=1&q=missing-fixture-entry",
        "tm-detail": f"/tm/{ids['tm_entry']}?customer=1",
        "glossaries": "/glossaries?customer=1",
        "glossary-suggest": f"/documents/{ids['document']}/glossary-suggest",
    }


def workflow_pages(ids):
    """Workflows, groups, routing and the recorded run graph."""
    return {
        "workflow": f"/workflows/{ids['workflow']}",
        # workflows pages
        "workflows": "/workflows",
        "workflows-empty": "/workflows?archived=only",
        "workflows-new": "/workflows/new?customer_id=1",
        "workflow-empty": f"/workflows/{ids['empty_workflow']}",
        "workflow-plan": f"/workflows/{ids['workflow']}/plan?doc_id={ids['document']}&locale=fr-FR",
        "workflow-group": f"/workflow-groups/{ids['workflow_group']}",
        "building-blocks": "/workflows/building-blocks",
        "routing": "/routing",
        "workflow-author-review": "/__ux-fixtures/workflow-author-review",
        # A finished run at a fixed id, inserted by render_pages so the shared dashboard
        # fixtures never list it. Not run 1: that is the seed's in-flight run (branch 7),
        # whose live event stream never ends under the offline screenshot client.
        "workflow-run-graph": f"/runs/{RECORDED_RUN_ID}",
    }


def admin_pages(ids):
    """AI setup, settings, security, users, connectors, reports."""
    return {
        "setup-ai": "/setup-ai",
        # admin-ai
        "jev-inspect": f"/setup-ai/engines/{ids['judge']}/inspect",
        "settings": "/settings",
        # admin-settings pages
        "languages-settings": "/settings/languages",
        "enterprise": "/settings/enterprise",
        "security": "/security",
        "users": "/users",
        "connectors": "/connectors",
        "reports": "/reports",
    }


def shell_pages(ids):
    """Help, setup, error and component fixtures."""
    return {
        "help": "/help",
        "help-card": "/help/documents",
        "setup": "/auth/setup",
        "setup-error": "/__ux-fixtures/setup-error",
        "dashboard-onboarding": "/__ux-fixtures/dashboard-onboarding",
        "dashboard-empty": "/__ux-fixtures/dashboard-empty",
        "dashboard-attention": "/__ux-fixtures/dashboard-attention",
        "error": "/ux-fixture-not-found",
        "components": "/__ux-fixtures/components",
    }


def guidance_pages(ids):
    """Style guides, content types, languages."""
    return {
        # language-assets-guidance pages
        "style-guides": "/style-guides",
        "style-guide-detail": f"/style-guides/{ids['guide']}",
        "content-types": "/content-types",
        "languages": "/languages",
    }


def auth_pages(ids):
    """Offline replay URLs for the captured auth screens (never app routes)."""
    return {
        # offline replay URLs, never app routes.
        **{name: "/__ux-fixtures/auth/" + name for name in ids["auth_pages"]},
    }
