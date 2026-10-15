"""Direction A review-run DOM contracts. Documented in the section 08 hand-off."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, render_pages

CSS = (ROOT / "app/static/style.css").read_text(encoding="utf-8")


def styled(html):
    # Inline the stylesheet: no-JavaScript contexts cannot use add_style_tag.
    return html.replace("</head>", "<style>" + CSS + "</style></head>", 1)


NAMES = ("run", "run-step-diff", "batch", "my-work", "queries", "query-thread")


DUPLICATE_IDS = """() => {
    const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
    return ids.filter((id, i) => ids.indexOf(id) !== i);
}"""
FIELDS_OWNED = """() => [...document.querySelectorAll('main input, main select, main textarea')]
    .every(e => !!e.form)"""
SECTION_LINKS_RESOLVE = """() => [...document.querySelectorAll('.review-runs-sections a')]
    .every(e => document.getElementById(e.hash.slice(1)))"""
SECTION_NAV_NAMED = """() => [...document.querySelectorAll('.review-runs-sections')]
    .every(e => (e.getAttribute('aria-label') || '').trim())"""
CONTROLS_LABELLED = """() => [...document.querySelectorAll(
        'main input:not([type=hidden]), main select, main textarea')]
    .every(e => e.labels.length || (e.getAttribute('aria-label') || '').trim())"""
REGIONS_NAMED_FOCUSABLE = """() => [...document.querySelectorAll('main [role="region"]')]
    .every(e => (e.getAttribute('aria-label') || '').trim()
        && e.getAttribute('tabindex') === '0')"""
NO_PAGE_OVERFLOW = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"

with fresh_fixture(review_runs=True) as (client, pages, _):
    selected = {name: pages[name] for name in NAMES}
    rendered = render_pages(client, selected)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.route("**/*", lambda route: route.abort())
        for name, html in rendered.items():
            page.set_content(styled(html))
            assert page.locator("main h1").count() == 1, name
            assert page.locator("main .page-header h1").count() == 1, name
            assert page.locator("main .hero").count() == 0, name
            assert not page.evaluate(DUPLICATE_IDS), name
            assert page.locator("form form").count() == 0, name
            assert page.evaluate(FIELDS_OWNED), name
            assert page.evaluate(SECTION_LINKS_RESOLVE), name
            assert page.locator(".topbar h1").count() == 0, name
            # A11y basics: named section nav, labelled controls, statuses without raw underscores.
            assert page.evaluate(SECTION_NAV_NAMED), name
            assert page.evaluate(CONTROLS_LABELLED), name
            for badge in page.locator("main .status-text .badge").all():
                text = badge.text_content().strip()
                assert text and "_" not in text, (name, text)
                assert badge.get_attribute("title") == text, (name, text)
            for width in (320, 390, 720, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate(NO_PAGE_OVERFLOW), (name, width)
                if width == 390:
                    # Named, keyboard-reachable scroll regions on a phone.
                    assert page.evaluate(REGIONS_NAMED_FOCUSABLE), name
            for region in page.locator('main [role="region"][tabindex="0"]').all():
                if region.is_visible():
                    region.focus()
                    assert region.evaluate("(e) => e === document.activeElement"), name
                    page.keyboard.press("Tab")
                    page.keyboard.press("Shift+Tab")
                    assert region.evaluate("(e) => e === document.activeElement"), name
        page.set_content(rendered["run"])
        assert page.get_by_role("heading", name="Human review", exact=True).count() == 1
        form = page.locator('form[action$="/resume"]')
        assert form.get_attribute("method") == "post"
        assert form.locator('[name="locale"]').input_value() == "fr-FR"
        assert form.locator('textarea[name="edited_text"]').input_value().startswith("Bienvenue")
        assert form.get_by_role("button", name="Approve & resume").count() == 1
        assert page.locator("#stepLog[data-output-cap]").count() == 1
        assert page.locator("#runGraphCard").count() == 1
        assert (
            page.locator('main .status-text[data-status="paused"] .badge.paused').text_content()
            == "paused"
        )
        page.set_content(rendered["batch"])
        assert page.locator("tr[data-locale]").count() == 4
        for locale, state in [
            ("fr-FR", "paused"),
            ("de-DE", "completed"),
            ("es-ES", "failed"),
            ("it-IT", "not launched"),
        ]:
            assert state in page.locator(f'tr[data-locale="{locale}"] .bm-statuscell').inner_text()
        assert page.get_by_role("region", name="Locales × steps").get_attribute("tabindex") == "0"
        page.set_content(rendered["run-step-diff"])
        assert page.get_by_role("button", name="Compare", exact=True).count() == 1
        assert page.locator('select[name="from"] option:checked').count() == 1
        assert page.locator('select[name="to"] option:checked').count() == 1
        assert page.locator('main .status-text[data-status="paused"]').count() >= 2
        assert page.get_by_role("heading", name="From", exact=True).count() >= 1
        assert page.get_by_role("heading", name="To", exact=True).count() >= 1
        assert page.locator(".review-runs-comparison ins, .review-runs-comparison del").count() >= 1
        page.set_content(rendered["my-work"])
        assert page.locator(f'a[href="{pages["run"]}"]').count() == 1
        assert page.locator('input[name="days"]').input_value() == "90"
        assert not page.locator('input[name="quality"]').is_checked()
        page.set_content(rendered["queries"])
        assert (
            page.get_by_role("region", name="Threads", exact=True).get_attribute("tabindex") == "0"
        )
        assert page.locator('select[name="status"]').input_value() == ""
        assert (
            page.locator(
                'form[action="/queries"][method="post"] textarea[name="body"][required]'
            ).count()
            == 1
        )
        assert (
            page.locator('main .status-text[data-status="answered"] .badge').text_content()
            == "answered"
        )
        page.set_content(rendered["query-thread"])
        assert (
            page.locator('.page-header .status-text[data-status="answered"] .badge').text_content()
            == "answered"
        )
        assert page.locator(".post").count() == 2
        assert "Should the product name" in page.locator(".post").nth(0).inner_text()
        assert "Keep Atlas unchanged" in page.locator(".post").nth(1).inner_text()
        reply = page.locator('form[data-preserve-draft="query-reply"]')
        assert reply.locator('textarea[name="body"][maxlength="4000"][required]').count() == 1
        assert not reply.locator('input[name="answer"]').is_checked()
        assert page.get_by_role("button", name="Close query", exact=True).count() == 1
        # Existing read-only and empty states through their real routes.
        empty = client.get("/queries?status=closed")
        page.set_content(empty.text)
        assert page.locator(".empty-state").inner_text() == "No query threads match."
        response = client.post(
            pages["query-thread"] + "/status", data={"status": "closed"}, follow_redirects=False
        )
        assert response.status_code == 303
        page.set_content(client.get(pages["query-thread"]).text)
        assert page.locator('form[data-preserve-draft="query-reply"]').count() == 0
        assert page.get_by_role("button", name="Reopen query").count() == 1
        # Arbitrary object names must reflow without changing stored data rules.
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Document, StepRun, Workflow

        with Session(engine) as session:
            workflow = session.exec(select(Workflow)).first()
            workflow.name = "Atlas " + "international-product-review-" * 6
            session.add(workflow)
            document = session.exec(select(Document)).first()
            document.filename = "getting-started-" * 10 + ".txt"
            session.add(document)
            session.commit()
        page.set_viewport_size({"width": 320, "height": 844})
        for name in ("run", "run-step-diff", "batch"):
            page.set_content(styled(client.get(pages[name]).text))
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            ), (name, "long name")
        with Session(engine) as session:
            for step in session.exec(select(StepRun)):
                session.delete(step)
            session.commit()
        page.set_content(client.get(pages["run-step-diff"]).text)
        assert "No comparable pair recorded" in page.locator(".empty-state").inner_text()
        # Status labels come from the catalogs (page.jobs keys; query keys in page.review_runs).
        assert client.post(
            "/account/locale", data={"ui_locale": "de"}, follow_redirects=False
        ).status_code in (302, 303)
        page.set_content(client.get(pages["run"]).text)
        run_badge = page.locator('main .status-text[data-status="paused"]')
        assert run_badge.get_attribute("lang") == "de" and run_badge.text_content() == "pausiert"
        page.set_content(client.get(pages["query-thread"]).text)
        thread_badge = page.locator('.page-header .status-text[data-status="closed"]')
        assert (
            thread_badge.get_attribute("lang") == "de"
            and thread_badge.text_content() == "geschlossen"
        )
        context.close()
        browser.close()

print("REVIEW RUNS UX OK")
