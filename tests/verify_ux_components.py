"""Rendered shared component and keyboard contracts. Documented in DESIGN.md."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from tests.test_design_tokens import (
    SELECTORS,
    test_contrast_matrix,
    test_reduced_motion_contract,
    test_shared_geometry_and_aliases,
)

for selector in SELECTORS:
    test_contrast_matrix(selector)
    test_shared_geometry_and_aliases(selector)
test_reduced_motion_contract()

env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
env.globals["static_v"] = lambda name: "fixture"
source = """{% import '_macros.html' as m %}
{{ m.page_header('Example <title>', meta='Metadata', status=m.status_badge('approved'),
                 primary='<unsafe>', secondary='Secondary') }}
{{ m.action_bar('Actions', primary='Save', secondary='Cancel', destructive='Delete',
                consequence='Existing warning') }}
{% call m.data_table('Records', [{'label':'Name'}, {'label':'Count','numeric':true},
                     {'label':'Actions','actions':true}], caption='Existing records') %}
<tr><th scope="row">Item</th><td class="numeric">12</td>
<td class="row-actions"><a href="#owner" aria-label="Open Item">Open</a></td></tr>{% endcall %}
{{ m.empty_state('No records', action='Existing action') }}
<form id="owner">{% call m.form_section('fields', 'Details', help='Help text') %}
<label>Name<input name="name" value="unchanged"></label>{% endcall %}</form>
{% call m.disclosure('More') %}<p>Additional content</p>{% endcall %}
<form id="tab-form">{% call(section) m.tabs('sample', 'Sections', [{'id':'one','label':'One'},
    {'id':'two','label':'Two'}, {'id':'three','label':'Three'}]) %}
<h2>{{ section.label }}</h2>{% if section.id != 'one' %}
<label>Required<input id="required-{{ section.id }}" required
                     name="required-{{ section.id }}"></label>
{% endif %}{% endcall %}<button>Submit</button></form>
<button id="opener" data-component-open="sample-dialog">Open</button>
{% call m.dialog('sample-dialog', 'Details', 'Close') %}
<label>Value<input id="dialog-field"></label>{% endcall %}
{% call m.dialog('drawer', 'Drawer', 'Close', drawer=true) %}<p>Drawer content</p>{% endcall %}
{{ m.toast('Saved') }}{{ m.toast('Existing outcome', announce=true) }}
{{ m.step_indicator('Progress', [
    {'label':'Translate','status':'completed','status_label':'Completed'},
    {'label':'Review','status':'approved','status_label':'Approved','current':true}]) }}
{% call m.card() %}<h2>Card</h2>{% endcall %}
{{ m.component_script() }}
"""
html = env.from_string(source).render()
legacy = sorted(
    p.name
    for p in (ROOT / "app/templates").glob("*.html")
    # The exact `hero` class token only: `download-hero` and `wf-hero-row` are unrelated.
    if re.search(r'class=["\'][^"\']*(?<![\w-])hero(?![\w-])', p.read_text(encoding="utf-8"))
)
# Every page uses the page_header macro now, so the legacy alias is gone for good.
assert not legacy, (len(legacy), legacy)
assert not re.search(
    r"\.hero(?![\w-])", (ROOT / "app/static/style.css").read_text(encoding="utf-8")
)

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome")
    nojs = browser.new_context(java_script_enabled=False)
    fallback = nojs.new_page()
    fallback.set_content(html)
    assert fallback.locator("[data-component-panel]:visible").count() == 3
    assert fallback.get_by_role("tab").count() == 0
    assert fallback.locator(".component-tab-list a").count() == 3
    nojs.close()
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    page.set_content(html)
    assert page.locator("h1").count() == 1
    assert page.locator("h1").inner_text() == "Example <title>"
    assert page.locator(".page-header-primary").inner_text() == "<unsafe>"
    assert page.get_by_role("group", name="Actions").count() == 1
    assert page.locator(".action-bar-consequence").inner_text() == "Existing warning"
    assert page.get_by_role("region", name="Records").get_attribute("tabindex") == "0"
    assert page.locator('thead th[scope="col"]').count() == 3
    assert page.locator("caption").inner_text() == "Existing records"
    assert page.locator(".numeric").count() == 2
    assert page.locator(".empty-state p").inner_text() == "No records"
    assert page.locator(".empty-state > div").inner_text() == "Existing action"
    assert (
        page.get_by_role("group", name="Details").get_attribute("aria-describedby") == "fields-help"
    )
    assert page.locator('[name="name"]').evaluate("(e) => e.form.id") == "owner"
    assert page.locator('[name="name"]').input_value() == "unchanged"
    assert page.locator("details:not([open]) summary").inner_text() == "More"
    assert page.locator("[data-component-panel]:visible").count() == 3
    assert page.get_by_role("tab").count() == 0
    assert page.locator("dialog").count() == 2
    assert page.locator(".component-drawer").count() == 1
    assert page.locator('[role="status"]').count() == 1
    assert page.locator(".component-toast:not([role])").count() == 1
    assert page.locator('li[aria-current="step"]').inner_text() == "Review Approved"
    assert page.locator(".step-indicator .badge").all_text_contents() == ["Completed", "Approved"]
    assert page.locator("section.card h2").inner_text() == "Card"
    assert page.locator("script").get_attribute("src").startswith("/static/components.js?v=")
    badge_html = env.from_string(
        "{% import '_macros.html' as m %}{% for value in values %}"
        "{{ m.status_badge(value) }}{% endfor %}{{ m.status_badge('approved', '') }}"
    ).render(
        values=[
            "completed",
            "approved",
            "failed",
            "paused",
            "running",
            "in_review",
            "pending",
            None,
        ]
    )
    badge_page = browser.new_page()
    badge_page.set_content(badge_html)
    assert badge_page.locator(".badge").all_text_contents() == [
        "completed",
        "approved",
        "failed",
        "paused",
        "running",
        "in_review",
        "pending",
        "?",
        "",
    ]
    assert [badge_page.locator(".badge").nth(i).get_attribute("class") for i in range(7)] == [
        "badge exact",
        "badge exact",
        "badge danger",
        "badge paused",
        "badge run",
        "badge warn",
        "badge status",
    ]
    badge_page.close()
    page.add_style_tag(path=str(ROOT / "app/static/style.css"))
    page.evaluate("location.hash = 'three'")
    page.add_script_tag(path=str(ROOT / "app/static/components.js"))
    tabs = page.get_by_role("tab")
    assert tabs.count() == 3
    assert tabs.nth(2).get_attribute("aria-selected") == "true"
    assert page.evaluate("location.hash") == "#three"
    assert page.locator("html").get_attribute("data-loc-components-ready") == "true"
    assert page.evaluate("typeof window.locComponentsReady") == "undefined"
    history_length = page.evaluate("history.length")
    tabs.nth(1).click()
    assert page.evaluate("location.hash") == "#two"
    assert page.evaluate("history.length") == history_length
    page.add_script_tag(path=str(ROOT / "app/static/components.js"))
    assert tabs.nth(1).get_attribute("aria-selected") == "true"
    # Field fragments survive initialization; selected tabs survive a page reload.
    linked = browser.new_page()
    linked.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
    linked.goto("http://components.test/#required-two")
    linked.add_script_tag(path=str(ROOT / "app/static/components.js"))
    assert linked.locator("#two").is_visible()
    assert linked.evaluate("location.hash") == "#required-two"
    linked.get_by_role("tab").nth(2).click()
    linked.reload()
    linked.add_script_tag(path=str(ROOT / "app/static/components.js"))
    assert linked.get_by_role("tab").nth(2).get_attribute("aria-selected") == "true"
    assert linked.evaluate("localStorage.length + sessionStorage.length") == 0
    linked.close()
    tabs.nth(0).focus()
    for key, index in [
        ("ArrowRight", 1),
        ("End", 2),
        ("ArrowRight", 0),
        ("ArrowLeft", 2),
        ("Home", 0),
    ]:
        page.keyboard.press(key)
        assert tabs.nth(index).get_attribute("aria-selected") == "true"
        assert tabs.nth(index).evaluate("(e) => e === document.activeElement")
        assert page.locator('[role="tabpanel"]:visible').count() == 1
        assert page.evaluate("location.hash") == ["#one", "#two", "#three"][index]
        assert page.evaluate("history.length") == history_length
    page.locator("#tab-form").evaluate("(e) => e.reportValidity()")
    assert page.locator("#two").is_visible()
    assert page.locator("#required-two").evaluate("(e) => e === document.activeElement")
    page.locator(".component-tab-list").evaluate('(e) => e.dir = "rtl"')
    tabs.nth(0).focus()
    page.keyboard.press("ArrowRight")
    assert tabs.nth(2).get_attribute("aria-selected") == "true"
    page.locator(".component-tab-list").evaluate('(e) => e.dir = "ltr"')
    page.locator("#opener").click()
    assert page.locator("#sample-dialog").evaluate('(e) => e.open && e.matches(":modal")')
    page.locator("#dialog-field").focus()
    page.keyboard.press("Shift+Tab")
    assert page.locator("#sample-dialog [data-component-close]").evaluate(
        "(e) => e === document.activeElement"
    )
    page.keyboard.press("Tab")
    assert page.locator("#dialog-field").evaluate("(e) => e === document.activeElement")
    page.locator("#opener").evaluate("(e) => e.focus()")
    assert page.locator("#sample-dialog").evaluate("(e) => e.contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert not page.locator("#sample-dialog").evaluate("(e) => e.open")
    assert page.locator("#opener").evaluate("(e) => e === document.activeElement")
    page.locator("#opener").click()
    page.locator("#sample-dialog [data-component-close]").click()
    assert page.locator("#opener").evaluate("(e) => e === document.activeElement")
    for width in (1440, 390, 320):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".data-table").evaluate('(e) => e.style.minWidth = "600px"')
    region = page.get_by_role("region", name="Records")
    region.focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function('document.querySelector(".data-table-region").scrollLeft > 0')
    assert page.locator(".row-actions a").is_visible()
    assert page.locator(".row-actions a").bounding_box()["x"] < 320
    assert (
        page.locator("thead th").first.evaluate("(e) => getComputedStyle(e).position") == "sticky"
    )
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate('document.documentElement.style.zoom = "2"')
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    browser.close()

# Use the shared disposable application fixture for the adopted standalone page.
from tests._ux_smoke import fresh_fixture

with fresh_fixture() as (client, pages, _):
    response = client.get(pages["error"])
    assert response.status_code == 404
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page()
        page.set_content(response.text)
        assert page.locator("main .page-header h1").inner_text() == "404"
        assert page.locator("main h1").count() == 1
        assert page.get_by_role("link", name="Back to dashboard").get_attribute("href") == "/"
        assert page.locator("main .error").inner_text()
        browser.close()
print(f"Component DOM and keyboard checks; legacy hero templates: {len(legacy)}")
print("UX COMPONENTS OK")
