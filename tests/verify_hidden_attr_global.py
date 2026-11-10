"""The HTML `hidden` attribute wins over every display-setting class (issue #636), in real Chromium.

  [1] style.css has a global `[hidden] { display: none !important }` rule.
  [2] A `.form` (display:flex), a `.btn` and a `.grid2` that carry `hidden` render as display:none;
      clearing the attribute (what the page scripts do) shows them again with their own display.
  [3] No template pairs `hidden` with a class whose rule would have shown it AND whose script
      toggles it with style.display (a global rule must not fight inline-style toggles).

Final line: "verify_hidden_attr_global OK".
"""

import os
import re
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from playwright.sync_api import sync_playwright

CSS = Path("app/static/style.css").read_text(encoding="utf-8")


def check_rule_text() -> None:
    assert re.search(r"(?m)^\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", CSS), (
        "style.css must carry a global [hidden] { display: none !important; } rule"
    )
    print("  [1] global [hidden] rule present OK")


def check_browser(browser) -> None:
    page = browser.new_page()
    html = (
        "<style>" + CSS + "</style><body>"
        '<form id="f" class="form" hidden><input name="n"></form>'
        '<a id="b" class="btn" href="#" hidden>x</a>'
        '<div id="g" class="grid2" hidden><p>x</p></div>'
        "</body>"
    )
    page.set_content(html)
    for el_id in ("f", "b", "g"):
        shown = page.evaluate(f"getComputedStyle(document.getElementById('{el_id}')).display")
        assert shown == "none", f"#{el_id} with hidden renders as display:{shown}"
    page.evaluate("document.getElementById('f').hidden = false")
    flex = page.evaluate("getComputedStyle(document.getElementById('f')).display")
    assert flex == "flex", f"unhidden .form must be flex again, got {flex}"
    page.close()
    print("  [2] hidden form/btn/grid2 are display:none; unhiding restores display OK")


def check_templates() -> None:
    bad = []
    for path in Path("app/templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'<\w+[^>]*\shidden(?=[\s>/])[^>]*\sid="([\w-]+)"[^>]*>', text):
            el_id = m.group(1)
            if re.search(rf"getElementById\(['\"]{re.escape(el_id)}['\"]\)\.style\.display", text):
                bad.append(f"{path}:{el_id}")
    assert not bad, f"hidden elements toggled with style.display (would stay hidden): {bad}"
    print("  [3] no hidden element is toggled by inline style.display OK")


if __name__ == "__main__":
    check_rule_text()
    check_templates()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        try:
            check_browser(browser)
        finally:
            browser.close()
    print("verify_hidden_attr_global OK")
