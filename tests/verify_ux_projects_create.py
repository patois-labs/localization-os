"""Project page visual contract, disposable fixtures and real Chromium interaction.

The golden tests/fixtures/ux_projects_forms.json pins every form contract (action, method,
encoding, field names, values, states). Forms that every page shares (the New project dialog)
are stored once under "shared"; each page lists only its own forms, one form per line.
Regenerate it ONLY after an intentional form change, from the current templates:
    UX_PROJECTS_FORMS_WRITE=1 .venv/Scripts/python.exe tests/verify_ux_projects_create.py
"""

import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

source = (ROOT / "app/templates/project.html").read_text(encoding="utf-8")
assert "m.tabs(" in source, "Project sections must use the shared tabs macro"
assert "project_sections.js" in source

OWNED = [
    "projects",
    "project",
    "customers",
    "customer",
    "project-remove-locale",
    "project-delete",
    "project-key",
    "project-key-delete",
]
GOLDEN = ROOT / "tests/fixtures/ux_projects_forms.json"


def canon(forms):
    return sorted(
        json.dumps(f, sort_keys=True, ensure_ascii=False, separators=(",", ":")) for f in forms
    )


def write_golden(actual_forms):
    names = list(actual_forms)
    first = canon(actual_forms[names[0]])
    shared = [f for f in first if all(f in canon(actual_forms[n]) for n in names)]
    nl = chr(10)
    lines = ['{"shared":[']
    lines.append(("," + nl).join(shared))
    lines.append('],"pages":{')
    entries = []
    for n in names:
        rest = [f for f in canon(actual_forms[n]) if f not in shared]
        entries.append(json.dumps(n) + ":[" + nl + ("," + nl).join(rest) + nl + "]")
    lines.append(("," + nl).join(entries))
    lines.append("}}" + nl)
    GOLDEN.write_bytes(nl.join(lines).encode("utf-8"))


FORM_CONTRACT = """() => Array.from(document.forms).map(f => ({
  action:f.getAttribute('action'), method:f.method, enctype:f.enctype,
  fields:Array.from(f.elements).filter(e=>e.name).map(e=>({
    tag:e.tagName, name:e.name, type:e.type, id:e.id,
    value:e.name==='csrf_token'?'CSRF':e.value,
    checked:e.checked, required:e.required, disabled:e.disabled,
    options:e.options?Array.from(e.options).map(o=>[o.value,o.selected,o.textContent.trim()]):null
  }))
})).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)))"""

with fresh_fixture() as (client, pages, _):
    from app.services import settings

    selected = {name: pages[name] for name in OWNED}
    rendered = render_pages(client, selected)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        writes = []
        override = {}

        route = install_offline_route(
            context,
            client,
            stubs={"/workflows/models": {"models": []}},
            blocked=writes,
            overrides=override,
            follow_redirects=False,
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def visit(path):
            page.goto("about:blank")
            response = page.goto("http://ux.local" + path, wait_until="networkidle")
            assert response.status == 200, (path, response.status)

        # Form destinations, field ownership and defaults from the pre-build templates.
        contracts = json.loads(GOLDEN.read_text(encoding="utf-8"))
        contract_page = browser.new_page(java_script_enabled=False)
        actual_forms = {}
        for name, html in rendered.items():
            contract_page.set_content(html)
            actual_forms[name] = contract_page.evaluate(FORM_CONTRACT)
        if os.environ.get("UX_PROJECTS_FORMS_WRITE") == "1":
            write_golden(actual_forms)
            contracts = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for name, actual in actual_forms.items():
            expected = contracts["shared"] + contracts["pages"][name]
            assert canon(actual) == canon(expected), ("Form contract changed", name)
        contract_page.close()
        for name in OWNED:
            visit(pages[name])
            assert page.locator("main h1").count() == 1, name
            assert page.locator("#new-project-dialog").count() == 1, name
            assert page.locator('#new-project-dialog form[action="/projects"]').count() == 1
            assert page.locator("main .hero").count() == 0
            assert (
                page.locator("main h1").inner_text()
                != page.locator(".topbar2 .crumbs").inner_text().strip()
            )
            ids = page.locator("[id]").evaluate_all("(es)=>es.map(e=>e.id)")
            assert len(ids) == len(set(ids)), (name, "duplicate IDs")
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    name,
                    width,
                )
            page.set_viewport_size({"width": 1440, "height": 900})
            page.evaluate('document.documentElement.style.zoom="2"')
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                name,
                "200% zoom",
            )
            page.evaluate('document.documentElement.style.zoom=""')
        visit(pages["projects"] + "?q=ux-fixture-no-match")
        assert page.locator("main .empty-state").filter(has_text="No projects match").is_visible()
        visit(pages["project"])
        page.evaluate(
            'window.originalCards = Array.from(document.querySelectorAll("[data-card]")); '
            'window.originalLayout = document.getElementById("page-layout")'
        )
        for label, key in [
            ("Languages", "langs"),
            ("Translation Memory", "tm"),
            ("Glossary", "glossary"),
            ("Documents", "documents"),
        ]:
            page.get_by_role("tab", name=label, exact=True).click()
            assert page.locator('[data-card="' + key + '"]').is_visible(), label
            assert page.evaluate(
                'window.originalLayout === document.getElementById("page-layout") && '
                "window.originalCards.every(e=>e.isConnected && originalLayout.contains(e))"
            )
        page.set_viewport_size({"width": 320, "height": 844})
        region = page.locator('[data-card="documents"] .data-table-region')
        assert region.get_attribute("aria-label") and region.get_attribute("tabindex") == "0"
        region.focus()
        page.keyboard.press("ArrowRight")
        # Native key scrolling animates briefly. Poll through CDP evaluation;
        # wait_for_function's injected eval is rejected by the real page CSP.
        for _ in range(20):
            if region.evaluate("e => e.scrollLeft") > 0:
                break
            page.wait_for_timeout(50)
        assert region.evaluate("e => e.scrollLeft") > 0
        page.set_viewport_size({"width": 1440, "height": 900})
        page.get_by_role("tab", name="Languages", exact=True).focus()
        page.keyboard.press("ArrowRight")
        assert (
            page.get_by_role("tab", name="Translation Memory", exact=True).get_attribute(
                "aria-selected"
            )
            == "true"
        )
        page.keyboard.press("End")
        assert (
            page.get_by_role("tab", name="Settings", exact=True).get_attribute("aria-selected")
            == "true"
        )
        # Native validation reveals a hidden panel and keeps typed values.
        page.evaluate('document.getElementById("project-glossary-term").value="Draft term"')
        page.evaluate('document.getElementById("tm-import-file").form.requestSubmit()')
        assert page.locator('[data-card="tm"]').is_visible()
        assert page.locator("#project-glossary-term").input_value() == "Draft term"
        visit(pages["project"] + "#pair-providers")
        assert (
            page.get_by_role("tab", name="Settings", exact=True).get_attribute("aria-selected")
            == "true"
        )
        # Connected projects T3: the settings toolbar never moves when a button opens.
        SETTINGS_BOXES = (
            "() => Array.from(document.querySelectorAll("
            "'.project-settings .edit-row > details > summary,"
            " .project-settings .edit-danger > *'))"
            ".map(e => { const r = e.getBoundingClientRect();"
            " const o = e.closest('.edit-row').getBoundingClientRect();"
            " return [e.textContent.trim(), Math.round(r.x - o.x), Math.round(r.y - o.y),"
            " Math.round(r.width), Math.round(r.height)]; })"
        )
        DANGER = ("Archive project", "Delete project…")
        for width in (1440, 375):
            page.set_viewport_size({"width": width, "height": 900})
            visit(pages["project"] + "#pair-providers")
            page.evaluate(
                'document.querySelectorAll(".project-settings details[open]")'
                ".forEach(d => d.open = false)"
            )
            closed = page.evaluate(SETTINGS_BOXES)
            assert len(closed) >= 7, closed
            edits = [b for b in closed if b[0] not in DANGER]
            danger = [b for b in closed if b[0] in DANGER]
            assert len(danger) == 2, closed
            tops = {b[2] for b in edits}
            if width == 1440:
                assert len(tops) == 1, ("toolbar buttons share one row", closed)
                assert max(b[3] for b in closed) < 220, ("buttons stay natural width", closed)
            toolbar_bottom = max(b[2] + b[4] for b in edits)
            count = page.locator(".project-settings .edit-row > details").count()
            for i in range(count):
                summary = page.locator(".project-settings .edit-row > details > summary").nth(i)
                summary.scroll_into_view_if_needed()
                summary.click()
                after = page.evaluate(SETTINGS_BOXES)
                # No edit button moves; the danger group keeps its place in its row and only
                # follows the opened form down (same x and size).
                assert [b for b in after if b[0] not in DANGER] == edits, (
                    width,
                    i,
                    "toolbar moved",
                )
                after_danger = [b for b in after if b[0] in DANGER]
                assert [(b[0], b[1], b[3], b[4]) for b in after_danger] == [
                    (b[0], b[1], b[3], b[4]) for b in danger
                ], (width, i, "danger group shifted sideways")
                assert page.locator(".project-settings .edit-row > details[open]").count() == 1
                assert summary.evaluate("e => e.parentElement.open")
                opened = page.locator(
                    ".project-settings .edit-row > details[open] > :not(summary)"
                ).first
                box = opened.bounding_box()
                card = page.locator(".project-settings .edit-row").bounding_box()
                form_top = box["y"] - card["y"]
                form_bottom = form_top + box["height"]
                assert box["height"] > 0, (width, i, "form is visible")
                assert form_top >= toolbar_bottom - 1, (width, i, "form opens below toolbar")
                assert form_bottom <= min(b[2] for b in after_danger) + 1, (
                    width,
                    i,
                    "form opens above the Archive/Delete group",
                )
                assert box["width"] >= card["width"] - 2, (i, "form spans the card")
                summary.click()
                assert page.evaluate(SETTINGS_BOXES) == closed, (width, i, "toolbar moved on close")
        page.set_viewport_size({"width": 1440, "height": 900})
        visit(pages["project"] + "?err=glossary_add")
        assert page.locator("#project-glossary-term").is_visible()
        # Cross-section rows stay the same rows. No persistence request on tab changes.
        blob = re.search(
            r'(<script\b[^>]*id="page-layout-data"[^>]*>)(.*?)(</script>)',
            rendered["project"],
            re.S,
        )
        assert blob
        data = json.loads(blob[2])
        data.update(
            saved=True, rows=[["tm", "documents"], ["glossary", "langs"], ["overview", "upload"]]
        )
        override[pages["project"]] = (
            rendered["project"][: blob.start(2)]
            + json.dumps(data)
            + rendered["project"][blob.end(2) :]
        ).encode()
        visit(pages["project"])
        before = page.locator(".layout-row").evaluate_all(
            '(rows)=>rows.map(r=>Array.from(r.querySelectorAll("[data-card]")).map(c=>c.dataset.card))'
        )
        page.get_by_role("tab", name="Glossary", exact=True).click()
        page.locator("[data-layout-edit]").click()
        assert page.locator("#page-layout").get_attribute("class").find("layout-editing") >= 0
        assert page.locator("#page-layout [data-card][hidden]").count() == 0
        page.locator('[data-layout-action="up"][data-layout-card="langs"]').click()
        moved = page.locator(".layout-row").evaluate_all(
            '(rows)=>rows.map(r=>Array.from(r.querySelectorAll("[data-card]")).map(c=>c.dataset.card))'
        )
        assert moved != before
        page.locator('[data-layout-bar="cancel"]').click()
        after = page.locator(".layout-row").evaluate_all(
            '(rows)=>rows.map(r=>Array.from(r.querySelectorAll("[data-card]")).map(c=>c.dataset.card))'
        )
        assert before == after
        assert page.locator('[data-card="glossary"]').is_visible()
        override.clear()
        for host in ("new-project", "new-project-customer"):
            visit(pages[host])
            assert page.locator("#new-project-dialog").is_visible()
            assert (
                page.locator('#new-project-dialog select[name="default_provider"]').input_value()
                == "claude"
            )
            assert (
                page.locator(
                    '#new-project-dialog select[name="segmentation_profile_id"]'
                ).input_value()
                == ""
            )
            assert page.locator(
                '#new-project-dialog input[name="provider_mode"][value="uniform"]'
            ).is_checked()
            radio = page.locator('#new-project-dialog input[name="project_type"][value="document"]')
            assert radio.bounding_box()["width"] < 40
            page.keyboard.press("Escape")
            assert not page.locator("#new-project-dialog").is_visible()
        visit(pages["project-key"])
        assert page.get_by_role("link", name="Open the key grid").is_visible()
        assert not page.locator("#doc-upload-form").count()
        settings.set_many({"key_projects_enabled": "0"})
        settings.invalidate_cache()
        visit(pages["project-key"])
        assert not page.get_by_role("link", name="Open the key grid").count()
        assert "turned off on this install" in page.locator("main").inner_text()
        settings.set_many({"key_projects_enabled": "1"})
        settings.invalidate_cache()
        # Progressive fallback uses the same fields once, including New project.
        nojs = browser.new_context(java_script_enabled=False)
        nojs.route("**/*", route)
        plain = nojs.new_page()
        plain.goto("http://ux.local" + pages["project"], wait_until="networkidle")
        for key in ("documents", "langs", "tm", "glossary"):
            assert plain.locator('[data-card="' + key + '"]').is_visible()
        # The no-JS New project form is scoped to its own pages: never inline on others.
        assert not plain.locator('#new-project-dialog input[name="name"]').first.is_visible()
        for path in (pages["projects"], pages["customer"]):
            plain.goto("http://ux.local" + path, wait_until="networkidle")
            assert plain.locator('#new-project-dialog input[name="name"]').first.is_visible(), path
        nojs.close()
        long_name = "Atlas international launch / 日本語 / العربية / " * 5
        response = client.post(
            pages["project"] + "/rename", data={"name": long_name}, follow_redirects=False
        )
        assert response.status_code == 303
        visit(pages["project"])
        page.set_viewport_size({"width": 320, "height": 844})
        assert page.locator("main h1").inner_text() == long_name.strip()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("tab", name="Settings", exact=True).click()
        active = page.get_by_role("tab", name="Settings", exact=True).bounding_box()
        assert active["x"] >= 0 and active["x"] + active["width"] <= 320
        page.set_viewport_size({"width": 1440, "height": 900})
        # Authenticated manager sees all six sections; a member viewer keeps the
        # route's existing restricted action set. No auth predicate is rewritten.
        from app.models import Role
        from app.services import security
        from tests._csrf import csrf_for

        fixture_password = "offline-project-fixture"
        security.create_user(
            "ux-admin", "UX Admin", Role.ADMIN, fixture_password, enforce_policy=False
        )
        security.create_user(
            "ux-viewer", "UX Viewer", Role.VIEWER, fixture_password, enforce_policy=False
        )
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()

        def login(who):
            csrf_for(client)
            response = client.post(
                "/auth/login",
                data={"email": who, "password": fixture_password, "next": "/"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            csrf_for(client)

        login("ux-admin")
        visit(pages["project"])
        assert page.get_by_role("tab").all_text_contents() == [
            "Documents",
            "Languages",
            "Translation Memory",
            "Glossary",
            "Members",
            "Settings",
        ]
        page.get_by_role("tab", name="Members", exact=True).click()
        assert page.locator('[data-card="members"]').is_visible()
        assert page.get_by_role("button", name="Add member", exact=True).is_visible()
        response = client.post(
            pages["project"] + "/members", data={"email": "ux-viewer"}, follow_redirects=False
        )
        assert response.status_code == 303
        login("ux-viewer")
        visit(pages["project"])
        viewer_ids = page.locator("[id]").evaluate_all("(es)=>es.map(e=>e.id)")
        assert len(viewer_ids) == len(set(viewer_ids)), "viewer duplicate IDs"
        # Configuration facts render exactly once for a read-only user.
        assert page.locator("main .fact-lbl", has_text="Content type").count() == 1
        assert page.locator("section.card.project-header").count() == 1
        page.get_by_role("tab", name="Members", exact=True).click()
        assert page.locator('[data-card="members"]').is_visible()
        assert not page.get_by_role("button", name="Add member", exact=True).count()
        page.get_by_role("tab", name="Settings", exact=True).click()
        assert not page.get_by_role("button", name="Rename", exact=True).count()
        assert not page.get_by_role("link", name="Delete project", exact=False).count()
        assert not writes, writes
        assert not errors, errors
        browser.close()
print("PROJECT SECTIONS OK")
