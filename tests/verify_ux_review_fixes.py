"""Regression contracts for the UX overhaul review fixes (#550 CSS, #552 JS, legacy hero).

Source-level checks always run; the computed-style check needs Chrome and prints a skip
line (and still finishes OK) when Playwright or Chrome is missing.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "app/static"
TEMPLATES = ROOT / "app/templates"


def read(path):
    return path.read_text(encoding="utf-8")


def rule_bodies(css, selector):
    """Declaration blocks of every rule whose selector list contains `selector` verbatim."""
    bodies = []
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in match.group(1):
            bodies.append(match.group(2))
    return bodies


def test_css_tokens_defined():
    css = read(STATIC / "style.css") + read(STATIC / "app_shell.css")
    defined = set(re.findall(r"(--space-\d+)\s*:", css))
    used = set(re.findall(r"var\((--space-\d+)\)", css))
    assert used <= defined, f"undefined spacing tokens (compute to 0): {sorted(used - defined)}"
    print("  (CSS) every var(--space-N) names a defined token OK")


def test_actions_column_is_a_table_cell():
    css = read(STATIC / "style.css")
    bodies = rule_bodies(css, ".data-table :is(th, td).row-actions")
    assert any("display: table-cell" in b for b in bodies), (
        "the generic data-table actions cell must reset the flex display so sticky pins"
    )
    print("  (CSS) data-table actions cells are table cells (sticky pin) OK")


def test_run_graph_region_column():
    css = read(STATIC / "style.css")
    assert "#runGraphCard > :is(.wf-graph, .wf-graph-region) { grid-column: 1;" in css
    assert ":not(.wf-graph):not(.wf-graph-region) { grid-column: 2; }" in css
    print("  (CSS) run graph wrapper stays in the left column OK")


def test_selected_row_has_a_non_colour_cue():
    css = read(STATIC / "style.css")
    assert re.search(r"\.review-selected > :first-child \{ box-shadow: inset 3px 0", css)
    print("  (CSS) selected review row carries a non-colour cue OK")


def test_selected_row_contrast():
    """The accent bar (UI part, 3:1) and row text (4.5:1) read on the selected tint (2 themes)."""
    from tests.test_design_tokens import SELECTORS, contrast, rgb, tokens

    assert len(SELECTORS) == 2, SELECTORS
    for selector in SELECTORS:
        t = tokens(selector)
        for surface in ("--bg", "--panel", "--panel-2"):
            under = rgb(t[surface])
            selected = rgb(t["--color-selected"], under)
            bar = contrast(rgb(t["--color-accent"], selected), selected)
            assert bar >= 3, (selector, surface, "accent bar", round(bar, 2))
            text = contrast(rgb(t["--text"]), selected)
            assert text >= 4.5, (selector, surface, "row text", round(text, 2))
    print("  (CSS) selected-row bar >= 3:1 and text >= 4.5:1 in light and dark OK")


def test_phone_sidebar_no_flash():
    base = read(TEMPLATES / "base.html")
    head = base[base.index("No-flash sidebar UX bootstrap") :]
    boot = head[head.index("<script") : head.index("</script>")]
    set_js = boot.index("classList.add('sb-js')")
    collapsed = boot.index("lsSidebarCollapsed")
    assert set_js < collapsed, "sb-js must be set for every JS-capable load, not only collapsed"
    assert "__locSidebarUxReady" in base and "classList.remove('sb-js')" in base, (
        "the end-of-body fail-open guard must remain"
    )
    print("  (SHELL) head bootstrap pre-sets sb-js on every load; fail-open guard kept OK")


def test_js_fixes():
    sidebar = read(STATIC / "sidebar_ux.js")
    order = sidebar[sidebar.index("function syncOrderFromLs()") :]
    order = order[: order.index("\n  }") + 4]
    assert "setDraggables(true)" in order, "rebuilt sections need draggable after a cross-tab sync"
    assert "a === tipTarget && !reposition" in sidebar, "rail tooltip must not re-run for its link"
    assert 'removeAttribute("title")' in sidebar and "data-rail-title" in sidebar, (
        "the native title must be set aside while the rail tooltip shows"
    )
    assert 'setAttribute("title", saved)' in sidebar, "the title must be restored on hide"
    help_js = read(STATIC / "help_scroll.js")
    assert "requestAnimationFrame" in help_js
    assert "addEventListener('resize', sync)" not in help_js, "resize must be coalesced"
    palette = read(STATIC / "command_palette.js")
    assert "getClientRects" not in palette, "links in a collapsed group stay searchable"
    print("  (JS) cross-tab drag, rail tooltip, help scan and palette contracts OK")


def test_no_legacy_hero():
    for path in sorted(TEMPLATES.glob("*.html")):
        text = read(path)
        assert not re.search(r'class=["\'][^"\']*(?<![\w-])hero(?![\w-])', text), path.name
    audit = read(TEMPLATES / "audit.html")
    assert 'm.page_header("Audit log"' in audit
    print("  (HTML) no legacy hero markup; audit uses the page_header macro OK")


def test_browser_actions_cell():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print("  SKIP: Playwright is not installed")
        return
    css = read(STATIC / "style.css")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Error as error:
            message = str(error).lower()
            if not any(w in message for w in ("executable", "distribution")):
                raise
            print("  SKIP: system Chrome is not installed")
            return
        page = browser.new_page(viewport={"width": 600, "height": 400})
        page.route("**/*", lambda route: route.abort())
        page.set_content(
            "<html><body><div class='data-table-region'><table class='data-table'><thead><tr>"
            "<th scope='col'>Name</th><th scope='col' class='row-actions'>Actions</th></tr></thead>"
            "<tbody><tr><td>One</td><td class='row-actions'><a href='/x'>Open</a>"
            "<form><button type='button'>Go</button></form></td></tr></tbody></table></div>"
            "</body></html>"
        )
        page.add_style_tag(content=css)
        for selector in ("th.row-actions", "td.row-actions"):
            style = page.locator(selector).evaluate(
                "(e)=>({d:getComputedStyle(e).display,p:getComputedStyle(e).position})"
            )
            assert style == {"d": "table-cell", "p": "sticky"}, (selector, style)
        browser.close()
    print("  (BROWSER) actions cells compute to table-cell and sticky OK")


def test_browser_cross_tab_drag():
    """#552: after another tab reorders the sidebar, every rebuilt section is still draggable."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print("  SKIP: Playwright is not installed")
        return
    from tests._ux_smoke import fresh_fixture, install_offline_route

    with fresh_fixture() as (client, _pages, _), sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Error as error:
            message = str(error).lower()
            if not any(w in message for w in ("executable", "distribution")):
                raise
            print("  SKIP: system Chrome is not installed")
            return
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        install_offline_route(page, client, follow_redirects=False)
        page.goto("http://ux.local/projects", wait_until="networkidle")
        page.locator(".sb-edit").click()
        probe = (
            "()=>Array.from(document.querySelectorAll('.side-nav .nav-section'))"
            ".map(s=>s.getAttribute('draggable'))"
        )
        before = page.evaluate(probe)
        assert before and set(before) == {"true"}, before
        keys = page.evaluate(
            "()=>Array.from(document.querySelectorAll('.side-nav .nav-section'))"
            ".map(s=>s.getAttribute('data-nav-key'))"
        )
        page.evaluate(
            "(groups)=>{localStorage.setItem('lsSidebarOrder',JSON.stringify({groups:groups}));"
            "window.dispatchEvent(new StorageEvent('storage',{key:'lsSidebarOrder'}));}",
            list(reversed(keys)),
        )
        after_keys = page.evaluate(
            "()=>Array.from(document.querySelectorAll('.side-nav .nav-section'))"
            ".map(s=>s.getAttribute('data-nav-key'))"
        )
        assert after_keys == list(reversed(keys)), (keys, after_keys)
        after = page.evaluate(probe)
        assert set(after) == {"true"}, f"sections lost draggable after a cross-tab sync: {after}"
        browser.close()
    print("  (BROWSER) sections stay draggable after a cross-tab order change OK")


def main():
    test_css_tokens_defined()
    test_actions_column_is_a_table_cell()
    test_run_graph_region_column()
    test_selected_row_has_a_non_colour_cue()
    test_selected_row_contrast()
    test_phone_sidebar_no_flash()
    test_js_fixes()
    test_no_legacy_hero()
    test_browser_actions_cell()
    test_browser_cross_tab_drag()
    print("UX REVIEW FIXES OK")


if __name__ == "__main__":
    main()
