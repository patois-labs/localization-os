"""Issue #564 (UX pass 1, second round): stale detail links, TM project id hint, upload error
lands on the form. Offline; anchors on ids, status codes and geometry, never on visible text."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route

with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    # M6: every web detail route answers a missing id with the themed 404, not a redirect.
    for path in (
        "/projects/999999",
        "/documents/999999",
        "/customers/999999",
        "/runs/999999",
        "/runs/999999/graph-fragment",
        "/runs/999999/step-diff",
        "/projects/999999/export",
        "/projects/999999/delete",
        "/projects/999999/remove-locale/de-DE",
    ):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 404, (path, r.status_code)
        assert 'class="error"' in r.text, path
    bodies = {
        re.sub(r'nonce="[^"]*"|name="csrf[^>]*>', "", client.get(p, follow_redirects=False).text)
        for p in ("/projects/999999", "/customers/999999", "/runs/999999")
    }
    assert len(bodies) == 1, "missing-id bodies differ"
    # (The /api twins already answer a JSON 404: tests/verify_id_overflow.py.)

    # m6: the TM page's bare number box says what the number is.
    tm = client.get(pages["tm"]).text
    box = re.search(r'<input[^>]*name="project_id"[^>]*placeholder="Any project"[^>]*>', tm)
    assert (
        box
        and 'title="' in box.group(0)
        and 'aria-describedby="tm-project-id-hint"' in box.group(0)
    )
    assert 'id="tm-project-id-hint"' in tm

    # m9: an upload error opens the page at the form, not a screen above it.
    browser = pw.chromium.launch(channel="chrome")
    context = browser.new_context(reduced_motion="reduce", viewport={"width": 1280, "height": 700})
    install_offline_route(context, client)
    page = context.new_page()
    page.goto("http://ux.local" + pages["project"] + "?err=parse", wait_until="networkidle")
    assert page.locator("#doc-upload-file-error").count() == 1
    assert page.locator("#page-error").count() == 1
    page.wait_for_function("window.scrollY > 0")
    inside = page.evaluate("""() => {
      const r = document.getElementById('doc-upload-file').getBoundingClientRect();
      return r.top >= 0 && r.bottom <= innerHeight;
    }""")
    assert inside, "the upload field is not in view after an upload error"
    # Focus lands on the field, on screen (WCAG 2.2 SC 2.4.11), not on a summary scrolled
    # off the top; the summary sentence and the inline message both describe the field.
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "doc-upload-file"
    )
    described = page.get_attribute("#doc-upload-file", "aria-describedby").split()
    assert "page-error-msg" in described and "doc-upload-file-error" in described, described
    assert page.get_attribute("#doc-upload-file", "aria-invalid") == "true"
    # (A summary whose field is on screen keeps the focus: verify_a11y_browser, sign-in.)
    # No error: the page opens at the top as before.
    page.goto("http://ux.local" + pages["project"], wait_until="networkidle")
    assert page.evaluate("window.scrollY") == 0
    browser.close()

print("verify_ux564a OK")
