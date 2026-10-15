"""Console account/auth DOM, form, keyboard, reflow and no-JS regression checks."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route, render_pages

# Captured before the migration. Pins fields, values, defaults, autocomplete,
# form ownership/order, submit hooks and CSRF includes; only header geometry differs.
contract = json.loads(
    (ROOT / "tests/fixtures/ux_auth_form_contract.json").read_text(encoding="utf-8")
)
NO_OVERFLOW = "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
pattern = (
    r'</?(?:form|input|button|select|textarea|option|label)\b[^>]*>|{% include "_csrf.html" %}'
)
for filename, expected in contract.items():
    source = (ROOT / "app/templates" / filename).read_text(encoding="utf-8")
    source = source.replace("layout-edit-btn layout-edit-inline", "layout-edit-btn")
    actual = [re.sub(r"\s+", " ", tag).strip() for tag in re.findall(pattern, source)]
    assert actual == expected, filename

with fresh_fixture(auth=True) as (client, pages, _):
    # Admin-insight previews are (path, form) tuples; only the auth replay URLs are owned here.
    replay = "/__ux-fixtures/auth/"
    pages = {n: p for n, p in pages.items() if isinstance(p, str) and p.startswith(replay)}
    rendered = render_pages(client, pages)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        context = browser.new_context()

        route = install_offline_route(
            context, client, documents={p: rendered[n] for n, p in pages.items()}, strict=True
        )
        page = context.new_page()
        for name, path in pages.items():
            page.goto("http://ux.local" + path)
            assert page.locator("main h1").count() == 1, name
            assert page.locator("main .page-header h1").count() == 1, name
            assert page.locator("main .hero").count() == 0, name
            if page.locator(".crumbs").count():
                assert (
                    page.locator(".crumbs").inner_text() != page.locator("main h1").inner_text()
                ), name
            assert page.evaluate("""() => {
                const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                return new Set(ids).size === ids.length;
            }"""), name
            for field in page.locator(
                "main input:not([type=hidden]), main select, main textarea"
            ).all():
                assert field.evaluate(
                    "e => !!(e.labels.length || e.getAttribute('aria-label')"
                    " || e.getAttribute('aria-labelledby'))"
                ), (name, field.get_attribute("name"))
                if field.get_attribute("name"):
                    assert field.evaluate("e => !!e.form"), name
            for form in page.locator("main form[method=post]").all():
                assert form.locator("input[name=csrf_token]").count() == 1, name
            for region in page.locator("main .data-table-region").all():
                assert region.get_attribute("aria-label"), name
                region.focus()
                assert region.evaluate("e => e === document.activeElement"), name
            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate(NO_OVERFLOW), (name, width)
            if page.locator(".brand-big").count():
                brand = page.locator(".brand-big")
                old = brand.inner_html()
                brand.evaluate(
                    "e => e.textContent ="
                    " 'Northstar International Localization Operations and Research' "
                )
                assert page.evaluate(NO_OVERFLOW), (name, "long brand")
                brand.evaluate("(e, html) => e.innerHTML = html", old)
            for theme in ("light", "dark"):
                page.evaluate("t => {document.documentElement.dataset.theme=t}", theme)
                assert page.evaluate(NO_OVERFLOW), (name, theme)
            page.set_viewport_size({"width": 1440, "height": 900})
            page.locator("body").evaluate("e => e.style.zoom = '2'")
            assert page.evaluate(NO_OVERFLOW), (name, "zoom")
            page.locator("body").evaluate("e => e.style.zoom = ''")
            controls = [
                e
                for e in page.locator(
                    "main a[href], main button:not([disabled]),"
                    " main input:not([type=hidden]):not([disabled]),"
                    " main select, main textarea, main summary"
                ).all()
                if e.is_visible()
            ]
            assert controls, name
            controls[0].focus()
            assert controls[0].evaluate("e => e === document.activeElement"), name
            if len(controls) > 1:
                page.keyboard.press("Tab")
                assert page.evaluate("document.activeElement.tagName !== 'BODY'"), name
                page.keyboard.press("Shift+Tab")
                assert controls[0].evaluate("e => e === document.activeElement"), name
            if "error" in name:
                assert page.locator("[role=alert]").count() >= 1, name
            if name in ("account-mfa-setup", "account-mfa-error", "mfa-enroll", "mfa-enroll-error"):
                assert page.locator("#mfa-setup-key").count() == 1
                assert (
                    page.locator("#mfa-enroll-code").get_attribute("autocomplete")
                    == "one-time-code"
                )
            if name in ("account-mfa-codes", "mfa-codes"):
                assert page.locator("#mfa-codes-copy").count() == 1
                assert page.locator(".mfa-codes li").count() == 10
                assert page.locator("#mfa-codes-copy").get_attribute("readonly") is not None
            if name == "account-off":
                assert page.locator("form[action='/account/name'] button").is_disabled()
                assert page.locator("input[name=theme]").count() == 3
                assert page.locator("[data-card=apikeys]").count() == 0
            if name in ("account", "account-viewer", "account-keys", "account-mfa-on"):
                assert (
                    page.locator("form[action='/account/name'] input[name=name]").get_attribute(
                        "autocomplete"
                    )
                    == "name"
                )
                assert page.locator("input[name=theme]:checked").get_attribute("value") == "system"
                for link in page.locator(".auth-section-nav a").all():
                    assert page.locator(link.get_attribute("href")).count() == 1
            if name == "account-keys":
                # Security batch behavior kept through the restyle: the new key is shown once,
                # read-only.
                box = page.locator("input[aria-label='New API key']")
                assert box.count() == 1 and box.get_attribute("readonly") is not None, name
                assert box.get_attribute("value"), name
            if name in ("account", "account-viewer", "account-mfa-on"):
                assert page.locator("input[aria-label='New API key']").count() == 0, name
            if name == "login":
                assert page.locator("form").get_attribute("action") == "/auth/login"
                assert page.locator("#login-username").get_attribute("autocomplete") == "username"
                assert (
                    page.locator("#login-password").get_attribute("autocomplete")
                    == "current-password"
                )
            if name == "logout":
                assert (
                    page.locator("#logout-confirm").evaluate("e => e.form.getAttribute('action')")
                    == "/auth/logout"
                )
                assert page.locator("main a").get_attribute("href") == "/"
        context.close()
        nojs = browser.new_context(java_script_enabled=False)
        nojs.route("**/*", route)
        page = nojs.new_page()
        for name in ("account", "account-mfa-setup", "mfa-verify", "logout"):
            page.goto("http://ux.local" + pages[name])
            assert page.locator("main form").first.is_visible(), name
        browser.close()
print(f"{len(pages)} real response fixtures; DOM, forms, keyboard, reflow, zoom, no-JS")
print("UX AUTH ACCOUNT OK")
