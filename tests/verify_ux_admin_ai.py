"""Setup AI presentation contracts using the shared offline fixture."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route

with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome")
    page = browser.new_page()
    for name in ("setup-ai", "jev-inspect"):
        response = client.get(pages[name])
        assert response.status_code == 200
        page.set_content(response.text)
        assert page.locator("main h1").count() == 1
        heading = page.locator("main h1").inner_text().rstrip(" ?")
        assert heading == (
            "Setup AI"
            if name == "setup-ai"
            else "Inspect: Atlas quality judge for multilingual product documentation"
        )
        assert heading not in page.locator(".topbar2").inner_text()
        ids = page.locator("main [id]").evaluate_all("(els) => els.map(e => e.id)")
        assert len(ids) == len(set(ids)), name
        assert page.locator("main .page-header").count() == 1
        assert page.locator("main .hero").count() == 0
        for link in page.locator(".admin-ai-index a").all():
            assert page.locator(link.get_attribute("href")).count() == 1
        assert page.locator(".admin-ai-index a").count() >= 4
        assert page.locator('input[type=password][value]:not([value=""])').count() == 0
        if name == "setup-ai":
            # S2: the web verification and semantic TM cards left with the paid add-ons
            assert page.locator("#page-layout > [data-card]").count() == 8
            assert (
                page.locator(
                    'form[action="/setup-ai/local"] [name="local_sampling_form"]'
                ).input_value()
                == "1"
            )
            assert page.locator('form[action="/setup-ai/local"] [name="local_model"]').count() == 2
            assert page.locator("#lmodelManual").is_disabled()
            assert page.locator("#engineAddForm").get_attribute("action") == "/setup-ai/engines"
            assert page.locator('form[action="/setup-ai/spend"] #spend-cap-input').count() == 1
            assert (
                page.locator('form[action="/setup-ai"] button[type=submit]').inner_text()
                == "Save defaults"
            )
        else:
            assert page.locator('.data-table-region[tabindex="0"]').count() == 1
            assert page.locator('#jevTryForm [name="source_locale"]').input_value() == "en"
            assert page.locator('#jevTryForm [name="target_locale"]').input_value() == "de"
            assert page.locator("#jevTryBtn").is_disabled()
            assert page.locator('#criteria pre[tabindex="0"]').count() == 1
            assert page.locator('a[href="/setup-ai#quality-judges"]').count() == 1
    # Empty and restricted rendering keep the same form gates and anchor targets.
    from app.web.deps import templates

    setup = client.get(pages["setup-ai"])
    context = dict(setup.context)
    context.update(engines=[], can_inspect_judges=False, can_see_spend=False)
    page.set_content(templates.env.get_template("setup_ai.html").render(context))
    assert page.locator(".empty-state").count() == 2
    assert page.locator("#engineAddForm").count() == 0
    assert (
        page.locator("#ai-cloud input[type=password], #ai-local input[type=password]").count() == 0
    )
    assert page.locator('form[action^="/setup-ai/search-key"]').count() == 0  # paid card is gone
    assert page.locator('form[action="/setup-ai/local"]').count() == 0
    assert page.locator("#ai-spend").count() == 1
    assert page.locator("#ai-default-fields").count() == 1

    invalid = client.post(
        "/setup-ai/spend", data={"spend_cap_usd": "not-a-number"}, follow_redirects=False
    )
    assert invalid.status_code == 303
    error = client.get(invalid.headers["location"])
    page.set_content(error.text)
    assert page.locator('#spend-cap-input[aria-invalid="true"]').count() == 1

    # Actual CSS/JS, offline routes, keyboard access, narrow reflow and zoom.
    route = install_offline_route(page, client, forward_headers=False)
    for name in ("setup-ai", "jev-inspect"):
        page.goto("http://ux.local" + pages[name], wait_until="networkidle")
        for width in (1440, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                name,
                width,
            )
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("document.body.style.zoom = '2'")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            name,
            "zoom",
        )
        page.evaluate("document.body.style.zoom = ''")
        if name == "setup-ai":
            # A short laptop viewport must not clip the sticky index's last links.
            page.set_viewport_size({"width": 1440, "height": 560})
            assert (
                page.locator(".admin-ai-index").evaluate("(e) => getComputedStyle(e).position")
                == "static"
            )
            page.set_viewport_size({"width": 1440, "height": 900})
            assert (
                page.locator(".admin-ai-index").evaluate("(e) => getComputedStyle(e).position")
                == "sticky"
            )
        link = page.locator(".admin-ai-index a").first
        link.focus()
        page.keyboard.press("Enter")
        assert page.evaluate("location.hash") == link.get_attribute("href")
        if name == "jev-inspect":
            region = page.locator(".data-table-region")
            region.focus()
            assert region.evaluate("(e) => document.activeElement === e")
            page.keyboard.press("Tab")
            assert page.locator("#criteria summary").evaluate("(e) => document.activeElement === e")
            page.keyboard.press("Enter")
            page.keyboard.press("Tab")
            assert page.locator("#criteria pre").evaluate("(e) => document.activeElement === e")
    fallback = browser.new_context(java_script_enabled=False)
    nojs = fallback.new_page()
    for name in ("setup-ai", "jev-inspect"):
        nojs.set_content(client.get(pages[name]).text)
        assert nojs.locator(".admin-ai-index a").count() >= 4
        assert nojs.locator(".admin-ai-page section.card:visible").count() == (
            8 if name == "setup-ai" else 4
        )
    fallback.close()
    browser.close()
print("Setup AI and inspector DOM contracts OK")
