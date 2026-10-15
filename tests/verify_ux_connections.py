"""Section 14 DOM, payload and keyboard regression. See the section 14 handoff.

The form/navigation contract is tests/fixtures/ux_connections_contracts.json. Regenerate it after
an intended markup change (review the diff before committing):
    UX_CONNECTIONS_WRITE=1 python tests/verify_ux_connections.py
"""

import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tests._ux_connections import PAGES, Contract
from tests._ux_golden import write_golden, write_requested
from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

GOLDEN = ROOT / "tests/fixtures/ux_connections_contracts.json"

with fresh_fixture(connections=True) as (client, pages, _):
    selected = {key: pages[key] for key in PAGES}
    rendered = render_pages(client, selected)
    if write_requested("UX_CONNECTIONS_WRITE"):
        snapshots = {name: Contract(html).snapshot() for name, html in rendered.items()}
        write_golden(GOLDEN, json.dumps(snapshots, indent=2) + chr(10))
    baseline = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for name, html in rendered.items():
        assert 'class="page-header"' in html, f"Shared page header missing: {name}"
        actual = Contract(html).snapshot()
        assert actual == baseline[name], f"Form/navigation contract changed: {name}\n" + "\n".join(
            difflib.unified_diff(
                json.dumps(baseline[name], indent=2).splitlines(),
                json.dumps(actual, indent=2).splitlines(),
            )
        )

    selected["vendor-work"] = pages["vendor-work"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        context = browser.new_context(reduced_motion="reduce", service_workers="block")

        route = install_offline_route(context, client)
        page = context.new_page()
        for name, path in selected.items():
            page.goto("http://ux.local" + path, wait_until="networkidle")
            main = page.locator("main")
            assert main.locator("h1").count() == 1, name
            assert main.locator(".page-header").count() == 1, name
            assert main.locator(".hero").count() == 0, name
            for password in main.locator('input[type="password"]').all():
                assert password.get_attribute("autocomplete") == "new-password", name
            ids = main.locator("[id]").evaluate_all("es => es.map(e => e.id)")
            assert len(ids) == len(set(ids)), (name, ids)
            heading = main.locator("h1").inner_text().strip()
            topbar = page.locator(".topbar2").inner_text()
            assert heading not in topbar, (name, heading)
            for width, font_size in ((1440, 16), (390, 16), (320, 16), (720, 32)):
                page.set_viewport_size({"width": width, "height": 900})
                page.evaluate(
                    "size => document.documentElement.style.fontSize = size + 'px'", font_size
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                ), (name, width, font_size)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("document.documentElement.style.fontSize = ''")
            for summary in main.locator("details > summary").all():
                summary.click()
            for region in main.locator(".data-table-region").all():
                assert region.get_attribute("aria-label"), name
                region.focus()
                assert region.evaluate("e => e === document.activeElement"), name
                page.keyboard.press("ArrowRight")
                page.wait_for_function(
                    "e => e.scrollWidth <= e.clientWidth || e.scrollLeft > 0",
                    arg=region.element_handle(),
                )
            for anchor in main.locator(".connections-section-nav a").all():
                target = anchor.get_attribute("href")
                assert page.locator(target).count() == 1, (name, target)
                anchor.focus()
                page.keyboard.press("Enter")
                assert page.locator(target).is_visible(), (name, target)
            if name == "connectors":
                assert main.locator('[href="/setup-ai"]').count() > 0
            if name == "connection":
                save = main.locator('form[data-browse="settings"]')
                assert save.get_attribute("action") == path
                assert save.get_by_role("button", name="Save", exact=True).count() == 1
                assert main.locator(f'form[action="{path}/sync"]').count() == 1
                assert save.locator("[data-busy-label]").count() == 0
                assert main.locator('select[name="every_minutes"]').input_value() == "0"
                assert main.locator('button:has-text("Register webhook")').is_disabled()
            if name == "vendor-detail":
                for control in main.locator('input[type="password"]').all():
                    assert control.input_value() == ""
                assert main.locator('input[name="requires_quote_approval"]').is_checked() is False
            if name == "vendor-work":
                for suffix in (
                    "/return",
                    "/quote",
                    "/approve-quote",
                    "/decline-quote",
                    "/reject-rework",
                ):
                    assert main.locator(f'form[action$="{suffix}"]').count() == 1, suffix
                assert main.get_by_role("link", name="Download XLIFF", exact=True).count() == 2

        # Render supported empty/read-only states from the same route contexts.
        # Only presentation flags/data are changed; no synthetic application route.
        from app.web.deps import templates

        scenarios = (
            ("vendors", "vendors.html", {"rows": []}, "No vendors yet."),
            (
                "vendor-detail",
                "vendor_detail.html",
                {"linked": [], "legs": []},
                "Not linked to any customer yet.",
            ),
            (
                "connector-type",
                "connector_type.html",
                {"connections": [], "can_manage": False},
                "connections yet.",
            ),
            (
                "connection",
                "connection.html",
                {"history": [], "can_manage": False},
                "No syncs yet.",
            ),
            (
                "connectors",
                "connectors.html",
                {"can_manage": False, "is_admin": False},
                "Read-only",
            ),
        )
        for name, template, overrides, message in scenarios:
            response = client.get(selected[name])
            ctx = dict(response.context)
            ctx.update(overrides)
            html = templates.get_template(template).render(ctx)
            page.set_content(html)
            assert page.locator("main h1").count() == 1
            assert message in " ".join(page.locator("main").text_content().split()), name
            if overrides.get("can_manage") is False:
                assert (
                    page.locator('main form[action$="/sync"], main form[data-browse]').count() == 0
                )
        # Existing vendor validation summary stays singular and targets the same input.
        page.goto("http://ux.local/vendors?err=vendor_name_required", wait_until="networkidle")
        assert page.locator("#vendor-create-name").count() == 1
        assert page.locator("#vendor-create-name").get_attribute("aria-invalid") == "true"
        # Native anchors and full forms remain available with JavaScript disabled.
        nojs = browser.new_context(
            java_script_enabled=False, viewport={"width": 390, "height": 844}
        )
        nojs.route("**/*", route)
        fallback = nojs.new_page()
        for name in ("connection", "vendor-detail", "vendor-work"):
            fallback.goto("http://ux.local" + selected[name], wait_until="networkidle")
            for anchor in fallback.locator(".connections-section-nav a").all():
                anchor.focus()
                fallback.keyboard.press("Enter")
                assert fallback.locator(anchor.get_attribute("href")).is_visible()
            assert fallback.locator("main form").count() > 0
        nojs.close()
        context.close()
        browser.close()

print("verify_ux_connections: seven pages, unchanged form contracts, reflow and keyboard OK")
