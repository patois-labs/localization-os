"""UI light-theme smoke (visual-refresh wave 3A).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no
network). Presentation-only change; this is a cheap regression tripwire that:

  1. style.css defines a `html[data-theme="light"]` block that redefines EXACTLY
     the same custom-property NAMES as `:root` (set equality). This is the killer
     assertion: it stops a future token added to :root from being silently
     forgotten in light mode (which would leave that component unthemed on light).
  2. both blocks declare `color-scheme` (dark / light) and the toggle button's
     ghost styling ships.
  3. base.html emits the no-FOUC bootstrap script BEFORE the stylesheet link
     (so the saved theme paints without a flash), plus the toggle button and the
     `color-scheme` meta.
  4. every main page still renders (200) with the toggle markup reaching the HTML.

Final line: "UI THEME OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_uitheme_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app

_CSS = os.path.join(_ROOT, "app", "static", "style.css")


def _block_body(css, selector):
    """Return the declaration text inside the first `selector { ... }` block.

    :root and the light block contain no nested braces, so a non-greedy match up
    to the first closing brace is exact.
    """
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"CSS block not found: {selector!r}"
    return m.group(1)


def _custom_props(block_body):
    return set(re.findall(r"(--[A-Za-z0-9-]+)\s*:", block_body))


def test_light_block_token_parity():
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    root = _custom_props(_block_body(css, ":root"))
    light = _custom_props(_block_body(css, 'html[data-theme="light"]'))
    assert root, "no custom properties parsed from :root"
    # KILLER ASSERTION: light theme must redefine exactly the :root token names.
    missing = root - light
    extra = light - root
    assert not missing, f"light theme forgot to redefine tokens: {sorted(missing)}"
    assert not extra, f"light theme defines tokens absent from :root: {sorted(extra)}"
    print(f"  token parity: :root and light block both define {len(root)} identical token names OK")


def test_color_scheme_and_toggle_css():
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    assert "color-scheme: dark" in css, ":root should declare color-scheme: dark"
    assert "color-scheme: light" in css, "light block should declare color-scheme: light"
    for marker in (
        'html[data-theme="light"] {',  # the light token block
        ".um-check {",  # the checkmark on the chosen Light / Dark / Match my computer item
        'html[data-theme="light"] .topbar2',  # opaque-literal override
    ):
        assert marker in css, f"style.css missing light-theme marker: {marker!r}"
    print("  css: color-scheme dark/light; account-menu choice + topbar overrides present OK")


def test_base_no_fouc_and_toggle(client):
    html = client.get("/", follow_redirects=True).text
    # no-FOUC bootstrap present and BEFORE the stylesheet link (no flash).
    assert "loc_theme" in html, "no-FOUC theme bootstrap missing from <head>"
    assert 'name="color-scheme"' in html, "color-scheme meta missing"
    assert 'data-theme-set="light"' in html and 'data-theme-set="system"' in html, (
        "Light / Dark / Match my computer choices missing from the account menu"
    )
    assert "/static/theme.js" in html, "theme.js must be loaded for the menu choices"
    boot = html.index("loc_theme")
    css_link = html.index("/static/style.css")
    assert boot < css_link, "theme bootstrap must run BEFORE the stylesheet link"
    print("  base.html: color-scheme meta; no-FOUC script before stylesheet; menu choices OK")


def test_pages_render(client):
    for path in (
        "/",
        "/projects",
        "/workflows",
        "/setup-ai",
        "/settings",
        "/tm",
        "/connectors",
        "/customers",
        "/glossaries",
        "/help",
    ):
        r = client.get(path, follow_redirects=True)
        assert r.status_code == 200, (path, r.status_code)
        assert 'data-theme-set="dark"' in r.text, f"{path} lost the theme choices"
    # style.css served with the light block.
    r = client.get("/static/style.css")
    assert r.status_code == 200 and 'html[data-theme="light"]' in r.text, (
        "style.css not served / missing light block"
    )
    print("  pages: 10 main pages -> 200 with theme choices; style.css served with light block OK")


def main():
    init_db()
    client = TestClient(app)
    test_light_block_token_parity()
    test_color_scheme_and_toggle_css()
    test_base_no_fouc_and_toggle(client)
    test_pages_render(client)
    print("UI THEME OK")


if __name__ == "__main__":
    main()
