"""Guidance page layout and unchanged form contracts, using shared offline fixtures.

The form contract (tests/fixtures/ux_guidance_forms.json) holds one form per line,
as compact JSON. To regenerate it after an intended form change, review the diff
and run: UX_GUIDANCE_WRITE=1 python -m pytest tests/test_verify_suites.py -k verify_ux_guidance
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests._ux_golden import FormContractParser, write_golden, write_requested
from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

PAGES = (
    "style-guides",
    "style-guide-detail",
    "content-types",
    "languages",
)
CONTRACT = ROOT / "tests/fixtures/ux_guidance_forms.json"


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def dump_contract(contracts):
    nl = chr(10)
    lines = [
        dumps(name) + ":[" + nl + ("," + nl).join(dumps(f) for f in forms) + nl + "]"
        for name, forms in contracts.items()
    ]
    return "{" + nl + ("," + nl).join(lines) + nl + "}" + nl


class ContractParser(FormContractParser):
    """<main> only; a form is [its attributes, its controls], every attribute kept."""

    main_only = True

    def form_record(self, a):
        return [dict(a), []]

    def record_control(self, tag, attrs):
        if self.form is not None:
            self.form[1].append([tag, attrs])


with fresh_fixture() as (client, pages, _):
    rendered = render_pages(client, {name: pages[name] for name in PAGES})
    contracts = {}
    for name, html in rendered.items():
        parser = ContractParser()
        parser.feed(html)
        assert len(parser.ids) == len(set(parser.ids)), (name, "duplicate IDs")
        contracts[name] = parser.forms
    if write_requested("UX_GUIDANCE_WRITE"):
        write_golden(CONTRACT, dump_contract(contracts))
    assert contracts == json.loads(CONTRACT.read_text(encoding="utf-8")), "Form contract changed"
    for name, html in rendered.items():
        assert html.count("<h1>") == 1, name
        assert '<header class="page-header">' in html, name
        assert 'class="hero' not in html, name
        assert 'class="guidance-sections"' in html, name
        body = html[
            html.index("<main") : html.index("</main>")
        ]  # the shell dialogs hold tables of their own
        # Equal strength to the whole-page check: every table the page owns (the main region;
        # the shell dialogs hold tables of their own) is a shared data table with scoped headers.
        assert body.count("<table") == body.count('<table class="data-table"'), name
        assert body.count("<th ") == body.count('<th scope="col"'), name
        assert body.count("<table") == body.count('role="region" aria-label='), name
    assert "Editing the body or scope reverts it to draft." in rendered["style-guide-detail"]
    # Render each real route's context with its existing read-only predicates.
    from app.web.deps import templates

    for name in PAGES:
        response = client.get(pages[name])
        context = dict(response.context)
        context.update(
            can_edit=False,
            can_approve=False,
            can_manage=False,
            can_invalidate=False,
            can_purge=False,
            can_apply=False,
            can_generate=False,
            assist_can_write=lambda request: False,
        )
        readonly = templates.env.get_template(response.template.name).render(context)
        parser = ContractParser()
        parser.feed(readonly)
        assert not [form for form in parser.forms if form[0].get("method") == "post"], name
        if name in ("style-guides", "content-types", "languages"):
            context.update(
                rows=[], sets=[], candidates=[], conflicted=[], proposed=[], approved=[], history=[]
            )
            empty = templates.env.get_template(response.template.name).render(context)
            assert 'class="empty-state"' in empty, name
            assert "<table" not in empty[empty.index("<main") : empty.index("</main>")], name
    # Real Chrome keyboard/local scrolling, 320px reflow and 200% zoom.

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        for width, zoom in [(320, 1), (1440, 2)]:
            context = browser.new_context(
                viewport={"width": width, "height": 900}, reduced_motion="reduce"
            )

            route = install_offline_route(context, client)
            page = context.new_page()
            for name in PAGES:
                page.goto("http://ux.local" + pages[name], wait_until="networkidle")
                page.evaluate("(zoom) => document.body.style.zoom = zoom", zoom)
                assert page.locator("main h1").count() == 1, name
                assert (
                    page.locator("main h1").inner_text().strip()
                    != page.locator(".crumbs").inner_text().strip()
                ), name
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                assert overflow <= 1, (name, width, zoom, overflow)
                for link in page.locator(".guidance-sections a").all():
                    target = link.get_attribute("href")
                    assert page.locator(target).count() == 1, (name, target)
                for region in page.locator(".data-table-region").all():
                    assert region.get_attribute("aria-label"), name
                    region.focus()
                    assert region.evaluate("(e) => e === document.activeElement"), name
                    page.keyboard.press("ArrowRight")
                    assert region.evaluate("(e) => getComputedStyle(e).outlineStyle") != "none", (
                        name
                    )
            context.close()
        nojs = browser.new_context(java_script_enabled=False)
        nojs.route("**/*", route)
        page = nojs.new_page()
        for name in PAGES:
            page.goto("http://ux.local" + pages[name], wait_until="networkidle")
            assert page.locator("main h1").is_visible(), name
            for link in page.locator(".guidance-sections a").all():
                assert page.locator(link.get_attribute("href")).is_visible(), name
        nojs.close()
        browser.close()
print("UX GUIDANCE OK")
