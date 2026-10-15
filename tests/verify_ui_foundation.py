"""UI foundation smoke (visual-refresh wave 1).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no
network). This is a PRESENTATION-ONLY change; there is no behaviour to assert, so
this suite is a cheap regression tripwire that:

  1. the systemic style.css changes are present and well-formed:
       - the text-control rule is GLOBAL (unscoped from `.form`) yet still excludes
         checkbox/radio/file/range/color so native controls keep rendering;
       - the new foundation component blocks exist (.checkbox, danger-btn alias,
         details styling, file input, select[multiple], .trunc, a.ghost buttons);
  2. every main page still renders (200, no Jinja/template throw) after the class
     renames, and a couple of the new class markers actually reach the HTML.

Final line: "UI FOUNDATION OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_uifound_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app

_CSS = os.path.join(_ROOT, "app", "static", "style.css")


def test_css_systemic():
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    # 1a: the text-control rule is unscoped to a global input/select/textarea, and
    # still excludes the native-rendering types.
    assert 'input:not([type="checkbox"]):not([type="radio"]):not([type="file"])' in css, (
        "global unscoped input rule (with type exclusions) missing"
    )
    assert ".form input, .form select, textarea {" not in css, (
        "old .form-scoped text-control rule should be gone (widened to global)"
    )
    # 1a: danger alias safety net extends the canonical .danger-btn selector.
    assert "button.danger, .btn-danger" in css, "danger-btn alias safety net missing"
    # 1a: new foundation component blocks (existing tokens only).
    for marker in (
        ".checkbox {",  # real checkbox row
        ":focus-visible",  # keyboard focus ring
        "::file-selector-button",  # file input
        "select[multiple]",  # multi-select fallback
        ".trunc {",  # truncation utility
        "a.btn, a.ghost, a.btn-secondary",  # anchors-as-buttons alias
        ".kv {",  # key/value dl
    ):
        assert marker in css, f"style.css missing foundation block: {marker!r}"
    # button size/ghost aliases
    assert "button.small, .btn-small" in css, "btn-small alias missing"
    assert "button.ghost, button.btn-secondary" in css, "btn-secondary alias missing"
    print("  css: input rule global+excluded; danger/btn aliases; new component blocks present OK")


def test_pages_render(client):
    # Every main page returns 200 (no template throw) on a fresh empty DB.
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
    ):
        r = client.get(path, follow_redirects=True)
        assert r.status_code == 200, (path, r.status_code)
    print(
        "  pages: / projects workflows setup-ai settings tm connectors customers glossaries -> 200 OK"  # noqa: E501
    )


def test_new_class_markers(client):
    # The normalized markup reaches the HTML on always-rendered controls.
    r = client.get("/settings", follow_redirects=True)
    assert 'class="checkbox"' in r.text, "/settings should carry a real .checkbox row"
    r = client.get("/setup-ai", follow_redirects=True)
    assert 'class="checkbox"' in r.text, "/setup-ai should carry a real .checkbox row"
    # style.css is served and carries the systemic rule.
    r = client.get("/static/style.css")
    assert r.status_code == 200 and ".checkbox {" in r.text, "style.css not served / stale"
    print(
        "  markers: checkbox rows on settings+setup-ai; style.css served with foundation blocks OK"
    )


def main():
    init_db()
    client = TestClient(app)
    test_css_systemic()
    test_pages_render(client)
    test_new_class_markers(client)
    print("UI FOUNDATION OK")


if __name__ == "__main__":
    main()
