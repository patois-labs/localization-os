"""Direction A review pages: real seeded DOM, resources and unchanged save payloads."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright

from tests._ux_smoke import fresh_fixture

with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    # Optional red-baseline proof, using immutable templates without changing the checkout.
    if os.environ.get("UX_REVIEW_BASELINE") == "1":
        from jinja2 import ChoiceLoader, DictLoader

        from app.web.deps import templates

        templates.env.loader = ChoiceLoader(
            [
                DictLoader(
                    {
                        name + ".html": subprocess.check_output(
                            ["git", "show", "d8aac05e:app/templates/" + name + ".html"]
                        ).decode("utf-8")
                        for name in ("editor", "lqa", "paragraphs", "delivery_empties")
                    }
                ),
                templates.env.loader,
            ]
        )
        templates.env.cache.clear()
    browser = pw.chromium.launch(channel="chrome")
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    writes = []
    fail_save = False

    def route(r):
        url = urlsplit(r.request.url)
        if url.netloc != "ux.local":
            r.abort()
            return
        if r.request.method != "GET":
            writes.append((url.path, parse_qs(r.request.post_data or "")))
            if url.path.endswith("/save"):
                r.fulfill(
                    status=503 if fail_save else 200,
                    content_type="application/json",
                    body="{}" if fail_save else '{"ok":true,"status":"edited","qa":[]}',
                )
            else:
                r.abort()
            return
        response = client.get(url.path + ("?" + url.query if url.query else ""))
        r.fulfill(
            status=response.status_code,
            body=response.content,
            headers={
                k: v
                for k, v in response.headers.items()
                if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")
            },
        )

    context.route("**/*", route)
    page = context.new_page()
    for name in (
        "editor",
        "editor-de",
        "lqa",
        "lqa-empty",
        "paragraphs",
        "paragraphs-unavailable",
        "delivery-empties",
        "delivery-empties-clear",
        "delivery-empties-keys",
    ):
        page.goto("http://ux.local" + pages[name], wait_until="networkidle")
        assert page.locator("main h1").count() == 1, name
        assert page.locator("main .page-header").count() == 1, name
        ids = page.locator("[id]").evaluate_all("(els) => els.map(e => e.id)")
        assert len(ids) == len(set(ids)), name
        for width in (1440, 720, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
                name,
                width,
            )
        page.set_viewport_size({"width": 1440, "height": 900})
        for region in page.locator(".data-table-region").all():
            region.focus()
            assert region.evaluate("(e) => e === document.activeElement")
    page.goto("http://ux.local" + pages["editor-de"], wait_until="networkidle")
    targets = page.locator(".tgt-edit")
    assert targets.count() == 2
    assert page.locator(".review-resources").is_visible()
    assert "100%" in page.locator(".review-resources").inner_text()
    assert "Atlas" in page.locator(".review-resources").inner_text()
    # QA flags are disclosures: they stay in their own row, in Tab order, never in the rail.
    assert page.locator(".review-resources .qa-flag").count() == 0
    assert page.locator("tr.review-selected td.qa .qa-flag").count() >= 1
    assert page.locator("tr.review-selected td.qa .qa-flag summary").first.is_visible()
    assert "Needs review" in page.locator(".review-state").first.text_content()
    # Table semantics: every row keeps all 8 cells in the accessibility tree (a cell
    # dropped with display:none moves later cells under the wrong column header).
    for width in (1440, 390):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.locator("table.review-editor tr").evaluate_all(
            "rows => rows.every(r => r.children.length === 8"
            " && [...r.children].every(c => getComputedStyle(c).display !== 'none'))"
        ), width
        cdp = context.new_cdp_session(page)
        tree = cdp.send("Accessibility.getFullAXTree")["nodes"]
        cdp.detach()
        by_id = {n["nodeId"]: n for n in tree}

        def role(node):
            return node.get("role", {}).get("value")

        tables = [n for n in tree if role(n) == "table"]
        assert len(tables) == 1, (width, len(tables))
        per_row = [
            sum(
                role(by_id[c]) in ("cell", "columnheader")
                for c in n.get("childIds", [])
                if c in by_id
            )
            for n in tree
            if role(n) == "row"
        ]
        assert per_row == [8, 8, 8], (width, per_row)
    page.set_viewport_size({"width": 1440, "height": 900})
    # The percentage shows in the rail; the Accept TM button stays in the selected row,
    # the next Tab stop after the target (not at the end of the page).
    # The rail moves in a matchMedia change handler that fires a frame or two after the
    # viewport change (slower on Linux headless Chrome): wait for it, do not read at once.
    expect(page.locator('.review-resources [data-resource="tm"]')).to_contain_text("100%")
    accept = page.locator('tr.review-selected [data-action="accept-tm"]')
    assert accept.is_visible()
    targets.first.focus()
    page.keyboard.press("Tab")
    assert accept.evaluate("e => e === document.activeElement"), (
        "Accept TM is not next after the text"
    )
    accept.click()
    page.wait_for_timeout(300)
    assert any(
        data.get("target_text") == ["Willkommen bei Atlas."]
        for path, data in writes
        if path.endswith("/save")
    )
    assert targets.evaluate_all("""els => els.every(e => {
        const row = e.closest('tr').getBoundingClientRect(), box = e.getBoundingClientRect();
        return box.bottom <= row.bottom && box.top >= row.top;
    })""")
    targets.nth(1).focus()
    assert "segment 2" in page.locator(".review-resource-segment").inner_text()
    targets.nth(0).fill("Un nouveau texte.")
    targets.nth(0).press("Enter")
    page.wait_for_timeout(300)
    assert any(
        data.get("target_text") == ["Un nouveau texte."] and "tte_ms" in data
        for path, data in writes
        if path.endswith("/save")
    )
    fail_save = True
    targets.nth(0).fill("Keep this failed draft.")
    targets.nth(0).press("Enter")
    page.wait_for_timeout(1200)
    assert targets.nth(0).input_value() == "Keep this failed draft."
    assert page.locator("tr.save-failed").count() >= 1
    assert page.locator("#edAlert").inner_text()
    page.set_viewport_size({"width": 390, "height": 844})
    # The rail hides in the same matchMedia handler: wait for it instead of reading at once.
    expect(page.locator(".review-resources")).to_be_hidden()
    assert page.locator("tr[data-seg]").first.locator('[data-action="accept-tm"]').is_visible()
    assert page.locator('[id^="qa-"]').count() == 2
    # A segment with nothing in a section shows no empty section label on a phone.
    assert page.locator(
        "td.tm:not(:has(.badge, button)), td.terms:not(:has(.term)), td.qa:not(:has(details))"
    ).evaluate_all(
        "els => els.length > 0 && els.every(e => getComputedStyle(e).display !== 'none'"
        " && e.getBoundingClientRect().height <= 1 && e.getBoundingClientRect().width <= 1)"
    )
    assert page.locator("tr[data-seg]").first.evaluate("""row => {
        const source = row.querySelector('.src').getBoundingClientRect();
        const target = row.querySelector('.tgt').getBoundingClientRect();
        const actions = row.querySelector('.acts').getBoundingClientRect();
        const tm = row.querySelector('.tm').getBoundingClientRect();
        return target.top >= source.bottom && actions.top >= target.bottom
          && tm.top >= actions.bottom;
    }""")
    # Progressive fallback keeps all resources with their row and all forms available.
    fallback = browser.new_context(
        java_script_enabled=False, viewport={"width": 390, "height": 844}
    )
    fallback.route("**/*", route)
    plain = fallback.new_page()
    plain.goto("http://ux.local" + pages["editor-de"])
    assert plain.locator(".review-resources").is_hidden()
    assert plain.locator("tr[data-seg]").first.locator('[data-action="accept-tm"]').is_visible()
    browser.close()

print("Review editor layout, reflow, keyboard, resources and save payloads OK")
