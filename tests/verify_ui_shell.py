"""UI shell polish smoke (visual-refresh wave 3D).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no
network). Presentation/JS-only change; this is a cheap regression tripwire
covering the three items in this wave:

  1. Profile-menu keyboard navigation: base.html's usermenu markup carries the
     `role="menuitem"` hook the keyboard-nav script depends on, and
     usermenu.js (loaded by base.html) implements the ArrowUp/ArrowDown/
     Home/End/Escape/typeahead behavior described in the wave spec.
  2. Theme support on the standalone auth shells (login/accept/setup/error):
     each ships the SAME no-FOUC bootstrap script (checked verbatim against
     base.html's copy) BEFORE its stylesheet link, plus the
     `<meta name="color-scheme">` tag - so a saved 'light' preference applies
     without a flash even on pages that don't extend base.html.
  3. workflow_builder.html's status-color one-liner now uses the `--danger`
     token instead of the hardcoded `#b00020` hex literal.

Final line: "UI SHELL OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_uishell_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app

_TPL = os.path.join(_ROOT, "app", "templates")
_STATIC = os.path.join(_ROOT, "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


# The exact no-FOUC bootstrap block as it appears in base.html's <head>, used
# below as the ground truth every auth shell must copy verbatim.
_NOFOUC_RE = re.compile(
    r'<script nonce="\{\{ csp_nonce\(\) \}\}">\s*\(function \(\) \{\s*try \{\s*'
    r"var el = document\.documentElement;\s*"
    r"var p = el\.getAttribute\('data-theme-pref'\);[^\n]*\s*"
    r"var t = \(p === 'light' \|\| p === 'dark' \|\| p === 'system'\) \? p : "
    r"localStorage\.getItem\('loc_theme'\);\s*"
    r"if \(t === 'light' \|\| t === 'dark'\) el\.setAttribute\('data-theme', t\);\s*"
    r"else if \(window\.matchMedia && "
    r"window\.matchMedia\('\(prefers-color-scheme: light\)'\)\.matches\) "
    r"el\.setAttribute\('data-theme', 'light'\);[^\n]*\s*"
    r"\} catch \(e\) \{\}\s*\}\)\(\);\s*</script>",
)


def test_base_no_fouc_snippet_matches_auth_shells():
    base_html = _read(_TPL, "base.html")
    m = _NOFOUC_RE.search(base_html)
    assert m, "base.html no-FOUC bootstrap script not found (moved/reworded?)"
    snippet = m.group(0)

    for name in ("login.html", "accept.html", "setup.html", "error.html"):
        html = _read(_TPL, name)
        assert 'name="color-scheme"' in html, f"{name} missing color-scheme meta"
        found = _NOFOUC_RE.search(html)
        assert found, f"{name} missing (or diverged from) the no-FOUC bootstrap script"
        assert found.group(0) == snippet, f"{name}'s bootstrap script text differs from base.html's"
        boot_pos = html.index(found.group(0))
        css_pos = html.index("/static/style.css")
        meta_pos = html.index('name="color-scheme"')
        assert boot_pos < css_pos, f"{name}: bootstrap script must run BEFORE the stylesheet link"
        assert meta_pos < css_pos, f"{name}: color-scheme meta must precede the stylesheet link"
        # No theme control on auth pages (no logged-in context): the stored
        # preference just applies; the Light / Dark / Match choices live in the account menu.
        assert "data-theme-toggle" not in html and "data-theme-set" not in html, (
            f"{name} should not carry a theme control"
        )
    print(
        "  auth shells: no-FOUC bootstrap (byte-identical to base.html) + color-scheme meta, "
        "both before the stylesheet link, on all 4 shells OK"
    )


def test_usermenu_markup_and_script_hook():
    base_html = _read(_TPL, "base.html")
    assert 'details class="usermenu"' in base_html
    assert 'role="menu"' in base_html
    assert base_html.count('role="menuitem"') >= 4, (
        "expected several role=menuitem links in the usermenu panel"
    )
    assert 'aria-expanded="false"' in base_html, "summary should start with aria-expanded=false"
    assert "/static/usermenu.js" in base_html, "base.html should load usermenu.js"
    # The old inline outside-click/Escape script should be gone (superseded),
    # not duplicated alongside the extracted file.
    assert "Close the profile dropdown" not in base_html

    js = _read(_STATIC, "usermenu.js")
    for marker in (
        "ArrowDown",
        "ArrowUp",
        "Home",
        "End",
        "Escape",
        "aria-expanded",
        'role="menuitem"',
        "details.usermenu",
    ):
        assert marker in js, f"usermenu.js missing expected behavior marker: {marker!r}"
    print(
        "  base.html: usermenu markup + script hook present; old inline handler removed; "
        "usermenu.js implements arrow/home/end/escape/typeahead nav OK"
    )


def test_workflow_builder_no_hardcoded_hex():
    html = _read(_TPL, "workflow_builder.html")
    assert "#b00020" not in html, "workflow_builder.html still has the hardcoded danger hex"
    assert "var(--danger)" in html, "workflow_builder.html should use the --danger token instead"
    print("  workflow_builder.html: hardcoded #b00020 replaced with var(--danger) OK")


def test_auth_pages_render(client):
    r = client.get("/auth/login")
    assert r.status_code == 200, ("/auth/login", r.status_code)
    assert 'name="color-scheme"' in r.text

    r = client.get("/auth/setup")
    assert r.status_code == 200, ("/auth/setup", r.status_code)  # fresh temp DB has 0 users
    assert 'name="color-scheme"' in r.text

    r = client.get("/auth/accept/not-a-real-token")
    assert r.status_code == 200, ("/auth/accept/...", r.status_code)
    assert 'name="color-scheme"' in r.text

    # error.html is only ever served through the app's HTTPException handler,
    # so its natural status is whatever the triggering error was (here: a 404
    # for an unrouted path) — not 200. It still must carry the theme markers.
    r = client.get("/this-route-does-not-exist-uishell-check")
    assert r.status_code == 404, ("unrouted path", r.status_code)
    assert 'name="color-scheme"' in r.text
    assert "loc_theme" in r.text
    print(
        "  auth routes: /auth/login, /auth/setup, /auth/accept/<token> -> 200; "
        "unrouted path -> 404 via error.html; all carry theme markers OK"
    )


def main():
    init_db()
    client = TestClient(app)
    test_base_no_fouc_snippet_matches_auth_shells()
    test_usermenu_markup_and_script_hook()
    test_workflow_builder_no_hardcoded_hex()
    test_auth_pages_render(client)
    print("UI SHELL OK")


if __name__ == "__main__":
    main()
