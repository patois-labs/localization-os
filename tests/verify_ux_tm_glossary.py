"""Direction A TM and glossary DOM contracts using the shared offline seed."""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._ux_smoke import fresh_fixture, render_pages

with fresh_fixture() as (client, pages, _):
    names = (
        "tm",
        "tm-detail",
        "glossaries",
        "glossary",
        "glossary-suggest",
        "tm-empty",
        "tm-picker",
    )
    rendered = render_pages(client, {n: pages[n] for n in names})
    # Render supported permission/empty branches using the real route context.
    for name in ("tm", "tm-detail", "glossary"):
        response = client.get(pages[name])
        context = dict(response.context, can_edit=False, can_delete=False, can_approve=False)
        rendered[name + "-readonly"] = response.template.render(context)
    response = client.post(pages["glossary-suggest"])
    rendered["suggest-empty"] = response.template.render(dict(response.context, suggestions=[]))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page()
        for name, html in rendered.items():
            page.set_content(html)
            page.add_style_tag(
                path=str(Path(__file__).resolve().parents[1] / "app/static/style.css")
            )
            assert page.locator("main h1").count() == 1, name
            assert page.locator("main .page-header").count() == 1, name
            assert page.locator("main .hero").count() == 0, name
            if name.endswith("-readonly"):
                write_forms = (
                    'main form[method="post"][action^="/tm"], '
                    'main form[method="post"][action^="/glossaries"]'
                )
                assert page.locator(write_forms).count() == 0, name
            assert page.evaluate("""() => {
                const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                return ids.length === new Set(ids).size;
            }"""), name
            for region in page.locator(".data-table-region").all():
                assert region.get_attribute("tabindex") == "0" and region.get_attribute(
                    "aria-label"
                ), name
                if not region.is_visible():
                    continue  # focus check only: inside a collapsed disclosure (glossary tools)
                region.focus()
                assert region.evaluate("(e) => e === document.activeElement"), name
                page.keyboard.press("Shift+Tab")
                page.keyboard.press("Tab")
                assert region.evaluate("(e) => e === document.activeElement"), name
            for width in (320, 390, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                ), (name, width)
            page.evaluate("document.documentElement.style.fontSize = '200%'")
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            ), (name, "200%")
            page.evaluate("document.documentElement.style.fontSize = ''")
            assert page.evaluate("""() => [...document.querySelectorAll(
                'main form input, main form select, main form textarea')].every(e =>
                e.type === 'hidden' || e.type === 'submit' || e.labels.length
                || e.getAttribute('aria-label'))"""), name
            # Visually hidden legends still name their fieldsets (gate 10).
            for fid, label in (
                ("tm-filter-fields", "Filter"),
                ("new-glossary", "New glossary"),
                ("glossary-new-term", "Add a term"),
            ):
                if page.locator(f"#{fid}").count():
                    group = page.get_by_role("group", name=label, exact=True)
                    assert group.count() == 1, (name, fid)
                    assert group.get_attribute("id") == fid, (name, fid)
                    # Hidden visually only: still rendered (not display:none) and
                    # still the fieldset's first legend.
                    legend = page.locator(f"#{fid} > legend")
                    assert legend.count() == 1, (name, fid)
                    assert legend.text_content().strip() == label, (name, fid)
                    assert legend.evaluate(
                        "(e) => getComputedStyle(e).display !== 'none'"
                        " && getComputedStyle(e).visibility !== 'hidden'"
                    ), (name, fid)
            # Every action column header has an accessible name.
            assert page.evaluate("""() => [...document.querySelectorAll('main th')]
                .every(th => th.textContent.trim().length > 0)"""), name
        page.set_content(rendered["tm"])
        assert (
            page.locator('form[action="/tm/bulk-delete"] input[name="customer"]').input_value()
            == "1"
        )
        assert page.locator('a[href="/tm/1?customer=1"]').count() >= 1
        page.set_content(rendered["tm-detail"])
        assert (
            page.locator('form[action="/tm/1/edit"] textarea[name="target"]').input_value()
            == "Bienvenue dans Atlas."
        )
        assert page.locator('.tm-text-pair [lang="en-US"]').count() == 1
        assert page.locator('.tm-text-pair [lang="fr-FR"]').count() == 1
        page.set_content(rendered["glossary"])
        cards = page.locator("[data-card]").evaluate_all("(es) => es.map(e => e.dataset.card)")
        assert cards == [
            "terms",
            "pairs",
            "status",
            "concepts",
            "extract",
            "import",
            "ai_builder",
            "rename",
            "danger",
        ], cards
        assert page.locator(".asset-sections a").first.get_attribute("href") == "#glossary-terms"
        assert page.locator('[data-card="terms"] form[action="/glossaries/1/delete"]').count() == 0
        assert page.locator('[data-card="danger"] form[action="/glossaries/1/delete"]').count() == 1
        # Status (approve / unapprove) and Settings are open on load. Both still toggle
        # from the keyboard.
        for card, starts_open in (("status", True), ("rename", True)):
            disclosure = page.locator(f'[data-card="{card}"] details')
            assert disclosure.count() == 1, card
            assert (disclosure.get_attribute("open") is not None) == starts_open, card
            disclosure.locator("summary").focus()
            page.keyboard.press("Enter")
            assert (disclosure.get_attribute("open") is not None) == (not starts_open), card
        # Content type is always visible on load (inside the open Settings card), not folded away.
        page.set_content(rendered["glossary"])
        ct = page.locator('[data-card="rename"] form[action="/glossaries/1/content-type"]')
        assert ct.count() == 1
        assert ct.locator('select[name="content_type_id"]').is_visible()
        assert ct.locator("button").is_visible()
        assert (
            page.locator('[data-card="status"] form[action="/glossaries/1/content-type"]').count()
            == 0
        )
        approve = '[data-card="status"] form[action="/glossaries/1/approve"]'
        assert page.locator(approve).count() == 1
        # The review restored the form headings; each is a real heading exactly once.
        for page_name, heading in (
            ("glossary", "Add a term"),
            ("glossaries", "New glossary"),
            ("tm", "Filter"),
        ):
            page.set_content(rendered[page_name])
            found = page.get_by_role("heading", name=heading, exact=True)
            assert found.count() == 1, (page_name, heading)
        page.set_content(rendered["glossary"])
        assert (
            page.locator(
                'form[action="/glossaries/1/terms/add"][data-preserve-draft="glossary-add-term"]'
            ).count()
            == 1
        )
        assert page.locator('form[action="/glossaries/1/delete"][data-action-confirm]').count() == 1
        page.set_content(rendered["tm-empty"])
        assert page.locator(".empty-state").count() >= 1
        browser.close()
print("Eleven TM/glossary rendered page and state contracts OK")
