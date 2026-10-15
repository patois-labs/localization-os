"""Command palette browser and rendered role/flag contracts, offline."""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests._ux_smoke import fresh_fixture, install_offline_route


def render_nav(role, enabled):
    """Render the real server nav template for a role and feature-flag state."""
    import json

    from jinja2 import Environment

    source = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    start = source.index('<nav class="side-nav"')
    template = Environment(autoescape=True).from_string(
        source[start : source.index("</nav>", start) + 6]
    )
    catalog = json.loads((ROOT / "app/i18n/en.json").read_text(encoding="utf-8"))

    def t(_, key):
        value = catalog
        for part in key.split("."):
            value = value[part]
        return value

    return template.render(
        request=None,
        t=t,
        p="/projects",
        me=SimpleNamespace(role=role, customer_id=None),
        names=SimpleNamespace(ledger="Ledger", llmemory="LLMemory"),
        feature_enabled=lambda _, on=enabled: on,
        feature_installed=lambda _: True,
        can_tune_ai=lambda _, admin=(role == "admin"): admin,
    )


def nav_by_group(nav):
    """{group label: [hrefs]} in document order, from the rendered nav markup."""
    import re

    groups = {}
    for chunk in nav.split('<div class="nav-group"')[1:]:
        label = re.sub(r"<[^>]+>", "", chunk.split("</div>")[0].split(">", 1)[1]).strip()
        groups[label] = re.findall(r'<a\s[^>]*href="([^"]*)"', chunk.split("</div>", 1)[1])
    return groups


def static_contract():
    """Role and flag contracts that need no browser (they always run, even on a skip)."""
    for role in ("admin", "viewer"):
        for enabled in (False, True):
            groups = nav_by_group(render_nav(role, enabled))
            hrefs = [h for links in groups.values() for h in links]
            assert ("/users" in hrefs) == (role == "admin"), (role, enabled)
            administration = groups.get("Administration", [])
            assert len(administration) == (3 + int(enabled) if role == "admin" else 0), (
                role,
                enabled,
                administration,
            )
    # Every nav link is reachable by the palette even when its group is collapsed.
    script = (ROOT / "app/static/command_palette.js").read_text(encoding="utf-8")
    assert "getClientRects" not in script, "collapsed groups must stay searchable"
    print("  (STATIC) role and feature-flag nav contracts OK")


def main():
    static_contract()
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print("  SKIP: Playwright is not installed")
        print("collected ids: (skipped)")
        print("COMMAND PALETTE OK")
        return

    with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Error as error:
            message = str(error).lower()
            if not any(w in message for w in ("executable", "distribution")) or not any(
                w in message for w in ("not found", "doesn't exist", "not exist")
            ):
                raise
            print("  SKIP: system Chrome is not installed")
            print("collected ids: (skipped)")
            print("COMMAND PALETTE OK")
            return
        page = browser.new_page(reduced_motion="reduce")

        install_offline_route(page, client, follow_redirects=False)
        page.goto("http://ux.local/projects", wait_until="networkidle")
        trigger = page.locator("[data-command-palette-open]")
        assert trigger.count() == 1
        trigger.click()
        box = page.locator("#command-palette-input")
        assert box.evaluate("(e)=>e===document.activeElement")
        assert (
            page.locator("#command-palette [role=option]").count()
            == page.locator(".side-nav a:visible").count()
        )
        box.fill("TRANSLATION MEMORY")
        assert page.locator("[role=option]").count() == 2
        # The first row is "search everything"; arrow down reaches the matching page.
        assert page.locator("[role=option]").first.get_attribute("data-search-all") == "1"
        assert box.get_attribute("aria-activedescendant") == page.locator(
            "[role=option]"
        ).first.get_attribute("id")
        page.keyboard.press("ArrowDown")
        assert box.get_attribute("aria-activedescendant") == page.locator(
            "[role=option]"
        ).last.get_attribute("id")
        page.keyboard.press("Enter")
        page.wait_for_url("**/tm")
        page.locator("[data-command-palette-open]").focus()
        page.keyboard.press("Control+k")
        page.keyboard.press("Escape")
        assert page.locator("[data-command-palette-open]").evaluate(
            "(e)=>e===document.activeElement"
        )
        page.keyboard.press("Meta+k")
        assert box.is_visible()
        box.fill("palette & query")
        page.keyboard.press("Enter")
        page.wait_for_url("**/search?q=palette*")
        assert page.locator("#topsearch-q").input_value() == "palette & query"
        page.locator("[data-command-palette-open]").focus()
        page.keyboard.press("/")
        assert not page.locator("#command-palette").is_visible()
        page.evaluate(
            "document.body.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'k',ctrlKey:true,isComposing:true,bubbles:true}))"
        )
        assert not page.locator("#command-palette").is_visible()
        # Inputs, contenteditable, editor bindings, composition and handled events win.
        page.locator("#topsearch-q").focus()
        page.keyboard.press("Control+k")
        assert not page.locator("#command-palette").is_visible()
        page.locator("[data-command-palette-open]").click()
        box.fill("<img src=x onerror=alert(1)> $&")
        assert page.locator("#command-palette-results img").count() == 0
        assert (
            "<img src=x onerror=alert(1)> $&"
            in page.locator("#command-palette-results").inner_text()
        )
        page.keyboard.press("Escape")
        page.evaluate("""() => {
              const e = document.createElement('div'); e.contentEditable='true';
              document.body.append(e); e.focus();
              e.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,bubbles:true}));
              e.remove();
              document.addEventListener('keydown',
                e=>{if(e.key==='k')e.preventDefault()}, {once:true});
              document.body.dispatchEvent(new KeyboardEvent('keydown',
                {key:'k',ctrlKey:true,bubbles:true,cancelable:true}));
            }""")
        assert not page.locator("#command-palette").is_visible()
        page.goto("http://ux.local" + pages["editor"], wait_until="networkidle")
        page.locator("textarea").first.focus()
        page.keyboard.press("Control+k")
        assert not page.locator("#command-palette").is_visible()
        # Render actual server nav gates for admin and non-admin, flags off/on.
        for role in ("admin", "viewer"):
            for enabled in (False, True):
                page.goto("http://ux.local/projects", wait_until="networkidle")
                nav = render_nav(role, enabled)
                page.locator(".side-nav").evaluate("(e,html)=>e.outerHTML=html", nav)
                expected = page.locator(".side-nav a:visible").evaluate_all(
                    '(es)=>es.map(e=>e.getAttribute("href"))'
                )
                page.locator("[data-command-palette-open]").click()
                assert (
                    page.locator("#command-palette [data-href]").evaluate_all(
                        "(es)=>es.map(e=>e.dataset.href)"
                    )
                    == expected
                )
                assert ("/users" in expected) == (role == "admin")
                box.fill("administration")
                assert page.locator("#command-palette [data-href]").count() == (
                    3 + int(enabled) if role == "admin" else 0
                )
                page.keyboard.press("Escape")
        page.locator(".side-nav a").first.evaluate('(e)=>e.textContent="Café"')
        trigger.click()
        box.fill("CAFE")
        assert page.locator("#command-palette [data-href]").count() == 1
        # The count names page matches only, not the always-present search row.
        assert page.locator("#command-palette-count").inner_text() == "1 result"
        box.fill("zzzqq-no-match")
        assert (
            page.locator("#command-palette-count").inner_text()
            == "No pages match. Press Enter to search everything."
        )
        assert page.locator("#command-palette [role=option]").count() == 1
        box.fill("CAFE")
        hint_css = page.locator("[data-command-palette-open] kbd").evaluate(
            "(e)=>({b:getComputedStyle(e).borderTopWidth,"
            "p:getComputedStyle(e.parentElement).borderTopWidth})"
        )
        assert hint_css == {"b": "0px", "p": "1px"}, hint_css
        page.evaluate((ROOT / "tests/fixtures/axe/axe.min.js").read_text(encoding="utf-8"))
        violations = page.evaluate(
            "async()=> (await axe.run('#command-palette')).violations.map(v=>({id:v.id,"
            "nodes:v.nodes.map(n=>({html:n.html,summary:n.failureSummary}))}))"
        )
        assert not violations, violations
        for width in (390, 320, 720):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert box.is_visible()
            page.keyboard.press("Escape")
            # #564 p8 (ux564b, 10-08): phones have no keyboard, so the Ctrl+K hint is
            # hidden at 600px and below; clicking the search field opens the same dialog.
            if width <= 600:
                assert not trigger.is_visible(), f"Ctrl+K hint shows at {width}px"
                page.locator("#topsearch-q").click()
            else:
                assert trigger.is_visible(), f"Ctrl+K hint missing at {width}px"
                trigger.click()
            assert page.locator("#command-palette").is_visible(), width
            assert page.locator("#command-palette [data-href]").count() == len(expected)
            page.keyboard.press("Tab")
            assert page.locator("[data-command-palette-close]").evaluate(
                "(e)=>e===document.activeElement"
            )
            page.keyboard.press("Tab")
            assert box.evaluate("(e)=>e===document.activeElement")
        page.locator('#command-palette [data-href="/"]').click()
        page.wait_for_url("http://ux.local/")
        page.keyboard.press("Escape")
        page.goto("http://ux.local/projects", wait_until="networkidle")
        hint = page.locator("[data-command-palette-open]")
        # Dimmed by dropping the outline, not opacity: the hint stays an operable button,
        # so its text keeps WCAG 1.4.3 contrast (opacity .45 measured 2.0 to 3.2:1).
        border = "(e)=>getComputedStyle(e).borderTopColor"
        assert hint.evaluate("(e)=>getComputedStyle(e).opacity") == "1"
        assert hint.evaluate(border) != "rgba(0, 0, 0, 0)"
        page.locator("#topsearch-q").focus()
        assert hint.evaluate(border) == "rgba(0, 0, 0, 0)"
        assert hint.evaluate("(e)=>getComputedStyle(e).opacity") == "1"
        assert hint.locator("kbd").inner_text() in ("Ctrl+K", "Cmd+K")
        # #595: the top bar is sticky and Chrome scrolled a scrolled page up 56px per keystroke
        # typed into it. Typing there now opens the search dialog with what was typed, and
        # neither the first keystroke nor closing the dialog may move the page.
        page.set_viewport_size({"width": 1280, "height": 700})
        page.goto("http://ux.local/projects", wait_until="networkidle")
        page.evaluate("document.querySelector('.content').style.minHeight = '3000px'")
        page.evaluate("window.scrollTo(0, 400)")
        page.evaluate("document.getElementById('topsearch-q').focus({preventScroll: true})")
        assert page.evaluate("document.activeElement.id") == "topsearch-q"
        assert page.evaluate("window.scrollY") == 400
        page.keyboard.type("a")
        page.wait_for_timeout(250)
        assert page.locator("#command-palette").is_visible(), "typing in the top bar opens search"
        assert box.input_value() == "a", box.input_value()
        assert page.locator("#topsearch-q").input_value() == "", "the top bar box is put back"
        assert page.evaluate("window.scrollY") == 400, "the first keystroke scrolled the page"
        page.keyboard.type("tl")
        page.wait_for_timeout(500)
        assert box.input_value() == "atl"
        assert page.evaluate("window.scrollY") == 400, "typing in the dialog scrolled the page"
        page.keyboard.press("Escape")
        assert page.evaluate("document.activeElement.id") == "topsearch-q"
        assert page.evaluate("window.scrollY") == 400, "closing the dialog scrolled the page"
        # Tabbing into the box must not open anything (no context change on focus).
        page.keyboard.press("Tab")
        page.keyboard.press("Shift+Tab")
        assert not page.locator("#command-palette").is_visible()
        # (Tabbing onto the sticky bar scrolls the page itself; that is a focus scroll, not typing.)
        page.evaluate("window.scrollTo(0, 400)")
        # Clicking the box opens the same dialog.
        search_box = page.locator("#topsearch-q").bounding_box()
        page.mouse.click(
            search_box["x"] + search_box["width"] / 2, search_box["y"] + search_box["height"] / 2
        )
        assert page.locator("#command-palette").is_visible()
        assert page.evaluate("window.scrollY") == 400
        page.keyboard.press("Escape")
        # #552: a link inside a collapsed sidebar group has no layout box but is still a
        # permitted page, so the palette must find it.
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto("http://ux.local/projects", wait_until="networkidle")
        group = page.locator('.side-nav .nav-section[data-nav-key="language-assets"]')
        group.locator(".nav-disclosure").click()
        assert "nav-section-closed" in (group.get_attribute("class") or "")
        assert not page.locator('.side-nav a[href="/tm"]').is_visible()
        page.locator("[data-command-palette-open]").click()
        box.fill("translation memory")
        found = page.locator("#command-palette [data-href]").evaluate_all(
            "(es)=>es.map(e=>e.dataset.href)"
        )
        assert "/tm" in found, f"a collapsed group's link must stay searchable: {found}"
        page.keyboard.press("Escape")
        # #595: live grouped results, combobox/listbox semantics, Enter opens /search.
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto("http://ux.local/projects", wait_until="networkidle")
        page.locator("[data-command-palette-open]").click()
        box.fill("atlas")
        page.wait_for_selector("#command-palette [data-result-href]")
        headings = page.locator("#command-palette .command-palette-heading").all_inner_texts()
        assert "Projects" in headings, headings
        assert (
            box.get_attribute("role") == "combobox" and box.get_attribute("aria-expanded") == "true"
        )
        assert page.locator("#command-palette-results").get_attribute("role") == "listbox"
        for group in page.locator("#command-palette-results [role=group]").all():
            label = group.get_attribute("aria-labelledby")
            assert page.locator("#" + label).count() == 1, label
        assert (
            page.locator("#command-palette-results [role=option]").first.get_attribute(
                "data-search-all"
            )
            == "1"
        )
        count = page.locator("#command-palette-count").inner_text()
        assert count.endswith("results"), count
        page.evaluate((ROOT / "tests/fixtures/axe/axe.min.js").read_text(encoding="utf-8"))
        violations = page.evaluate(
            "async()=> (await axe.run('#command-palette')).violations.map(v=>v.id)"
        )
        assert not violations, violations
        # Arrow keys reach a result; Escape closes; the dialog never leaves a stale selection.
        page.keyboard.press("ArrowDown")
        selected = page.locator('#command-palette [aria-selected="true"]')
        assert selected.count() == 1
        page.keyboard.press("ArrowUp")
        assert page.locator("#command-palette [data-search-all][aria-selected=true]").count() == 1
        page.keyboard.press("Enter")
        page.wait_for_url("**/search?q=atlas")
        assert page.locator("#sg-projects").count() == 1
        assert page.locator("#topsearch-q").input_value() == "atlas"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # A hit opens its page.
        page.locator("[data-command-palette-open]").click()
        box.fill("getting-started")
        page.wait_for_selector("#command-palette [data-result-href]")
        page.locator("#command-palette [data-result-href]").first.click()
        page.wait_for_url("**/documents/*")
        # A too-short query asks for more and fetches nothing.
        page.goto("http://ux.local/projects", wait_until="networkidle")
        page.locator("[data-command-palette-open]").click()
        box.fill("a")
        assert page.locator("#command-palette [data-search-all]").count() == 0
        assert "2 characters" in page.locator("#command-palette .command-palette-note").inner_text()
        # The results page and the dialog at 390px: no sideways scroll.
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.keyboard.press("Escape")
            page.goto("http://ux.local/search?q=atlas", wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            # #564 p8: no Ctrl+K hint on a phone; the search field opens the dialog.
            assert not page.locator("[data-command-palette-open]").is_visible(), width
            page.locator("#topsearch-q").click()
            box.fill("atlas")
            page.wait_for_selector("#command-palette [data-result-href]")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert page.evaluate(
                "(()=>{const d=document.getElementById('command-palette');"
                "return d.scrollWidth <= d.clientWidth})()"
            ), width
        browser.close()
    print("COMMAND PALETTE OK")


if __name__ == "__main__":
    main()
