"""Real cascade, responsive density, focus and reduced-motion contract (Console, both themes)."""

import os
from pathlib import Path

import pytest

from tests.test_design_tokens import CSS, contrast, rgb


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as pw:
        # Offline: use the existing browser when the bundled binary is unavailable.
        channel = os.environ.get("UX_BROWSER_CHANNEL")
        if not channel and Path("C:/Program Files/Google/Chrome/Application/chrome.exe").exists():
            channel = "chrome"
        try:
            instance = pw.chromium.launch(channel=channel)
        except Error as error:
            # CI never runs `playwright install`: skip when no browser binary exists, but
            # let any other launch failure surface.
            message = str(error).lower()
            if "executable" not in message and "distribution" not in message:
                raise
            pytest.skip("no Chromium or Chrome binary installed")
        yield instance
        instance.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 390])
def test_real_css_cascade(browser, theme, width):
    context = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    # No application, fonts or other requests may reach the network.
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_content(
        f'<html data-theme="{theme}"><body>'
        '<h1>Title</h1><button>Action</button><input aria-label="Sample"></body></html>'
    )
    page.add_style_tag(content=CSS.read_text(encoding="utf-8"))
    page.keyboard.press("Tab")
    actual = page.evaluate("""() => {
      const s = getComputedStyle(document.querySelector('button'));
      const t = getComputedStyle(document.documentElement);
      return {radius: s.borderRadius, height: s.minHeight,
              focus: s.outlineWidth, offset: s.outlineOffset,
              motion: t.getPropertyValue('--motion-fast').trim(),
              body: getComputedStyle(document.body).fontSize,
              title: getComputedStyle(document.querySelector('h1')).fontSize};
    }""")
    assert actual == {
        "radius": "6px",
        "height": "40px" if width == 390 else "34px",
        "focus": "2px",
        "offset": "2px",
        "motion": "0s",
        "body": "14px",
        "title": "24px",
    }
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_disabled_buttons_and_focus_ring(browser, theme, tmp_path):
    context = browser.new_context(viewport={"width": 800, "height": 400})
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_content(
        f'<html data-theme="{theme}"><body>'
        '<button class="primary" disabled>Primary unavailable</button>'
        '<button class="danger-btn" disabled>Delete unavailable</button>'
        '<input aria-label="Focus sample"><div class="prog flash">Progress</div>'
        "</body></html>"
    )
    page.add_style_tag(content=CSS.read_text(encoding="utf-8"))
    for selector in ("button.primary", "button.danger-btn"):
        button = page.locator(selector)
        rest = button.evaluate(
            "e => { const s = getComputedStyle(e); return {color:s.color, bg:s.backgroundColor, "
            "opacity:s.opacity, filter:s.filter, transform:s.transform}; }"
        )
        button.hover(force=True)
        hover = button.evaluate(
            "e => { const s = getComputedStyle(e); return {color:s.color, bg:s.backgroundColor, "
            "opacity:s.opacity, filter:s.filter, transform:s.transform}; }"
        )
        assert rest == hover
        assert rest["opacity"] == "1" and rest["filter"] == "none" and rest["transform"] == "none"
        assert contrast(rgb(rest["color"]), rgb(rest["bg"])) >= 4.5
    page.keyboard.press("Tab")
    page.locator("input").evaluate("e => e.getAnimations().forEach(a => a.finish())")
    focus = page.locator("input").evaluate(
        "e => { const s = getComputedStyle(e); return {outline:s.outlineColor, "
        "width:s.outlineWidth, shadow:s.boxShadow, "
        "bg:getComputedStyle(document.body).backgroundColor}; }"
    )
    assert focus["width"] == "2px"
    assert contrast(rgb(focus["outline"]), rgb(focus["bg"])) >= 3
    assert focus["shadow"] == "none", "Console focus is an outline with no glow or shadow"
    frames = page.locator(".prog").evaluate(
        "e => { e.classList.remove('flash'); void e.offsetWidth; e.classList.add('flash'); "
        "const a = e.getAnimations()[0]; a.pause(); a.currentTime = 300; "
        "return a.effect.getKeyframes().map(f => f.boxShadow); }"
    )
    assert frames[0] == frames[-1] == "none"
    assert "0.18" in frames[1]
    page.screenshot(path=str(tmp_path / f"disabled-{theme}.png"))
    context.close()
