"""Console look verification (2026-10-08) - hermetic + offline.

Console is the ONLY look (light and dark). This suite pins what replaced the Kiln,
Instrument and Ledger skins and the skin system:

  1. CSS: no `data-skin` selector, only the :root (dark) and html[data-theme="light"] token
     sets, the same token NAMES in both, Public Sans + JetBrains Mono are the only bundled
     faces, 6px control radius, no shadow tokens, and the content column variable.
  2. The skin service, picker, route, API field and CLI verb are gone (404/405, no key,
     argparse usage error) and a stored `ui_skin` value is ignored by every render.
  3. base.html: no skin bootstrap; the top bar carries no theme toggle and no Get started
     link; the account menu carries the Light / Dark / Match my computer radios.
  4. "Get started" lives in the account menu for an eligible account that has not dismissed
     it; once dismissed the menu entry is gone and Account settings turns it back on
     (POST /account/get-started clears the same per-user flag the CLI and API clear).
  5. Account settings: an Appearance radio group (Light, Dark, Match my computer) and no
     skin picker. The choice is stored PER ACCOUNT (User.ui_theme) and rendered into
     <html> before first paint on every surface (web form, account-menu fetch,
     POST /api/account/theme, `account set-theme` local and --remote); a second browser of
     the same account sees it, another account does not, and bad values are refused.
  6. The row "..." menu markup (menu button pattern) on the Projects list, and the
     work-surface opt-out of the centered column.
  7. `<pre>` never uses the data font (code readouts stay on the mono face).

Final line: "UI CONSOLE OK".
"""

import json as _json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_uiconsole_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import _remote
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, User
from app.services import apikeys, security
from tests._cli_runner import run_cli
from tests._csrf import csrf_for

_CSS = os.path.join(_ROOT, "app", "static", "style.css")
_TEMPLATES_DIR = os.path.join(_ROOT, "app", "templates")
_FONTS_DIR = os.path.join(_ROOT, "app", "static", "fonts")


def _block_body(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"CSS block not found: {selector!r}"
    return m.group(1)


def _props(body):
    return set(re.findall(r"(--[A-Za-z0-9-]+)\s*:", body))


def test_css_single_look():
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    assert "data-skin" not in css, "the skin selectors must be gone from style.css"
    dark = _block_body(css, ":root")
    light = _block_body(css, 'html[data-theme="light"]')
    raw = {
        "--color-canvas",
        "--color-sidebar",
        "--color-surface",
        "--color-subtle",
        "--color-raised",
        "--color-border",
        "--color-border-soft",
        "--color-control-border",
        "--color-text",
        "--color-text-secondary",
        "--color-text-muted",
        "--color-accent",
        "--color-accent-text",
        "--color-on-accent",
        "--color-selected",
        "--color-nav-on",
        "--color-focus",
        "--success",
        "--warn",
        "--danger",
    }
    assert raw <= _props(dark), ("dark theme lacks tokens", sorted(raw - _props(dark)))
    assert raw <= _props(light), ("light theme lacks tokens", sorted(raw - _props(light)))
    # sidebar is the same surface as the page in both themes (hairline divider, no panel).
    for label, body in (("dark", dark), ("light", light)):
        side = re.search(r"--color-sidebar:\s*(#[0-9a-fA-F]+)", body).group(1).lower()
        page = re.search(r"--color-canvas:\s*(#[0-9a-fA-F]+)", body).group(1).lower()
        assert side == page, (label, "sidebar must equal the page surface", side, page)
    assert "--radius-control: 6px" in dark, "control radius must be 6px"
    assert "--elevation-overlay: none" in dark and "--elevation-popover: none" in dark, (
        "Console has no shadows"
    )
    assert "--content-max:" in dark, "the centered content column width token is missing"
    assert "color-scheme: dark" in dark and "color-scheme: light" in light
    # fonts: Public Sans (UI) + JetBrains Mono (data) only.
    families = set(re.findall(r'font-family:\s*"([^"]+)";', css.split(":root", 1)[0]))
    assert families == {"Public Sans", "JetBrains Mono"}, families
    for gone in ("space-grotesk", "hanken-grotesk", "ibm-plex", "spectral"):
        assert not [f for f in os.listdir(_FONTS_DIR) if gone in f.lower()], (
            f"{gone} font files must be removed"
        )
    assert "Public Sans" in dark.split("--font-body:", 1)[1].split(";", 1)[0]
    assert "JetBrains Mono" in dark.split("--mono:", 1)[1].split(";", 1)[0]
    print("  CSS: one look, dark + light tokens, sidebar == page, 6px, no shadows, 2 fonts OK")


def test_pre_never_uses_data_font():
    offenders = []
    for fname in sorted(os.listdir(_TEMPLATES_DIR)):
        if not fname.endswith(".html"):
            continue
        with open(os.path.join(_TEMPLATES_DIR, fname), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for m in re.finditer(r'<pre\b[^>]*\bclass="([^"]*)"', line):
                    if "data-mono" in m.group(1).split():
                        offenders.append(f"{fname}:{lineno}")
    assert not offenders, "<pre> bound to the data font: " + ", ".join(offenders)
    print("  template guard: no <pre> carries data-mono OK")


def test_skin_system_removed():
    assert not os.path.exists(os.path.join(_ROOT, "app", "services", "skins.py")), (
        "app/services/skins.py must be deleted"
    )
    for fname in ("base.html", "account.html", "login.html", "error.html"):
        with open(os.path.join(_TEMPLATES_DIR, fname), encoding="utf-8") as fh:
            body = fh.read()
        assert "data-skin" not in body and "loc_skin" not in body and "current_skin" not in body, (
            f"{fname} still carries skin code"
        )
    with open(os.path.join(_TEMPLATES_DIR, "base.html"), encoding="utf-8") as fh:
        base = fh.read()
    assert "data-theme-toggle" not in base and 'class="themetoggle"' not in base, (
        "the theme toggle left the top bar"
    )
    assert 'class="topaction ob-open"' not in base, "Get started left the top bar"
    assert 'data-theme-set="light"' in base and 'data-theme-set="system"' in base, (
        "the account menu must carry the Light / Dark / Match my computer choices"
    )
    print("  skin system gone from code and templates; top bar has no theme toggle OK")


def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _flag(email):
    with Session(engine) as s:
        return s.exec(select(User).where(User.email == email)).first().onboarding_dismissed_at


def test_web_account_and_menu():
    init_db()
    assert security.seed_initial() is True  # admin / linguist / user1, login wall ON
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303

    # a stored legacy skin value is ignored: no attribute, no error.
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "admin")).first()
        u.ui_skin = "ledger"
        s.add(u)
        s.commit()
    home = c.get("/").text
    assert "data-skin" not in home, "a stored skin value must be ignored"
    assert "loc_skin" not in home

    # menu: theme radios + Get started (eligible, not dismissed).
    assert 'role="menuitemradio"' in home and 'data-theme-set="dark"' in home
    assert "data-tour-open" in home and "/?tour=1" in home, (
        "an eligible account that has not dismissed Get started sees it in the account menu"
    )
    menu = home.split('class="um-panel"', 1)[1].split("</details>", 1)[0]
    topbar = home.split('class="topbar2"', 1)[1].split('class="usermenu"', 1)[0]
    assert "data-tour-open" in menu and "data-tour-open" not in topbar, (
        "Get started is in the account menu, not the top bar"
    )

    # account page: Appearance radios, no skin picker, Get started state.
    acct = c.get("/account").text
    for value in ("light", "dark", "system"):
        assert f'name="theme" value="{value}"' in acct, (value, "appearance radio missing")
    assert "skin-option" not in acct and "ui_skin" not in acct and "/account/skin" not in acct
    assert 'id="get-started-state"' in acct and 'id="get-started-form"' not in acct, (
        "while Get started is on, the card says so and offers no re-enable button"
    )

    # dismiss (the dashboard's Don't show this again), then it leaves the menu and the
    # account page offers to turn it back on.
    r = c.post("/onboarding/dismiss", follow_redirects=False)
    assert r.status_code == 303
    assert _flag("admin") is not None
    home = c.get("/").text
    assert "data-tour-open" not in home, "a dismissed Get started leaves the account menu"
    acct = c.get("/account").text
    assert 'id="get-started-form"' in acct and 'action="/account/get-started"' in acct
    r = c.post("/account/get-started", follow_redirects=False)
    assert r.status_code == 303 and "saved=getstarted" in r.headers["location"], r.headers
    assert _flag("admin") is None, "POST /account/get-started must clear the per-user flag"
    assert "data-tour-open" in c.get("/").text, "Get started is back in the menu"
    with Session(engine) as s:
        acts = [a.action for a in s.exec(select(AuditLog).where(AuditLog.actor == "admin")).all()]
    assert "account.reset_onboarding" in acts, acts

    # the old skin routes and API field are gone.
    assert c.post("/account/skin", data={"ui_skin": "ledger"}).status_code in (404, 405)
    assert "ui_skin" not in c.get("/account").text
    rc, _, _ = run_cli(["account", "set-skin", "ledger", "--user", "admin"])
    assert rc == 2, "the account set-skin verb must be gone"
    rc, out, _ = run_cli(["--json", "account", "show", "--user", "admin"])
    assert rc == 0 and "ui_skin" not in out, out
    print("  web: menu radios + Get started, Account card re-enables, skin routes/verb gone OK")


def _html_tag(page):
    m = re.search(r"<html[^>]*>", page)
    assert m, "no <html> tag"
    return m.group(0)


def _theme_of(email):
    with Session(engine) as s:
        return s.exec(select(User).where(User.email == email)).first().ui_theme


def _mock_transport(token_client):
    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        resp = token_client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            resp.status_code,
            content=resp.content,
            headers={"content-type": resp.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def test_theme_per_user():
    """2026-10-08: Light / Dark / Match my computer is stored per ACCOUNT, not per browser."""
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    assert _theme_of("admin") == "", "a new account starts on Match my computer"
    tag = _html_tag(c.get("/").text)
    assert 'data-theme-pref="system"' in tag and "data-theme=" not in tag, tag
    acct = c.get("/account").text
    assert re.search(r'name="theme" value="system" checked', acct), "system is the checked radio"
    assert 'action="/account/theme"' in acct

    # the Account settings form (no JS): stored on the account, rendered before first paint.
    csrf = csrf_for(c)
    r = c.post(
        "/account/theme", data={"theme": "light", "csrf_token": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=theme" in r.headers["location"], r.headers
    assert _theme_of("admin") == "light"
    tag = _html_tag(c.get("/").text)
    assert 'data-theme-pref="light"' in tag and 'data-theme="light"' in tag, tag
    acct = c.get("/account?saved=theme").text
    assert re.search(r'name="theme" value="light" checked', acct)
    assert "Appearance updated." in acct

    # a second browser of the same account gets the choice; another account does not.
    c2 = TestClient(app)
    assert _login(c2, "admin", "admin").status_code == 303
    assert 'data-theme="light"' in _html_tag(c2.get("/projects").text), "per account"
    c3 = TestClient(app)
    assert _login(c3, "linguist", "linguist").status_code == 303
    tag = _html_tag(c3.get("/").text)
    assert 'data-theme-pref="system"' in tag and "data-theme=" not in tag, tag

    # the account menu posts with fetch (Accept: application/json) and gets JSON.
    csrf = csrf_for(c)
    js = {"X-CSRF-Token": csrf, "Accept": "application/json"}
    r = c.post("/account/theme", data={"theme": "dark"}, headers=js)
    assert r.status_code == 200 and r.json() == {"ok": True, "ui_theme": "dark"}, r.text
    assert _theme_of("admin") == "dark"
    assert 'data-theme="dark"' in _html_tag(c2.get("/").text)

    # bad values are refused and never stored (fetch -> 400, form -> back to /account).
    r = c.post("/account/theme", data={"theme": "neon"}, headers=js)
    assert r.status_code == 400 and r.json()["ok"] is False, r.text
    csrf = csrf_for(c)
    r = c.post("/account/theme", data={"theme": "neon", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "saved=" not in r.headers["location"], r.headers
    assert _theme_of("admin") == "dark", "a refused value must not change the stored choice"

    # without a CSRF token the change is refused (it is a state-changing POST).
    fresh = TestClient(app)
    assert _login(fresh, "admin", "admin").status_code == 303
    fresh.cookies.pop("csrf_token", None)
    fresh.headers.pop("X-CSRF-Token", None)
    r = fresh.post("/account/theme", data={"theme": "light"}, follow_redirects=False)
    assert r.status_code in (303, 400, 403), r.status_code
    assert _theme_of("admin") == "dark", "a POST without CSRF must not change the choice"

    # a signed-out page carries no account choice: the browser fallback applies.
    tag = _html_tag(TestClient(app).get("/auth/login").text)
    assert "data-theme-pref" not in tag and "data-theme=" not in tag, tag

    # the linguist (not an admin) sets their own: self-service for every role.
    csrf = csrf_for(c3)
    r = c3.post(
        "/account/theme", data={"theme": "light", "csrf_token": csrf}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    assert _theme_of("linguist") == "light" and _theme_of("admin") == "dark"

    # API: GET /api/account carries ui_theme; POST /api/account/theme sets the token's own.
    with Session(engine) as s:
        uid = s.exec(select(User.id).where(User.email == "admin")).first()
    _row, raw = apikeys.create_key(uid, "console-theme", scopes="api docs")
    hdr = {"X-Loc-Token": raw}
    api = TestClient(app)
    assert api.get("/api/account", headers=hdr).json()["ui_theme"] == "dark"
    r = api.post("/api/account/theme", headers=hdr, json={"theme": "system"})
    assert r.status_code == 200 and r.json() == {"ok": True, "ui_theme": "system"}, r.text
    assert _theme_of("admin") == "", "system is stored as the empty default"
    r = api.post("/api/account/theme", headers=hdr, json={"theme": "neon"})
    assert r.status_code == 400 and "unknown theme" in r.json()["error"], r.text
    assert api.post("/api/account/theme", json={"theme": "dark"}).status_code == 401
    assert _theme_of("admin") == ""

    # CLI in-process: set-theme + show; a bad value is an argparse usage error.
    rc, out, err = run_cli(["--json", "account", "set-theme", "dark", "--user", "admin"])
    assert rc == 0 and _json.loads(out)["ui_theme"] == "dark", (rc, out, err)
    rc, out, _ = run_cli(["--json", "account", "show", "--user", "admin"])
    assert rc == 0 and _json.loads(out)["ui_theme"] == "dark", out
    rc, _, _ = run_cli(["account", "set-theme", "neon", "--user", "admin"])
    assert rc == 2 and _theme_of("admin") == "dark"
    rc, _, _ = run_cli(["account", "set-theme", "light"])
    assert rc == 2, "in-process set-theme needs --user"

    # CLI --remote: the token is the account; --user is refused.
    _remote._set_transport(_mock_transport(api))
    try:
        base = ["--json", "--remote", "http://127.0.0.1", "--token", raw, "account"]
        rc, out, err = run_cli([*base, "set-theme", "light"])
        assert rc == 0 and _json.loads(out)["ui_theme"] == "light", (rc, out, err)
        assert _theme_of("admin") == "light"
        rc, out, err = run_cli([*base, "show"])
        assert rc == 0 and _json.loads(out)["ui_theme"] == "light", (rc, out, err)
        rc, _, _ = run_cli([*base, "set-theme", "dark", "--user", "linguist"])
        assert rc == 2 and _theme_of("linguist") == "light"
    finally:
        _remote._set_transport(None)
    assert 'data-theme="light"' in _html_tag(c.get("/").text), "a CLI change shows on next load"

    with Session(engine) as s:
        acts = [a.action for a in s.exec(select(AuditLog)).all()]
    assert acts.count("account.update_theme") >= 6, acts
    print("  appearance per account: web form, menu fetch, API, CLI local + remote; first paint OK")


def test_row_menu_and_content_width():
    c = TestClient(app)
    assert _login(c, "admin", "admin").status_code == 303
    csrf = csrf_for(c)
    c.post("/customers", data={"name": "Console Co", "csrf_token": csrf}, follow_redirects=False)
    csrf = csrf_for(c)
    r = c.post(
        "/projects",
        data={
            "name": "Console project",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "default_provider": "mock",
            "customer_id": "1",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    page = c.get("/projects").text
    assert "data-row-menu-btn" in page and 'aria-haspopup="menu"' in page, "row menu button missing"
    assert 'aria-expanded="false"' in page and 'role="menu"' in page
    assert re.search(r'aria-controls="(pm-\d+)"', page) and 'role="menuitem"' in page
    assert 'aria-label="More actions for Console project"' in page
    assert "/static/rowmenu.js" in page
    assert '<main class="content"' in page and "content-wide" not in page.split("<main", 1)[1][:80]
    # the editor is a work surface: full width.
    assert c.get("/projects/1").status_code == 200
    with open(os.path.join(_TEMPLATES_DIR, "editor.html"), encoding="utf-8") as fh:
        assert "wide_page" in fh.read(), "editor.html must opt out of the centered column"
    print("  row menu (menu button pattern) on Projects; editor opts out of the column OK")


def test_overlay_cards_opaque():
    """A dialog's card panel must not inherit the transparent hairline card (10-08 bug: the
    New project dialog showed the Projects list through it)."""
    with open(_CSS, encoding="utf-8") as fh:
        css = fh.read()
    card = re.search(r"(?m)^\.card\s*\{[^}]*background:\s*transparent", css)
    assert card, "the Console hairline .card rule moved; re-check overlay backgrounds"
    rule = re.search(r"(?m)^([^{}]*\.modal-card[^{}]*)\{([^}]*)\}", css[card.end() :])
    assert rule and "dialog .card" in rule.group(1), (
        "no dialog/.modal-card rule after the transparent .card"
    )
    bg = re.search(r"background:\s*([^;]+);", rule.group(2))
    assert bg and bg.group(1).strip() not in ("transparent", "none"), "dialog cards must be opaque"
    for tpl in ("_new_project_dialog.html", "_session_timeout.html"):
        with open(os.path.join(_TEMPLATES_DIR, tpl), encoding="utf-8") as fh:
            assert "modal-card" in fh.read(), f"{tpl} must keep its .modal-card panel"
    print("  dialog card panels stay opaque over the page OK")


def main():
    test_css_single_look()
    test_overlay_cards_opaque()
    test_pre_never_uses_data_font()
    test_skin_system_removed()
    test_web_account_and_menu()
    test_theme_per_user()
    test_row_menu_and_content_width()
    print("UI CONSOLE OK")


if __name__ == "__main__":
    main()
