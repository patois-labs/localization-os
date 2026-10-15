"""UI i18n verification (offline). Suite #26.

Proves the i18n foundation without disturbing the English default:
  - loader: en/es/fr/de load; translate() returns values; unknown key -> key;
    unknown locale -> en; {placeholder} formatting; es/fr/de differ from en.
  - per-request t(): no session -> English (the compiled-in default) renders the
    exact nav strings other suites pin; POST /account/locale=es flips a real page
    to Spanish; a signed-in user's choice persists to User.ui_locale.
  - REGRESSION GUARD: with locale UNSET, base.html renders the English nav labels
    verbatim ("Dashboard", "Projects", "Language sets", "Background jobs", ...).

Prints are ASCII-only (cp1252 console).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_i18n_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.i18n import loader as i18n
from app.main import app
from app.models import User
from app.services import security, settings
from tests._csrf import csrf_for


def login(c, who, pw):
    # Auth on: the login POST needs a CSRF token, and a successful login rotates
    # the session -> refresh the client's default X-CSRF-Token header.
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def test_loader():
    # all four catalogs load and are non-empty
    for loc in ("en", "es", "fr", "de"):
        cat = i18n.load_catalog(loc)
        assert cat and "nav.dashboard" in cat, (loc, len(cat))
    assert set(("en", "es", "fr", "de")).issubset(set(i18n.available_locales()))  # noqa: C405
    assert i18n.available_locales()[0] == "en", "en must sort first"
    print("  loader: en/es/fr/de catalogs load; available_locales lists them (en first)")

    # translate returns the value; en default
    assert i18n.translate("en", "nav.dashboard") == "Dashboard"
    assert i18n.translate("es", "nav.dashboard") == "Panel"
    assert i18n.translate("fr", "nav.dashboard") == "Tableau de bord"
    # de differs from en (non-ASCII string not printed, only compared)
    assert i18n.translate("de", "nav.dashboard") != "Dashboard"
    # es/fr/de actually differ from en for a sample nav key (no mock junk)
    en_val = i18n.translate("en", "nav.jobs")
    for loc in ("es", "fr", "de"):
        assert i18n.translate(loc, "nav.jobs") != en_val, (loc, "not translated")
    print("  translate: en/es/fr/de return distinct real strings for nav.jobs")

    # unknown key -> the key itself
    assert i18n.translate("en", "nope.not.here") == "nope.not.here"
    # unknown locale -> en fallback
    assert i18n.translate("zz", "nav.dashboard") == "Dashboard"
    # missing key in es -> en fallback (role labels all exist, so use a fake key
    # that we know is en-only by construction: none here, so test locale fallback)
    assert i18n.translate("es", "totally.absent") == "totally.absent"
    print("  translate: unknown key -> key; unknown locale -> en")

    # {placeholder} formatting
    got = i18n.translate("en", "page.dashboard.on_document", document="report.docx")
    assert got == "on report.docx", got
    got_es = i18n.translate("es", "page.dashboard.on_document", document="report.docx")
    assert "report.docx" in got_es and got_es != got, got_es
    # a bad token does not raise (degrades to raw value)
    raw = i18n.translate("en", "page.dashboard.on_document")  # no kwargs
    assert "{document}" in raw, raw
    print("  translate: {placeholder} formatting works; bad/absent token does not raise")

    # js_catalog: short keys, en-merged
    js_en = i18n.js_catalog("en")
    assert "queued" in js_en and js_en["queued"] == "Queued…"
    js_de = i18n.js_catalog("de")
    assert js_de["queued"] != js_en["queued"], "de js not translated"
    print("  js_catalog: js.* slice, short keys, per-locale + en-merged")


def test_appearance_i18n():
    # Console: Account -> Appearance offers Light / Dark / Match my computer, and the account
    # menu carries the same three choices. Key existence + real (non-key-echo, non-English)
    # translations in all 4 catalogs, and the rendered Spanish /account page uses them.
    for key in (
        "page.account.theme_light",
        "page.account.theme_dark",
        "page.account.theme_system",
        "page.account.get_started_hint",
        "page.account.get_started_show",
        "chrome.theme_light",
        "chrome.theme_dark",
        "chrome.theme_system",
        "chrome.theme_label",
    ):
        en_val = i18n.translate("en", key)
        assert en_val != key, ("missing en appearance i18n key", key)
        for loc in ("es", "fr", "de"):
            loc_val = i18n.translate(loc, key)
            assert loc_val != key, ("missing appearance i18n key", loc, key)
            assert loc_val != en_val, ("appearance i18n key not translated", loc, key)
    print("  appearance: theme + Get started keys exist in en/es/fr/de; es/fr/de differ from en")
    c = TestClient(app)
    c.post("/account/locale", data={"ui_locale": "es"}, follow_redirects=False)
    page = c.get("/account").text
    assert i18n.translate("es", "page.account.theme_system") in page, (
        "Spanish 'Match my computer' not rendered on /account"
    )
    assert "skin-option" not in page, "the skin picker must be gone"
    print("  /account renders the localized Appearance choices")


def test_per_request(c):
    # --- default (no session) renders English: the exact strings other suites pin ---
    home = c.get("/").text
    # ("Sign out" only renders with auth ON; checked in test_user_persistence.)
    for pinned in (
        "Dashboard",
        "Projects",
        "Customers",
        "Workflows",
        "Language sets",
        "Reports",
        "My account",
        "Help &amp; API",
        "Account settings",
    ):
        assert pinned in home, ("REGRESSION: English nav string missing", pinned)
    assert 'lang="en"' in home, "default html lang must be en"
    print("  default locale (no session) renders exact English nav strings (regression guard)")

    jobs = c.get("/jobs").text
    assert "Execution history" in jobs, "REGRESSION: jobs page English title changed"
    print("  /jobs still renders the pinned English 'Execution history' by default")

    # window.I18N injected for JS
    assert "window.I18N" in home and "Queued" in home, "window.I18N not injected"
    print("  window.I18N injected into every page (js.* strings for the current locale)")

    # --- switch to Spanish via the locale route; session cookie persists it ---
    r = c.post("/account/locale", data={"ui_locale": "es"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=locale" in r.headers["location"], r.headers
    home_es = c.get("/").text
    assert "Panel" in home_es and "Conectores" in home_es, "Spanish nav not applied"
    assert 'lang="es"' in home_es, "html lang should be es"
    assert "Dashboard" not in home_es.split("<nav")[1].split("</nav>")[0], (
        "English nav label leaked into the Spanish sidebar"
    )
    print("  POST /account/locale=es flips the live sidebar to Spanish (session-driven)")

    # unknown locale falls back to the install default (en), not stuck on a bad value
    c.post("/account/locale", data={"ui_locale": "zz"}, follow_redirects=False)
    home_back = c.get("/").text
    assert (
        "Dashboard" in home_back and "Panel" not in home_back.split("<nav")[1].split("</nav>")[0]
    ), "unknown locale should reset to the English default"
    print("  unknown locale resets to the English default (no bad value stuck on session)")


def test_user_persistence():
    # turn the login wall on and prove the choice persists to User.ui_locale
    security.seed_initial()
    cc = TestClient(app)
    assert login(cc, "admin", "admin").status_code == 303
    r = cc.post("/account/locale", data={"ui_locale": "fr"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "admin")).first()
        assert u.ui_locale == "fr", ("User.ui_locale not persisted", getattr(u, "ui_locale", None))
    # a fresh client (no ui_locale in its session) inherits the stored preference
    fresh = TestClient(app)
    assert login(fresh, "admin", "admin").status_code == 303
    home = fresh.get("/").text
    assert "Tableau de bord" in home, "stored User.ui_locale not honored on a new session"
    print("  signed-in choice persists to User.ui_locale and is honored on a new session")

    # with auth ON + English chosen, the pinned "Sign out" chrome renders in English
    fresh.post("/account/locale", data={"ui_locale": "en"}, follow_redirects=False)
    home_en = fresh.get("/").text
    assert "Sign out" in home_en and "Account settings" in home_en, (
        "REGRESSION: English user-menu chrome missing with auth on"
    )
    print("  auth-on user menu renders pinned English 'Sign out' by default")
    settings.set_many({"security_auth_required": "0"})


def main() -> None:
    init_db()
    # default_ui_locale setting exists and defaults to en
    assert settings.get("default_ui_locale") == "en", settings.get("default_ui_locale")
    test_loader()
    test_appearance_i18n()
    c = TestClient(app)
    test_per_request(c)
    test_user_persistence()
    print("I18N OK")


if __name__ == "__main__":
    main()
