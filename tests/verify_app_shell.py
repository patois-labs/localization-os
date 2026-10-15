"""App-shell rendered permissions, layout compatibility and Chrome keyboard checks.

Uses the shared offline UX fixture; no live install or provider calls.
"""

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from html.parser import HTMLParser

from jinja2 import Environment

from tests._ux_smoke import fresh_fixture


class NavLinks(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.link = None
        self.hidden = 0
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.link = [attrs["href"], [], attrs.get("class", "")]
        if self.hidden:
            self.hidden += 1
        elif attrs.get("class") == "nav-glyph":
            self.hidden = 1

    def handle_endtag(self, tag):
        if self.hidden:
            self.hidden -= 1
        if tag == "a" and self.link:
            href, words, active = self.link
            self.links.append((href, " ".join(" ".join(words).split()), active))
            self.link = None

    def handle_data(self, data):
        if self.link and not self.hidden:
            self.link[1].append(data)


def nav_contract():
    baseline = (ROOT / "tests/fixtures/app_shell_legacy_nav.html").read_text(encoding="utf-8")
    source = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    start = source.index('<nav class="side-nav"')
    candidate = source[start : source.index("</nav>", start) + 6]
    # Compare every original anchor attribute and Jinja conditional literally,
    # including active matches for paths outside the rendered /projects matrix.
    assert re.findall(r"<a\b[^>]*>", baseline) == re.findall(r"<a\b[^>]*>", candidate)
    assert re.findall(r"{%.*?%}", baseline) == re.findall(r"{%.*?%}", candidate)
    flags = [
        "queries",
        "worklist",
        "vendors",
        "images",
        "data_export",
        "reports",
        "routing",
    ]
    env = Environment(autoescape=True)
    templates = {text: env.from_string(text) for text in (baseline, candidate)}
    cases = 0
    for locale in ("en", "de", "es", "fr"):
        catalog = json.loads((ROOT / f"app/i18n/{locale}.json").read_text(encoding="utf-8"))
        english = json.loads((ROOT / "app/i18n/en.json").read_text(encoding="utf-8"))

        def lookup(_, key, *, catalog=catalog, english=english):
            def get(tree):
                for part in key.split("."):
                    tree = tree[part]
                return tree

            try:
                return get(catalog)
            except KeyError:
                return get(english)

        for role in ("admin", "linguist", "pm", "viewer"):
            for bound in (None, 1):
                for tune in (False, True):
                    for enabled in (set(), set(flags), *({f} for f in flags)):
                        context = {
                            "request": None,
                            "t": lookup,
                            "p": "/projects",
                            "me": SimpleNamespace(role=role, customer_id=bound),
                            "names": SimpleNamespace(ledger="Ledger", llmemory="LLMemory"),
                            "feature_enabled": lambda f, *, enabled=enabled: f in enabled,
                            "feature_installed": lambda _: True,
                            "can_tune_ai": lambda _, *, tune=tune: tune,
                        }

                        def links(template, *, context=context):
                            return sorted(NavLinks(templates[template].render(**context)).links)

                        assert links(baseline) == links(candidate), (
                            locale,
                            role,
                            bound,
                            tune,
                            enabled,
                        )
                        cases += 1
    assert "data-shell-ancestors" in source
    assert source.count('class="hero') == 0
    ancestor_start = source.index("{% set _shell_ancestor =")
    ancestor_end = source.index("{% endfor %}", ancestor_start) + len("{% endfor %}")
    ancestor_template = env.from_string(
        "{% set _shell_nav %}"
        + candidate
        + "{% endset %}"
        + source[ancestor_start:ancestor_end]
        + "{{ _shell_ancestor.label }}"
    )
    context["me"] = SimpleNamespace(role="admin", customer_id=None)
    for path, key in (
        ("/users", "nav.group_administration"),
        ("/security", "nav.group_administration"),
        ("/projects/123", "nav.group_workspace"),
        ("/unmatched-page", None),
    ):
        context["p"] = path
        assert ancestor_template.render(**context).strip() == (lookup(None, key) if key else ""), (
            path
        )
    print(f"  Navigation href, label, active-match parity: {cases} role/flag/locale renders")


def launch_chrome(pw):
    from playwright.sync_api import Error

    try:
        return pw.chromium.launch(channel="chrome")
    except Error as error:
        message = str(error).lower()
        if not any(word in message for word in ("executable", "distribution")) or not any(
            word in message for word in ("not found", "doesn't exist", "not exist")
        ):
            raise
        print("  SKIP: system Chrome is not installed")
        print("collected ids: (skipped)")
        return None


def browser_contract():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print("  SKIP: Playwright is not installed")
        print("collected ids: (skipped)")
        return
    # Exercise Chrome absence without uninstalling the developer's browser.
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import Mock

    missing = Mock()
    missing.chromium.launch.side_effect = Error(
        "Chromium distribution 'chrome' is not found at fixture path"
    )
    output = StringIO()
    with redirect_stdout(output):
        assert launch_chrome(missing) is None
    assert "collected ids: (skipped)" in output.getvalue()
    missing.chromium.launch.side_effect = Error("Unexpected browser crash")
    try:
        launch_chrome(missing)
    except Error:
        pass
    else:
        raise AssertionError("Browser failures must not be hidden as skips")
    with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
        browser = launch_chrome(pw)
        if browser is None:
            return
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
        )

        def route(r):
            u = urlsplit(r.request.url)
            if u.netloc != "ux.local" or r.request.method != "GET":
                r.abort()
                return
            response = client.get(
                u.path + ("?" + u.query if u.query else ""), follow_redirects=False
            )
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            }
            r.fulfill(status=response.status_code, headers=headers, body=response.content)

        context.route("**/*", route)
        context.add_init_script(
            """window.sidebarWrites = [];
          const set = Storage.prototype.setItem, del = Storage.prototype.removeItem;
          Storage.prototype.setItem = """
            """function(k,v) {if(k.startsWith('lsSidebar')) sidebarWrites.push(k); """
            """return set.call(this,k,v)};
          Storage.prototype.removeItem = """
            """function(k) {if(k.startsWith('lsSidebar')) sidebarWrites.push(k); """
            """return del.call(this,k)};"""
        )
        page = context.new_page()
        # Render the actual server nav for each role, then run the production JS.
        source = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        start = source.index('<nav class="side-nav"')
        nav_template = Environment(autoescape=True).from_string(
            source[start : source.index("</nav>", start) + 6]
        )
        catalog = json.loads((ROOT / "app/i18n/en.json").read_text(encoding="utf-8"))

        def lookup(_, key):
            value = catalog
            for part in key.split("."):
                value = value[part]
            return value

        for role, bound, enabled in (
            ("admin", None, True),
            ("pm", None, True),
            ("viewer", 1, True),
            ("pm", None, False),
            ("viewer", 1, False),
        ):
            nav = nav_template.render(
                request=None,
                t=lookup,
                p="/",
                me=SimpleNamespace(role=role, customer_id=bound),
                names=SimpleNamespace(ledger="Ledger", llmemory="LLMemory"),
                feature_enabled=lambda _, *, enabled=enabled: enabled,
                feature_installed=lambda _: True,
                can_tune_ai=lambda _: True,
            )
            # A future unmapped destination must survive in its server section.
            nav = nav.replace("</nav>", '<a href="/future-nav">Future destination</a></nav>')
            expected = sorted(href for href, _, _ in NavLinks(nav).links)

            def nav_route(nav_html):
                def handler(r):
                    html = client.get("/").text
                    html = re.sub(
                        r'<nav class="side-nav".*?</nav>', lambda _: nav_html, html, flags=re.S
                    )
                    r.fulfill(status=200, content_type="text/html", body=html)

                return handler

            role_route = nav_route(nav)
            page.route("http://ux.local/", role_route)
            page.goto("http://ux.local/", wait_until="networkidle")
            assert (
                page.locator(".side-nav a").evaluate_all(
                    '(es)=>es.map(e=>e.getAttribute("href")).sort()'
                )
                == expected
            ), role
            assert page.locator('[data-nav-key="system"] a[href="/future-nav"]').count() == 1
            group = "administration" if role == "admin" else "intelligence"
            assert page.locator(f'[data-nav-key="{group}"] a[href="/setup-ai"]').count() == 1
            if role != "admin":
                assert page.locator('.nav-section[data-nav-key="administration"]').count() == 0
                groups = page.locator(".nav-section").evaluate_all(
                    "(es)=>es.map(e=>e.dataset.navKey)"
                )
                assert groups.index("intelligence") < groups.index("system")
            if bound:
                assert page.locator('[data-nav-key="intelligence"] a[href="/audit"]').count() == 1
            page.unroute("http://ux.local/", role_route)

        def capture(name):
            destination = os.environ.get("UX_SHELL_SCREENSHOTS")
            if destination:
                folder = Path(destination)
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder / (name + ".png")))

        for name in ("dashboard", "projects", "project", "tm"):
            page.goto("http://ux.local" + pages[name], wait_until="networkidle")
            assert page.locator("main h1").count() == 1, name
            assert (
                page.locator("main h1").inner_text().strip()
                != page.locator(".crumbs").inner_text().strip()
            ), name
            assert (
                page.locator('.side-nav [data-nav-key="language-assets"] a[href="/tm"]').count()
                == 1
            )
            assert page.evaluate("sidebarWrites") == []
            assert page.locator(".side-nav a svg").count() == page.locator(".side-nav a").count()
            assert page.locator("form.topsearch-form").get_attribute("action") == "/search"
            assert page.locator("#topsearch-q").get_attribute("name") == "q"
            if name == "project":
                assert page.locator(".crumbs").inner_text().strip() == "Workspace / Projects"
            if name == "dashboard":
                assert page.locator("#topsearch-q").evaluate(
                    """e => {
                    const s = getComputedStyle(e), c = """
                    """document.createElement('canvas').getContext('2d');
                    c.font = s.font;
                    return c.measureText(e.placeholder).width <= e.clientWidth """
                    """- parseFloat(s.paddingLeft) - parseFloat(s.paddingRight);
                }"""
                ), "Search placeholder clips at 1440"
        # Disclosure Enter/Space and rail retain every link and name.
        disclosure = page.locator('[data-nav-key="language-assets"] .nav-disclosure')
        disclosure.focus()
        page.keyboard.press("Enter")
        assert disclosure.get_attribute("aria-expanded") == "false"
        assert not page.locator('.side-nav a[href="/tm"]').is_visible()
        page.locator(".sb-collapse").focus()
        page.keyboard.press("Enter")
        assert page.locator('.side-nav a[href="/tm"]').is_visible()
        for a in page.locator(".side-nav a").all():
            assert a.get_attribute("aria-label") and a.get_attribute("title")
            a.focus()
            assert a.evaluate("(e) => e === document.activeElement"), (
                a.get_attribute("href"),
                page.evaluate("document.activeElement.outerHTML"),
            )
        page.locator('.side-nav a[href="/tm"]').focus()
        assert page.locator("#shell-nav-tooltip").is_visible(), page.locator(
            "#shell-nav-tooltip"
        ).evaluate("(e)=>e.outerHTML")
        capture("rail-keyboard-console-dark-1440")
        page.keyboard.press("Escape")
        assert not page.locator("#shell-nav-tooltip").is_visible()
        # The rail's scroll event (queued by the focus scroll above) can land AFTER Escape;
        # it must not bring a dismissed tooltip back (remote sweep flake, 2026-10-10).
        page.locator(".side-nav").dispatch_event("scroll")
        assert not page.locator("#shell-nav-tooltip").is_visible(), "Escape must stick"
        page.locator(".sb-collapse").click()
        # Exact legacy containers, group and item ordering, custom width, no writes.
        legacy = {
            "groups": ["system", "automation", "workspace", "intelligence", "administration"],
            "items": {
                "workspace": ["/customers", "/projects", "/"],
                "automation": ["/tm", "/glossaries", "/images", "/workflows", "/dormant"],
            },
        }
        page.evaluate(
            '(o) => {localStorage.setItem("lsSidebarOrder", JSON.stringify(o));'
            ' localStorage.setItem("lsSidebarWidth", "310")}',
            legacy,
        )
        page.reload(wait_until="networkidle")
        assert page.evaluate("sidebarWrites") == []
        assert (
            page.locator(".sidebar").evaluate("(e) => Math.round(e.getBoundingClientRect().width)")
            == 310
        )
        assert (
            page.locator(".nav-section").evaluate_all("(es) => es.map(e=>e.dataset.navKey)")
            == legacy["groups"]
        )
        assert (
            page.locator('[data-nav-key="workspace"] > a').evaluate_all(
                '(es)=>es.slice(0,3).map(e=>e.getAttribute("href"))'
            )
            == legacy["items"]["workspace"]
        )
        assert page.locator('[data-nav-key="automation"] > a').first.get_attribute("href") == "/tm"
        # Missing labels and unexpected breadcrumb errors cannot stop saved order.
        for defect in ("label", "section", "throw"):
            page.evaluate(
                """defect => {
                const active = document.querySelector('.side-nav a.on');
                window.restoreCrumbProbe = () => {};
                if (defect === 'label') {
                    const label = active.querySelector('.nav-label'); label.remove();
                    window.restoreCrumbProbe = () => active.appendChild(label);
                } else {
                    active.closest = () => { if (defect === 'throw') """
                """throw new Error('crumb probe'); return null; };
                    window.restoreCrumbProbe = () => delete active.closest;
                }
                window.dispatchEvent(new StorageEvent('storage', {key:'lsSidebarOrder'}));
            }""",
                defect,
            )
            assert (
                page.locator(".nav-section").evaluate_all("(es)=>es.map(e=>e.dataset.navKey)")
                == legacy["groups"]
            ), defect
            assert (
                page.locator('[data-nav-key="workspace"] > a').first.get_attribute("href")
                == "/customers"
            )
            page.evaluate("window.restoreCrumbProbe()")
        from app.features import FEATURES
        from app.services import settings

        images_key = next(f.setting_key for f in FEATURES if f.slug == "images")
        settings.set_many({images_key: "0"})
        settings.invalidate_cache()
        page.reload(wait_until="networkidle")
        assert page.locator('.side-nav a[href="/images"]').count() == 0
        page.locator(".sb-edit").click()
        page.locator('[data-nav-key="automation"] > .sb-move').nth(1).locator(
            ".sb-move-down"
        ).click()
        saved = page.evaluate('JSON.parse(localStorage.getItem("lsSidebarOrder"))')
        assert "/dormant" in saved["items"]["automation"]
        assert "/images" in saved["items"]["automation"]
        page.locator(".sb-done").click()
        settings.set_many({images_key: "1"})
        settings.invalidate_cache()
        page.reload(wait_until="networkidle")
        assert page.locator('[data-nav-key="automation"] a[href="/images"]').count() == 1
        # Storage event switches layouts without writing or fabricating links.
        page.locator(".sb-edit").click()
        page.evaluate(
            'localStorage.removeItem("lsSidebarOrder");'
            " window.sidebarWrites=[];"
            ' window.dispatchEvent(new StorageEvent("storage", {key:"lsSidebarOrder"}))'
        )
        assert page.locator('[data-nav-key="language-assets"] a[href="/tm"]').count() == 1
        assert page.evaluate("sidebarWrites") == []
        page.locator(".sb-done").click()
        page.evaluate(
            '(o)=>{localStorage.setItem("lsSidebarOrder",JSON.stringify(o));'
            ' window.dispatchEvent(new StorageEvent("storage",{key:"lsSidebarOrder"}))}',
            saved,
        )
        assert page.locator(".sb-move").count() == 0
        page.evaluate(
            'localStorage.removeItem("lsSidebarOrder");'
            ' window.dispatchEvent(new StorageEvent("storage",{key:"lsSidebarOrder"}))'
        )
        # A save with the new group keys stays in the new model on reload.
        page.locator(".sb-edit").click()
        page.locator('[data-nav-key="language-assets"] > .sb-move').nth(1).locator(
            ".sb-move-down"
        ).click()
        fresh = page.locator('[data-nav-key="language-assets"] > a').evaluate_all(
            '(es)=>es.map(e=>e.getAttribute("href"))'
        )
        page.locator(".sb-done").click()
        page.reload(wait_until="networkidle")
        assert (
            page.locator('[data-nav-key="language-assets"] > a').evaluate_all(
                '(es)=>es.map(e=>e.getAttribute("href"))'
            )
            == fresh
        )
        assert page.evaluate("sidebarWrites") == []
        for raw in (
            "{broken",
            "null",
            "[]",
            '{"groups":["unknown"],"items":{"workspace":["/invented"]}}',
        ):
            page.evaluate('(s)=>localStorage.setItem("lsSidebarOrder",s)', raw)
            page.reload(wait_until="networkidle")
            assert page.locator('.side-nav a[href="/invented"]').count() == 0
            assert page.evaluate("sidebarWrites") == []
        page.evaluate('localStorage.removeItem("lsSidebarOrder")')
        page.reload(wait_until="networkidle")
        page.set_viewport_size({"width": 390, "height": 844})
        for name in ("dashboard", "projects", "tm"):
            page.goto("http://ux.local" + pages[name], wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
            toggle = page.locator("[data-nav-toggle]")
            toggle.focus()
            page.keyboard.press("Enter")
            assert page.locator(".main").get_attribute("inert") is not None
            if name == "dashboard":
                capture("drawer-console-dark-390")
            page.locator('[data-nav-key="administration"] .nav-disclosure').focus()
            page.keyboard.press("Space")
            focusables = page.locator(".sidebar").evaluate(
                """e=>Array.from(e.querySelectorAll('a[href],button,[tabindex="0"]'))"""
                """.filter(x=>!x.disabled&&x.getClientRects().length&&getComputedStyle(x).visibility!=='hidden')"""
                """.map(x=>{x.dataset.testFocus='1';return x}) .length"""
            )
            assert focusables > 5
            ends = page.locator('[data-test-focus="1"]')
            ends.last.focus()
            page.keyboard.press("Tab")
            assert ends.first.evaluate("(e)=>e===document.activeElement")
            page.keyboard.press("Shift+Tab")
            assert ends.last.evaluate("(e)=>e===document.activeElement")
            page.keyboard.press("Escape")
            assert toggle.evaluate("(e)=>e===document.activeElement")
            assert toggle.get_attribute("aria-expanded") == "false"
        page.set_viewport_size({"width": 320, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # 200% desktop-equivalent CSS viewport and long organization name.
        page.set_viewport_size({"width": 720, "height": 450})
        page.locator(".brand .nav-label").evaluate(
            '(e)=>e.textContent="Northstar International Localization Operations Workspace"'
        )
        page.locator("[data-nav-toggle]").click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.keyboard.press("Escape")
        # New disclosures/rail/edit controls must stay axe-clean in this fixture.
        page.locator("[data-nav-toggle]").click()
        page.evaluate((ROOT / "tests/fixtures/axe/axe.min.js").read_text(encoding="utf-8"))
        violations = page.evaluate(
            """async()=> (await axe.run('.sidebar', """
            """{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>v.id)"""
        )
        assert not violations, violations
        nojs = browser.new_context(
            java_script_enabled=False, viewport={"width": 390, "height": 844}
        )
        nojs.route("**/*", route)
        fallback = nojs.new_page()
        fallback.goto("http://ux.local/projects", wait_until="networkidle")
        assert fallback.locator('.side-nav a[href="/projects"]').is_visible()
        assert (
            fallback.locator("main h1").inner_text().strip()
            != fallback.locator(".crumbs").inner_text().strip()
        )
        for path, ancestor in (
            ("/projects", "Workspace"),
            ("/users", "Administration"),
            ("/security", "Administration"),
            ("/tm", "Automation"),
        ):
            fallback.goto("http://ux.local" + path, wait_until="networkidle")
            assert fallback.locator("[data-shell-ancestors]").inner_text().strip() == ancestor, path
        # An unmatched path must not claim Workspace as an ancestor.
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto("http://ux.local/", wait_until="networkidle")
        page.locator(".side-nav a.on").evaluate('(e)=>e.classList.remove("on")')
        page.evaluate('window.dispatchEvent(new StorageEvent("storage", {key:"lsSidebarOrder"}))')
        assert page.locator("[data-shell-ancestors]").inner_text() == ""
        nojs.close()
        context.close()
        browser.close()
    print(
        "  Chrome: ancestors, fresh/legacy/malformed order, dormant keys, rail, drawer,"
        " reflow passed"
    )


if __name__ == "__main__":
    nav_contract()
    browser_contract()
    print("APP SHELL OK")
