"""Direction A page layout DOM, payload and keyboard contracts; offline fixtures."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

NAMES = (
    "dashboard",
    "dashboard-empty",
    "dashboard-onboarding",
    "dashboard-attention",
    "help",
    "help-card",
    "setup",
    "setup-error",
)
BASELINE = json.loads(
    (ROOT / "tests/fixtures/ux_page_layout_contracts.json").read_text(encoding="utf-8")
)

LIVE = {}
with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    pages = {name: pages[name] for name in NAMES}
    rendered = render_pages(client, pages)
    browser = pw.chromium.launch(channel="chrome")
    context = browser.new_context(reduced_motion="reduce")

    route = install_offline_route(
        context, client, documents={p: rendered[n] for n, p in pages.items()}, forward_headers=False
    )
    page = context.new_page()
    checks = 0
    for name, path in pages.items():
        page.goto("http://ux.local" + path, wait_until="networkidle")
        assert page.locator("main h1").count() == 1, name
        assert page.locator("main .page-header").count() == 1, name
        assert not page.locator("main .hero").count(), name
        title = page.locator("main h1").inner_text()
        # Captured from HEAD's templates using these same offline states. Layout
        # may move groups, but no existing destination, identity or heading goes away.
        # Layout enhancement adds transient editing controls after load. The
        # immutable contract is the server-rendered content before enhancement.
        static = context.new_page()
        static.route("**/*", lambda r: r.abort())
        static.set_content(rendered[name])
        server_contract = static.locator("main").evaluate("""e => ({
          links: [...e.querySelectorAll('a[href]')].map(x => x.getAttribute('href')).sort(),
          ids: [...e.querySelectorAll('[id]')].map(x => x.id).sort(),
          headings: [...e.querySelectorAll('h1,h2,h3')].map(x => x.textContent.trim()).sort(),
          forms: [...e.querySelectorAll('form')].map(x =>
            [x.getAttribute('action'),x.getAttribute('method')]).sort(),
          fields: [...e.querySelectorAll('form [name]')].map(x => x.getAttribute('name'))
            .filter(n => n !== 'csrf_token')
        })""")
        LIVE[name] = server_contract
        if not os.environ.get("UX_PAGE_LAYOUTS_WRITE"):
            assert server_contract == BASELINE[name], (name, server_contract["fields"])
        static.close()
        if page.locator(".crumbs").count():
            assert title != page.locator(".crumbs").inner_text().strip(), name
        assert page.evaluate("""() => {
          const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
          return ids.length === new Set(ids).size;
        }"""), name
        if name.startswith("dashboard"):
            assert (
                page.locator("[data-layout-page]").get_attribute("data-layout-page") == "dashboard"
            )
            assert page.locator("[data-card]").evaluate_all(
                "es => es.map(e => e.dataset.card)"
            ) == ["running", "activity", "quick"]
            assert page.locator(".page-header-primary a").get_attribute("href") == "/projects#new"
            assert page.locator("main .btn-primary").count() == 1
        if name == "dashboard-empty":
            assert page.locator(".empty-state").count() == 2
            assert (
                page.locator("[data-card=running] .empty-state a").get_attribute("href")
                == "/projects#new"
            )
        if name == "dashboard-onboarding":
            assert page.locator("[data-onboarding-panel]").count() == 1
            assert page.locator("[data-tour-step]").count() == 4
            assert page.locator(".ob-step.is-done").count() == 3
            assert page.locator(".ob-dismiss-form").get_attribute("action") == "/onboarding/dismiss"
            assert page.locator(".ob-meter").get_attribute("aria-valuenow") == "3"
            assert page.locator(".ob-cta").get_attribute("href") == "/setup-ai"
            page.locator(".ob-summary").focus()
            page.keyboard.press("Enter")
            assert not page.locator(".ob-details").evaluate("e => e.open")
            page.keyboard.press("Enter")
        if name == "dashboard-attention":
            assert page.locator(".dash-attention a").count() == 2
            assert set(
                page.locator(".dash-attention a").evaluate_all(
                    "es => es.map(e => e.getAttribute('href'))"
                )
            ) == {"/my-work"}
            assert page.locator(".activity .badge").text_content().strip() == "paused"
            assert "page.jobs.status_" not in page.content()
        if name in ("setup", "setup-error"):
            form = page.locator("form[action='/auth/setup']")
            assert form.get_attribute("method") == "post"
            assert form.locator("input:not([type=hidden])").evaluate_all(
                "es => es.map(e => e.name)"
            ) == ["name", "email", "password"]
            assert form.locator("#setup-password").get_attribute("autocomplete") == "new-password"
            assert form.locator("#setup-email").get_attribute("required") is not None
            assert form.locator("#setup-password").input_value() == ""
            if name == "setup-error":
                assert form.locator("#setup-name").input_value() == "Jane Admin"
                assert form.locator("#setup-email").input_value() == "jane@example.test"
                assert form.locator("#setup-password").get_attribute("aria-invalid") == "true"
        if name == "help":
            for anchor in (
                "guides",
                "overview",
                "projects",
                "api",
                "api-authn",
                "api-index",
                "api-curl",
            ):
                assert page.locator("#" + anchor).count() == 1
            assert page.locator(".docs > .docs-body").count() == 1
            labels = page.locator("main [role=region]").evaluate_all(
                "es => es.map(e => e.getAttribute('aria-label'))"
            )
            assert labels and all(labels), "every help scroll region needs a label"
            assert len(labels) == len(set(labels)), "help scroll region labels must be unique"
            assert "Help & API" not in labels
            assert page.locator("#memory-capacity").evaluate("e => !!e.closest('.docs-body')")
        for width in (1440, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                name,
                width,
            )
            if name == "help":
                # Every region that really overflows is keyboard reachable, including
                # the ones rendered after help_scroll.js in the page source.
                page.wait_for_function("""() => [...document.querySelectorAll(
                  '[data-scroll-region]')].every(e =>
                  (e.scrollWidth > e.clientWidth + 1) === (e.getAttribute('tabindex') === '0'))""")
                if width == 390:
                    assert page.locator(
                        "[aria-label='Table: Host memory settings'][tabindex='0']"
                    ).count(), "trailing help table must be focusable at phone width"
            for region in page.locator("main [role=region][tabindex='0']").all():
                assert region.get_attribute("aria-label")
                region.focus()
                assert region.evaluate("e => e === document.activeElement")
                assert region.evaluate("e => getComputedStyle(e).outlineStyle != 'none'")
            checks += 1
        if name == "help":
            # Reference tables fill the column and method chips never wrap.
            page.set_viewport_size({"width": 1600, "height": 900})
            glossary_table = page.locator("[aria-label='Table: Glossary module'] table")
            table_width = glossary_table.evaluate("e => e.getBoundingClientRect().width")
            assert table_width > 900, ("glossary API table too narrow at 1600px", table_width)
            tall = page.locator(".table.api .method").evaluate_all(
                "es => es.filter(e => e.getBoundingClientRect().height > 1.6 * parseFloat("
                "getComputedStyle(e).lineHeight || 16)).length"
            )
            assert tall == 0, f"{tall} method chips wrap to more than one line"
            checks += 1
        if name == "dashboard":
            region = page.locator(".data-table-region")
            region.focus()
            page.keyboard.press("ArrowRight")
            page.wait_for_function("document.querySelector('.data-table-region').scrollLeft > 0")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("document.documentElement.style.zoom = '2'")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (name, "200%")
        page.evaluate("document.documentElement.style.zoom = ''")
    fallback_context = browser.new_context(
        java_script_enabled=False, viewport={"width": 320, "height": 844}
    )
    fallback_context.route("**/*", route)
    fallback = fallback_context.new_page()
    fallback.goto("http://ux.local" + pages["dashboard-onboarding"])
    assert fallback.locator(".ob-cta").is_visible()
    assert fallback.locator(".ob-dismiss").is_visible()
    assert not fallback.locator(".ob-showme").is_visible()
    fallback.goto("http://ux.local/help#api")
    assert fallback.locator("#api").count() == 1
    fallback.locator(".docs-toc-details > summary").click()
    assert fallback.locator(".docs-toc a[href='#api']").is_visible()
    # Branding is data, including very long install names.
    from app.web.deps import templates

    help_context = dict(client.get("/help").context)
    help_context["brand"] = "Northstar International Localization and Language Operations Workspace"
    fallback.set_content(templates.get_template("help.html").render(help_context))
    assert fallback.locator("main h1").inner_text() == help_context["brand"] + " documentation"
    assert fallback.evaluate("document.documentElement.scrollWidth <= innerWidth")
    fallback_context.close()
    # Shared label helper must not leak locale keys into badges.
    for locale in ("en", "es", "fr", "de"):
        from app.i18n import loader
        from app.services import settings

        settings.set_many({"default_ui_locale": locale})
        response = client.get("/")
        assert loader.translate(locale, "page.dashboard.title") in response.text
        assert "page.dashboard.status_" not in response.text
        cancelled_key = "page.jobs.status_cancelled"
        assert loader.translate(locale, cancelled_key) != cancelled_key
    browser.close()
if os.environ.get("UX_PAGE_LAYOUTS_WRITE"):
    # regenerate after an INTENDED markup change: review the diff of the fixture
    (ROOT / "tests/fixtures/ux_page_layout_contracts.json").write_text(
        json.dumps(LIVE, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
print(f"Page layout checks: {checks} page/viewport states plus 200% and payload contracts")
print("UX PAGE LAYOUTS OK")
