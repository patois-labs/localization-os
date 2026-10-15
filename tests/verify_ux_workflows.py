"""Workflow page component, form and keyboard regression contracts.

Documented in docs/architecture/browser-surface.md.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

PAGES = (
    "workflows",
    "workflows-new",
    "workflow",
    "workflow-empty",
    "workflow-plan",
    "workflow-group",
    "building-blocks",
    "routing",
    "workflow-author-review",
    "workflow-run-graph",
)
TEMPLATES = (
    "workflows",
    "workflows_new",
    "workflow_builder",
    "workflow_author_review",
    "workflow_plan",
    "workflow_group",
    "building_blocks",
    "routing",
)
# Captured before migration: submission destinations, field attributes, default and
# disabled options, labels, confirmation and busy hooks must remain byte-equivalent.
contract = json.loads(
    (ROOT / "tests/fixtures/ux_workflow_controls.json").read_text(encoding="utf-8")
)
for name, expected in contract.items():
    source = (ROOT / "app/templates" / (name + ".html")).read_text(encoding="utf-8")
    actual = [
        re.sub(r"\s+", " ", tag)
        for tag in re.findall(
            r"<(?:form|input|select|option|textarea|button|label)\b[^>]*>", source
        )
    ]
    assert actual == expected, (name, "submission/label contract changed")
for name in TEMPLATES:
    source = (ROOT / "app/templates" / (name + ".html")).read_text(encoding="utf-8")
    assert "m.page_header(" in source, name
    assert 'class="hero' not in source, name


def settle(page, polls=100):
    """Return once two consecutive reads of the document width agree (layout condition).

    Plain evaluate calls are used because the reflow context runs with JavaScript disabled,
    where wait_for_function and requestAnimationFrame callbacks never run.
    """
    read = "document.documentElement.scrollWidth + ':' + window.innerWidth"
    last = page.evaluate(read)
    for _ in range(polls):
        page.wait_for_timeout(25)
        now = page.evaluate(read)
        if now == last:
            return
        last = now
    raise AssertionError("layout did not settle")


with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    rendered = render_pages(client, {name: pages[name] for name in PAGES})
    browser = pw.chromium.launch(channel="chrome")
    page = browser.new_page()
    for name, html in rendered.items():
        page.set_content(html)
        assert page.locator("main h1").count() == 1, name
        ids = page.locator("[id]").evaluate_all("els => els.map(e => e.id)")
        assert len(ids) == len(set(ids)), (name, "duplicate IDs")
        assert page.locator("form form").count() == 0, name
        if name != "workflow-run-graph":
            assert page.locator("main .hero").count() == 0, name
        for region in page.locator("main .data-table-region, main .wf-graph-region").all():
            assert region.get_attribute("aria-label"), name
            region.focus()
            assert region.evaluate("e => e === document.activeElement"), name
        if name in ("workflow", "workflow-empty"):
            assert page.locator('form[action$="/steps"] [name="step_type"]').count() == 1
            assert page.locator("#system-prompt").count() == 1
            assert page.locator('form[action$="/copy-to-pairs"] [name="adapt"]').count() == 1
        if name == "workflow":
            move = page.locator('form[action$="/move"]').first
            assert move.locator('[name="position"]').input_value() == "1"
            destination = move.get_attribute("action")
            # The shared seed (branch 7) gives this workflow an in-flight run, and the
            # server refuses a move while one is in flight (400); finish runs in the
            # disposable fixture first so the move form itself is what is exercised.
            from sqlmodel import Session, select

            from app.db import engine
            from app.models import RunStatus, WorkflowRun

            with Session(engine) as session:
                for run in session.exec(select(WorkflowRun)).all():
                    run.status = RunStatus.COMPLETED
                    session.add(run)
                session.commit()
            response = client.post(destination, data={"position": "1"}, follow_redirects=False)
            assert response.status_code == 303
            updated = client.get(pages["workflow"]).text
            page.set_content(updated)
            assert page.locator("#wfStepList .step-head strong").all_text_contents() == [
                "Final review",
                "Human review",
            ]
        if name == "workflow-plan":
            assert page.get_by_role("button", name="Preview without running").count() == 1
            assert page.locator('main form[method="post"]').count() == 0
        if name == "workflow-author-review":
            assert page.locator('form[action="/workflows/author/confirm"]').count() == 1
            assert page.locator('[name="step_count"]').input_value() == "1"
        if name in ("workflows", "building-blocks"):
            assert page.locator('.wf-area-tabs a[href="/workflows"]').inner_text() == "Dashboard"
            assert (
                page.locator('.wf-area-tabs a[href="/workflows/building-blocks"]').inner_text()
                == "Building blocks"
            )
        if name == "workflow-run-graph":
            assert page.locator("#runGraphCard").count() == 1
            fragment_path = pages[name] + "/graph-fragment"
            fragment = client.get(fragment_path)
            assert fragment.status_code == 200
            page.set_content(fragment.text)
            assert page.locator("#runGraphCard").count() == 1
            assert page.locator('.wf-graph-region[tabindex="0"]').count() == 1
            assert page.locator("#wfgText").count() == 1
    # No-JS fallback and reflow: use the same rendered fixtures and actual styles.
    context = browser.new_context(java_script_enabled=False)

    route = install_offline_route(
        context,
        client,
        documents={"/fixture/" + n: h for n, h in rendered.items()},
        forward_headers=False,
    )
    responsive = context.new_page()
    for name in PAGES:
        if name == "workflow-run-graph":
            continue  # The host page is owned by ux/review-runs.
        responsive.set_viewport_size({"width": 320, "height": 844})
        responsive.goto("http://ux.local/fixture/" + name, wait_until="load")
        settle(responsive)
        overflow = responsive.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, (name, "320px", overflow)
        responsive.set_viewport_size({"width": 1440, "height": 900})
        responsive.evaluate("document.body.style.zoom = '2'")
        # Wait until the measured width is stable across polls (layout condition, no fixed sleep).
        settle(responsive)
        overflow = responsive.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, (name, "200%", overflow)
    context.close()
    browser.close()
print("UX WORKFLOWS OK")
