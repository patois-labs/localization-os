"""ux/admin-settings: rendered form contracts and keyboard/reflow regression checks.

The form contract (every form action, method, field name, id, type, default and
option) is pinned in tests/fixtures/ux_admin_settings_contract.jsonl, one JSON
form per line. Forms shared by all five pages (the top search and the new-project
dialog forms) are stored once as {"shared": key, "form": ...} lines and referenced
from each page by {"page": name, "shared": key}.

Regenerate after an intentional form change (review the diff before committing):
    UX_ADMIN_SETTINGS_WRITE=1 python tests/verify_ux_admin_settings.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests._ux_golden import FormContractParser, write_golden, write_requested
from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

PAGES = ("settings", "languages-settings", "enterprise", "security")
# Names the widest offenders when a page-wide overflow check fails.
OVERFLOWERS = """() => [...document.querySelectorAll('main *')]
  .filter(e => e.getBoundingClientRect().right > innerWidth + 1)
  .slice(0, 6).map(e => e.tagName + '.' + e.className + '#' + e.id)"""
CONTRACT = ROOT / "tests/fixtures/ux_admin_settings_contract.jsonl"


class Forms(FormContractParser):
    """Whole page, forms only; every attribute but `class` (class changes are styling)."""

    def form_record(self, a):
        return {"attributes": {k: v for k, v in a.items() if k != "class"}, "controls": []}

    def control_attrs(self, a):
        return {k: v for k, v in a.items() if k != "class"}

    def record_control(self, tag, attrs):
        if self.form is not None:
            self.form["controls"].append([tag, attrs])


def contracts(rendered):
    result = {}
    for name, html in rendered.items():
        parser = Forms()
        parser.feed(html)
        result[name] = parser.forms
    return result


def _dump(obj):
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def serialize(result):
    """Contract dict -> JSONL lines, shared forms stored once."""
    seen = {}
    for forms in result.values():
        for form in forms:
            seen.setdefault(_dump(form), []).append(1)
    shared = {text: f"s{i}" for i, text in enumerate(t for t, n in seen.items() if len(n) > 1)}
    lines = [_dump({"shared": key, "form": json.loads(text)}) for text, key in shared.items()]
    for name, forms in result.items():
        for form in forms:
            text = _dump(form)
            lines.append(
                _dump({"page": name, "shared": shared[text]})
                if text in shared
                else _dump({"page": name, "form": form})
            )
    return "\n".join(lines) + "\n"


def load_contract(text):
    shared, result = {}, {}
    for line in text.splitlines():
        item = json.loads(line)
        if "page" not in item:
            shared[item["shared"]] = item["form"]
        else:
            form = shared[item["shared"]] if "shared" in item else item["form"]
            result.setdefault(item["page"], []).append(form)
    return result


def assert_region_scrolls(page, region, label, attempts=4):
    """ArrowRight must scroll a focused table region (assertion: scrollLeft > 0).

    A smooth scroll from the focus jump can swallow the first key press on a loaded
    machine, so the key is pressed again, at most `attempts` times in all.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for _ in range(attempts):
        page.wait_for_timeout(400)
        page.keyboard.press("ArrowRight")
        try:
            page.wait_for_function(
                "(e) => e.scrollLeft > 0", arg=region.element_handle(), timeout=2500
            )
            return
        except PlaywrightTimeout:
            continue
    sizes = region.evaluate("(e) => [e.scrollWidth, e.clientWidth, e.className]")
    raise AssertionError((*label, "region did not scroll", sizes))


def main():
    from playwright.sync_api import sync_playwright

    with fresh_fixture() as (client, pages, _):
        pages = {name: pages[name] for name in PAGES}
        rendered = render_pages(client, pages)
        found = contracts(rendered)
        if write_requested("UX_ADMIN_SETTINGS_WRITE"):
            write_golden(CONTRACT, serialize(found))
        expected = load_contract(CONTRACT.read_text(encoding="utf-8"))
        # JSON round trip normalises tuples/None the same way the file does.
        assert json.loads(json.dumps(found)) == expected, (
            "form destinations, fields or defaults changed"
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            try:
                for js in (False, True):
                    context = browser.new_context(
                        java_script_enabled=js,
                        viewport={"width": 320, "height": 844},
                        reduced_motion="reduce",
                    )

                    install_offline_route(context, client)
                    page = context.new_page()
                    for name, path in pages.items():
                        page.goto("http://ux.local" + path, wait_until="networkidle")
                        assert page.locator("main h1").count() == 1, name
                        assert page.locator("main .page-header").count() == 1, name
                        assert page.locator("main .hero").count() == 0, name
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        ), name
                        ids = page.locator("[id]").evaluate_all("(els) => els.map(e => e.id)")
                        assert len(ids) == len(set(ids)), (name, "duplicate IDs")
                        if name != "languages-settings":
                            links = page.locator(".settings-index a")
                            assert links.count() > 0, name
                            for link in links.all():
                                target = link.get_attribute("href")
                                assert page.locator(target).count() == 1, (name, target)
                                link.focus()
                                page.keyboard.press("Enter")
                                assert page.evaluate("location.hash") == target, (name, target)
                        for region in page.locator(".data-table-region").all():
                            region.focus()
                            assert region.evaluate("(e) => e === document.activeElement"), name
                            assert_region_scrolls(page, region, (name, js))
                        if name == "security":
                            assert (
                                page.locator(
                                    'form[action="/security"] input[name="password_min_length"]'
                                ).count()
                                == 1
                            )
                            assert (
                                page.locator(
                                    'form[action="/security"] button[type="submit"]'
                                ).count()
                                == 1
                            )
                            # #753: the MCP OAuth admin page stays reachable from Security,
                            # from the Connector OAuth card, not from the in-page index
                            # (whose links must all be same-page anchors, checked above).
                            mcp_link = page.locator(
                                "#settings-oauth a#security-mcp-oauth-link"
                                '[href="/security/mcp-oauth"]'
                            )
                            assert mcp_link.count() == 1, "MCP OAuth link missing"
                            assert page.locator('.settings-index a[href^="/"]').count() == 0, (
                                "settings index links off the page"
                            )
                        if name == "enterprise":
                            assert page.get_by_text("Read-only.", exact=True).count() == 1
                            assert (
                                page.locator(
                                    'form[action="/settings/enterprise/preflight"] '
                                    "button:not([disabled])"
                                ).count()
                                == 7
                            )
                            assert (
                                page.locator(
                                    'form[action="/settings/enterprise/enable"] '
                                    "button:not([disabled])"
                                ).count()
                                == 0
                            )
                        page.set_viewport_size({"width": 1440, "height": 900})
                        page.evaluate('document.documentElement.style.zoom = "2"')
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        ), (name, "200% zoom", page.evaluate(OVERFLOWERS))
                        page.evaluate('document.documentElement.style.zoom = "1"')
                        page.set_viewport_size({"width": 320, "height": 844})
                    context.close()
            finally:
                browser.close()
        # Open core: the paid Databricks section is gone with its add-on.
        assert client.get("/admin/databricks").status_code == 404
    print("UX ADMIN SETTINGS OK")


if __name__ == "__main__":
    main()
