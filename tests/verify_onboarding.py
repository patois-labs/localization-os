"""First-run onboarding checklist verification (offline, hermetic).

Covers SPEC-onboarding-tour Phases 0+1:
  - Migration/backfill (Phase 0): the SAME ALTER+backfill the shim/alembic run stamp
    every PRE-existing user to dismissed; a user created AFTER the column lands NULL =
    eligible. Plus a textual guard that BOTH write sites (db._ADDED_COLUMNS shim +
    the alembic revision chained on f8c1a4d7e2b9) carry the column + backfill UPDATE.
  - Service predicates + i18n key parity x4.
  - D7: with auth OFF the SYSTEM user is ineligible (no card; topbar control -> /help).
  - Role-aware auto-show (D3): admin sees the full 4-step card; a linguist sees the
    trimmed (workflow, provider) variant; a translator sees NO card and a topbar
    control pointing at /help.
  - Dismiss persists across requests; re-summon (?tour=1) overrides the flag for
    display without clearing it.
  - Done-state flips when a REAL record is created (driving the real routes), and the
    D6 all-done auto-dismiss hides the card + stamps the flag.

Final line: "ONBOARDING OK".
"""

import hashlib
import json as _json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_onboarding_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.cli import _remote
from app.db import engine, init_db
from app.i18n import loader as i18n
from app.main import app
from app.models import AuditLog, Customer, Role, User, Workflow
from app.services import apikeys, onboarding, security, settings
from tests._cli_runner import run_cli
from tests._csrf import csrf_for

# The exact backfill UPDATE both write sites run (db.py shim + the alembic revision).
_BACKFILL = (
    'UPDATE "user" SET onboarding_dismissed_at = '
    "COALESCE(last_login, created_at, CURRENT_TIMESTAMP) "
    "WHERE onboarding_dismissed_at IS NULL"
)


def login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _rearm(email):
    """Clear the dismissed flag on a user (test setup only — re-arms eligibility)."""
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email)).first()
        u.onboarding_dismissed_at = None
        s.add(u)
        s.commit()


def _flag(email):
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email)).first()
        return u.onboarding_dismissed_at


# --- Phase 0: migration / backfill -----------------------------------------
def test_migration_backfill():
    tmp = tempfile.mkdtemp(prefix="loctms_ob_mig_")
    dbf = os.path.join(tmp, "old.db")
    c = sqlite3.connect(dbf)
    # An "old" install: a user table WITHOUT onboarding_dismissed_at + two rows.
    c.executescript(
        'CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT, role TEXT, '
        "created_at TEXT, last_login TEXT);"
        'INSERT INTO "user" (email, role, created_at, last_login) VALUES '
        "('old1', 'admin', '2020-01-01 00:00:00', '2021-05-05 00:00:00');"
        'INSERT INTO "user" (email, role, created_at, last_login) VALUES '
        "('old2', 'linguist', '2020-02-02 00:00:00', NULL);"
    )
    c.commit()
    # Apply the SAME two statements the shim/migration run.
    c.execute('ALTER TABLE "user" ADD COLUMN onboarding_dismissed_at DATETIME')
    c.execute(_BACKFILL)
    c.commit()
    rows = dict(c.execute('SELECT email, onboarding_dismissed_at FROM "user"').fetchall())
    # Every PRE-existing user is now dismissed (no live account sees the tour).
    assert rows["old1"] is not None and rows["old2"] is not None, rows
    assert rows["old1"].startswith("2021-05-05"), ("old1 -> last_login", rows["old1"])
    assert rows["old2"].startswith("2020-02-02"), ("old2 -> created_at", rows["old2"])
    # A user created AFTER the column exists lands NULL = eligible.
    c.execute(
        'INSERT INTO "user" (email, role, created_at) VALUES '
        "('new1', 'admin', '2026-01-01 00:00:00')"
    )
    c.commit()
    newval = c.execute(
        "SELECT onboarding_dismissed_at FROM \"user\" WHERE email = 'new1'"
    ).fetchone()[0]
    assert newval is None, ("post-migration user must be eligible", newval)
    c.close()
    print(
        "  migration: backfill stamps every pre-existing user (last_login/created_at); "
        "post-migration user lands NULL = eligible"
    )


def test_both_write_sites():
    db_src = open(os.path.join(_ROOT, "app", "db.py"), encoding="utf-8").read()  # noqa: SIM115
    assert '("onboarding_dismissed_at", "DATETIME")' in db_src, (
        "db._ADDED_COLUMNS['user'] missing the onboarding column"
    )
    assert (
        'UPDATE "user" SET onboarding_dismissed_at' in db_src
        and "COALESCE(last_login, created_at, CURRENT_TIMESTAMP)" in db_src
    ), "db.py just-added-branch missing the backfill UPDATE"
    import glob

    migs = glob.glob(os.path.join(_ROOT, "alembic", "versions", "*onboarding*.py"))
    assert migs, "onboarding alembic revision missing"
    msrc = open(migs[0], encoding="utf-8").read()  # noqa: SIM115
    assert "down_revision: Union[str, None] = 'f8c1a4d7e2b9'" in msrc, (
        "revision not chained on the verified head f8c1a4d7e2b9"
    )
    assert (
        "onboarding_dismissed_at" in msrc
        and "COALESCE(last_login, created_at, CURRENT_TIMESTAMP)" in msrc
    ), "alembic revision missing the column add or backfill UPDATE"
    print(
        "  both write sites: db.py shim column+backfill AND alembic revision "
        "(chained on f8c1a4d7e2b9) carry the same backfill"
    )


# --- Phase 2: vendored driver.js + spotlight garnish -----------------------
_VENDOR = os.path.join(_ROOT, "app", "static", "vendor", "driver")


def test_vendored_driver():
    """The driver.js spotlight lib is vendored offline (no CDN): the JS + CSS +
    LICENSE + README exist, the MIT license text + author are present, the version
    is recorded in a provenance header, and the shipped source makes ZERO network
    calls (the only http(s) literals are the W3C SVG namespace URIs it uses to
    build the overlay <svg>, plus the provenance banner's github source link)."""
    js = os.path.join(_VENDOR, "driver.js.iife.js")
    css = os.path.join(_VENDOR, "driver.css")
    lic = os.path.join(_VENDOR, "LICENSE")
    readme = os.path.join(_VENDOR, "README.md")
    for f in (js, css, lic, readme):
        assert os.path.exists(f), ("vendored driver asset missing", f)
    for path, size, digest in (
        (js, 25973, "3d09be0124f5500fd8e442ec1d7398aced537ed2e6fa3a30b0858ad4cea17c70"),
        (css, 3324, "46d110dd1c9c8c25170f2867ce8e78c868833ca35f23a6008c158a4a5b570368"),
    ):
        with open(path, "rb") as stream:
            data = stream.read()
        assert len(data) == size and hashlib.sha256(data).hexdigest() == digest
    lic_txt = open(lic, encoding="utf-8").read()  # noqa: SIM115
    assert "MIT License" in lic_txt and "Kamran Ahmed" in lic_txt, (
        "vendored LICENSE must carry the MIT text + author (license compliance)"
    )
    js_txt = open(js, encoding="utf-8").read()  # noqa: SIM115
    css_txt = open(css, encoding="utf-8").read()  # noqa: SIM115
    # Version header (provenance) present in the banner of BOTH assets + README.
    for txt, where in ((js_txt, "js"), (css_txt, "css")):
        assert "driver.js v1.8.0" in txt, "version header missing in vendored " + where
        assert "MIT" in txt, "license note missing in vendored banner " + where
    assert "1.8.0" in open(readme, encoding="utf-8").read(), "README must record the version"  # noqa: SIM115
    # No-CDN guard: no runtime network primitives, and every http(s) literal is
    # either a w3.org SVG namespace or the provenance github link (both inert).
    for tok in ("fetch(", "XMLHttpRequest", "WebSocket", "importScripts"):
        assert tok not in js_txt, "vendored driver.js must make no network call: " + tok
    for url in re.findall(r'https?://[^\s"\'`)]+', js_txt):
        assert ("w3.org" in url) or ("github.com/kamranahmedse" in url), (
            "unexpected external URL in vendored driver.js (no-CDN): " + url
        )
    print(
        "  vendored: driver.js@1.8.0 JS+CSS+LICENSE(MIT/Kamran Ahmed)+README present; "
        "version header recorded; zero network calls (svg-ns + provenance link only)"
    )


def test_onboarding_js_no_cdn():
    """Our own spotlight glue is pure-ASCII (repo convention) and self-hosted: no
    external URLs, no CDN, reads driver.js from window (the vendored global)."""
    p = os.path.join(_ROOT, "app", "static", "onboarding.js")
    assert os.path.exists(p), "app/static/onboarding.js missing"
    raw = open(p, "rb").read()  # noqa: SIM115
    assert all(b < 128 for b in raw), "onboarding.js must be pure ASCII (repo convention)"
    txt = raw.decode("ascii")
    assert not re.findall(r"https?://", txt), "onboarding.js must contain no external URLs"
    assert "window.driver" in txt, "onboarding.js should read the vendored driver.js global"
    print("  onboarding.js: pure-ASCII, self-hosted, no external URLs (no-CDN glue)")


def test_close_label_unconditional():
    """Reviewer nit 1: driver 1.8.0 already sets aria-label="Close" on the x
    button, so a guard on "no aria-label yet" would NEVER fire and the localized
    labels.close (Schliessen/Cerrar/Fermer) would never apply. The label must be
    set UNCONDITIONALLY. This asserts the source: setAttribute is present and the
    dead guard is gone (this fails against the pre-fix code, then guards the fix)."""
    p = os.path.join(_ROOT, "app", "static", "onboarding.js")
    txt = open(p, encoding="utf-8").read()  # noqa: SIM115
    assert "x.setAttribute('aria-label', labels.close)" in txt, (
        "onboarding.js must set the localized close aria-label"
    )
    assert "!x.getAttribute('aria-label')" not in txt, (
        "the dead 'no aria-label yet' guard must be removed (driver 1.8.0 pre-sets one)"
    )
    print("  close-label: localized aria-label set UNCONDITIONALLY (dead guard removed)")


def _target_pages_no_hardcoded_english():
    """The spotlight popover strings on the target pages must come from i18n, not
    hardcoded English: each anchored template feeds page.onboarding.* via t()."""

    hits = 0
    for tpl in ("customers.html", "projects.html", "workflows.html", "setup_ai.html"):
        src = open(os.path.join(_ROOT, "app", "templates", tpl), encoding="utf-8").read()  # noqa: SIM115
        assert "data-spotlight-anchor=" in src, ("anchor missing in template", tpl)
        assert "page.onboarding.spot_" in src and "'page.onboarding.step_" in src, (
            "target popover strings must come from page.onboarding.* via t()",
            tpl,
        )
        hits += 1
    assert hits == 4
    print(
        "  templates: all 4 target anchors feed popover text from page.onboarding.* (no hardcoded English)"  # noqa: E501
    )


def test_i18n_parity():
    keys = [
        "title",
        "subtitle",
        "progress",
        "done",
        "todo",
        "dismiss",
        "all_done_msg",
        "step_customer_title",
        "step_customer_desc",
        "step_customer_cta",
        "step_project_title",
        "step_project_desc",
        "step_project_cta",
        "step_workflow_title",
        "step_workflow_desc",
        "step_workflow_cta",
        "step_provider_title",
        "step_provider_desc",
        "step_provider_cta",
        # Phase 2 spotlight strings (SPEC-onboarding-tour Phase 2): the "Show
        # me" label, the driver.js button labels, and the per-step target hints.
        "showme",
        "spot_cta_hint",
        "spot_go",
        "spot_done",
        "spot_close",
        "spot_customer_target",
        "spot_project_target",
        "spot_workflow_target",
        "spot_provider_target",
    ]
    for loc in ("en", "es", "fr", "de"):
        for k in keys:
            full = "page.onboarding." + k
            assert i18n.translate(loc, full) != full, ("missing key", loc, full)
        assert i18n.translate(loc, "chrome.get_started") != "chrome.get_started", (
            "missing chrome.get_started",
            loc,
        )
    # es/fr/de titles differ from en (real translations, not en-echoes)
    en_title = i18n.translate("en", "page.onboarding.title")
    for loc in ("es", "fr", "de"):
        assert i18n.translate(loc, "page.onboarding.title") != en_title, (loc, "untranslated")
    # {done}/{total} placeholder formats
    got = i18n.translate("en", "page.onboarding.progress", done=1, total=4)
    assert "1" in got and "4" in got, got
    # es/fr/de spotlight strings differ from en too (real translations).
    for k in ("showme", "spot_go", "spot_done", "spot_customer_target"):
        en_v = i18n.translate("en", "page.onboarding." + k)
        for loc in ("es", "fr", "de"):
            assert i18n.translate(loc, "page.onboarding." + k) != en_v, (loc, k, "untranslated")
    print(
        "  i18n: page.onboarding.* (28 keys incl. 9 spotlight) + chrome.get_started "
        "present in en/es/fr/de; es/fr/de translated; {done}/{total} formats"
    )


# --- D7: auth OFF -> ineligible --------------------------------------------
def test_authoff_ineligible():
    # Fresh install, auth OFF (the verify default): session_user is the SYSTEM admin.
    c = TestClient(app)
    html = c.get("/").text
    assert "data-onboarding-panel" not in html, "auth-off SYSTEM user must not see the card (D7)"
    # Console: Get started lives in the account menu for an eligible account only.
    assert "data-tour-open" not in html, "auth-off user is not eligible -> no Get started entry"
    assert "/?tour=1" not in html
    assert not onboarding.is_eligible(security.SYSTEM), "SYSTEM user must be ineligible"
    print("  D7: auth-off SYSTEM user ineligible (no card; no Get started entry)")


# --- Role-aware auto-show (D3) ---------------------------------------------
def test_admin_autoshow():
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    html = c.get("/").text
    assert "data-onboarding-panel" in html, "admin should see the checklist card"
    assert '<details class="ob-details" open>' in html, "card should auto-expand for a fresh admin"
    for step in ("customer", "project", "workflow", "provider"):
        assert f'data-tour-step="{step}"' in html, ("admin missing step", step)
    # eligible and not dismissed -> the account menu entry re-summons the checklist
    assert "/?tour=1" in html and "data-tour-open" in html, (
        "eligible admin's account-menu Get started entry should point at /?tour=1"
    )
    assert 'class="topaction ob-open"' not in html, "Get started is not in the top bar"
    print("  D3 admin: full 4-step card auto-expands; account menu entry -> /?tour=1")


def test_linguist_variant():
    c = TestClient(app)
    assert login(c, "linguist", "linguist").status_code == 303
    html = c.get("/").text
    assert "data-onboarding-panel" in html, "linguist should see a (trimmed) card"
    assert 'data-tour-step="workflow"' in html and 'data-tour-step="provider"' in html, (
        "linguist variant should include workflow + provider"
    )
    assert 'data-tour-step="customer"' not in html and 'data-tour-step="project"' not in html, (
        "linguist variant must DROP the customer/project (manage/write-gated) steps"
    )
    print("  D3 linguist: trimmed variant = workflow + provider (customer/project dropped)")


def test_translator_none():
    c = TestClient(app)
    assert login(c, "user1", "user1").status_code == 303
    html = c.get("/").text
    assert "data-onboarding-panel" not in html, "translator must get NO auto-tour"
    assert "data-tour-open" not in html and "/?tour=1" not in html, (
        "an ineligible translator has no Get started entry"
    )
    print("  D3 translator: no auto-tour; no Get started entry")


def test_showme_markup():
    """Phase 2: the dashboard checklist renders the "Show me" spotlight affordance
    (hidden, JS-revealed) + the driver.js assets for an ELIGIBLE user only; an
    ineligible (translator) page carries neither the assets nor the labels blob."""
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    html = c.get("/?tour=1").text  # force the panel (admin may be dismissed)
    # Each undone step gets a hidden Show-me button anchored on its data-attrs.
    assert 'class="ob-showme"' in html and "data-spotlight-step=" in html, (
        "eligible dashboard must render the Show-me affordance"
    )
    assert "hidden>" in html or "hidden " in html or "hidden=" in html, (
        "Show-me must ship [hidden] (progressive enhancement: JS reveals it)"
    )
    assert 'data-spotlight-url="/customers?spotlight=customer"' in html, (
        "Show-me must carry the ?spotlight cross-page URL"
    )
    # The vendored lib + our glue + the translated labels blob load for eligibles.
    assert "/static/vendor/driver/driver.js.iife.js" in html, "driver.js must load for eligible"
    assert "/static/vendor/driver/driver.css" in html, "driver.css must load for eligible"
    assert "/static/onboarding.js" in html, "onboarding.js must load for eligible"
    assert "data-spotlight-labels" in html, "translated labels blob must render for eligible"
    # Ineligible translator: no card, no driver assets, no labels blob.
    t = TestClient(app)
    assert login(t, "user1", "user1").status_code == 303
    thtml = t.get("/?tour=1").text
    assert (
        "ob-showme" not in thtml
        and "driver.js.iife.js" not in thtml
        and "data-spotlight-labels" not in thtml
    ), "ineligible user must not receive the spotlight assets"
    print(
        "  Phase2 markup: eligible dashboard renders hidden Show-me + driver assets + "
        "labels blob; ineligible user gets none"
    )


def test_target_anchors_served():
    """Phase 2 cross-page mechanism: each of the four REAL target pages serves the
    spotlight anchor on the actual creation control, plus the labels blob + lib, so
    onboarding.js can run the ?spotlight=<step> highlight there (degrades to nothing
    if absent). Driven as admin (can reach all four)."""
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    targets = {
        "customer": "/customers",
        "project": "/projects",
        "workflow": "/workflows",
        "provider": "/setup-ai",
    }
    for key, path in targets.items():
        # The anchor is server-rendered unconditionally; the ?spotlight param is
        # read client-side, so assert both the plain and the spotlight URL carry it.
        for url in (path, path + "?spotlight=" + key):
            html = c.get(url).text
            assert (f'data-spotlight-anchor="{key}"') in html, (
                "target anchor missing",
                key,
                url,
            )
            assert "data-spotlight-labels" in html, ("labels blob missing", url)
            assert "/static/vendor/driver/driver.js.iife.js" in html, ("lib missing", url)
    print(
        "  Phase2 anchors: /customers /projects /workflows /setup-ai each carry "
        "data-spotlight-anchor + labels blob + driver.js (real controls)"
    )


def test_ineligible_target_no_assets():
    """Reviewer test (a): an INELIGIBLE user (translator) landing on a TARGET page
    must not receive the spotlight assets either — the eligibility gate covers the
    whole chrome, not just the dashboard. GET /customers as a translator: no lib,
    no labels blob (the anchor itself is harmless static markup and may remain)."""
    c = TestClient(app)
    assert login(c, "user1", "user1").status_code == 303
    html = c.get("/customers").text
    assert "/static/vendor/driver/driver.js.iife.js" not in html, (
        "ineligible user must not load driver.js on a target page"
    )
    assert "/static/vendor/driver/driver.css" not in html, (
        "ineligible user must not load driver.css on a target page"
    )
    assert "data-spotlight-labels" not in html, (
        "ineligible user must not receive the labels blob on a target page"
    )
    print(
        "  Phase2 gate: ineligible (translator) GET /customers loads no driver.js / "
        "css / labels blob"
    )


def test_spotlight_param_no_reflection():
    """Reviewer test (b): the ?spotlight param is consumed CLIENT-side only; the
    server must never echo it. Fuzz a target page with injection payloads and
    assert none of the attacker markup is reflected into the response (no XSS
    surface via the cross-page param)."""
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    payloads = [
        '"]',
        "<img src=x onerror=alert(1)>",
        'customer"]<script>alert(1)</script>',
        'customer%0A"]<script>',
        "  customer",
        '"><svg/onload=alert(1)>',
    ]
    for raw in payloads:
        r = c.get("/customers", params={"spotlight": raw})
        assert r.status_code == 200, ("target must render cleanly", raw, r.status_code)
        body = r.text
        # No attacker markup reflected anywhere in the response.
        for needle in ("<script>alert", "onerror=alert", "onload=alert", "<svg/onload"):
            assert needle not in body, ("XSS payload reflected via ?spotlight", raw, needle)
        # The legit anchor is still there (page rendered normally, not broken).
        assert 'data-spotlight-anchor="customer"' in body, ("anchor lost under fuzz", raw)
    print(
        "  Phase2 fuzz: ?spotlight injection payloads never reflected server-side; "
        "target renders clean"
    )


def test_dismiss_and_resummon():
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    csrf_for(c)
    assert _flag("admin") is None, "admin starts eligible"
    r = c.post("/onboarding/dismiss", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert _flag("admin") is not None, "dismiss must stamp onboarding_dismissed_at"
    # No longer auto-shows, and it persists across a fresh request/session.
    assert "data-onboarding-panel" not in c.get("/").text, "dismissed card must not auto-show"
    fresh = TestClient(app)
    login(fresh, "admin", "admin")
    assert "data-onboarding-panel" not in fresh.get("/").text, (
        "dismissal must persist across sessions"
    )
    # Re-summon: ?tour=1 overrides the flag for display WITHOUT clearing it.
    resummoned = fresh.get("/?tour=1").text
    assert "data-onboarding-panel" in resummoned, "re-summon (?tour=1) must show the card again"
    assert _flag("admin") is not None, "re-summon must NOT clear the dismissed flag"
    print("  dismiss persists across requests/sessions; re-summon (?tour=1) overrides w/o clearing")


def test_donestate_and_autodismiss():
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    csrf_for(c)
    _rearm("admin")  # test setup: make the admin eligible again for a clean walk
    # 0/4 done: every step undone.
    html = c.get("/?tour=1").text
    assert 'class="ob-step" data-tour-step="customer"' in html, "customer step should start undone"
    # Create a customer via the REAL route -> the step flips done on next render.
    r = c.post("/customers", data={"name": "Acme Games"}, follow_redirects=False)
    assert r.status_code == 303, ("customer create", r.status_code)
    html = c.get("/?tour=1").text
    assert 'class="ob-step is-done" data-tour-step="customer"' in html, (
        "customer step must flip done after a real customer is created"
    )
    # Drive the remaining real creation routes to reach 4/4. Once a customer exists
    # the project route requires a customer_id, so resolve the just-created one.
    from app.models import Customer

    with Session(engine) as s:
        cust_id = s.exec(select(Customer.id)).first()
    r = c.post(
        "/projects",
        data={
            "name": "Site copy",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
            "customer_id": str(cust_id),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in (r.headers.get("location") or ""), (
        "project create",
        r.status_code,
        r.headers.get("location"),
    )
    r = c.post(
        "/workflows", data={"name": "MT + review", "scope": "global"}, follow_redirects=False
    )
    assert r.status_code == 303, ("workflow create", r.status_code)
    r = c.post(
        "/setup-ai/keys/claude", data={"api_key": "sk-test-key-onboarding"}, follow_redirects=False
    )
    assert r.status_code in (303, 302), ("provider key set", r.status_code)
    # Confirm the live-provider predicate now reads done.
    assert onboarding._has_live_provider(), "provider step should be done after a key is set"
    # D6: a plain dashboard GET at 4/4 auto-dismisses (card hidden + flag stamped).
    assert _flag("admin") is None, "flag should still be NULL right before the all-done GET"
    plain = c.get("/").text
    assert "data-onboarding-panel" not in plain, (
        "all-done card must auto-hide on the plain dashboard"
    )
    assert _flag("admin") is not None, "D6 must stamp the flag when all steps are done"
    # Re-summon still works and shows the collapsed 'all set up' confirmation.
    resummoned = c.get("/?tour=1").text
    assert "data-onboarding-panel" in resummoned, "re-summon must still work after auto-dismiss"
    # all-done confirmation (all_done_msg) rendered instead of the setup subtitle.
    # Plain fragment avoids the apostrophe/em-dash HTML escaping in the full string.
    assert "nice work" in resummoned, "all-done re-summon should show the confirmation message"
    assert "Finish setting up your workspace" not in resummoned, (
        "all-done card should show the confirmation, not the setup subtitle"
    )
    print(
        "  done-state flips from real records; D6 auto-dismiss at 4/4 (card hidden + flag set); "
        "re-summon shows the all-set confirmation"
    )


def test_scope_isolation():
    """Reviewer finding #1: a CUSTOMER-BOUND user's workflow/customer done-state
    must never read a FOREIGN customer's row (existence-only cross-customer
    disclosure). Isolated in-memory DB so it is independent of every other test's
    data — asserts the ACTUAL _has_workflow/_has_customer against real access
    predicates. Requires auth ON (current_customer_id no-ops with auth off); the
    suite has already called seed_initial by the time this runs."""
    assert security.auth_required(), "scope-isolation test needs the login wall ON"
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        a, b = Customer(name="IsoA"), Customer(name="IsoB")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        # Only workflow so far is SCOPED to customer B.
        s.add(Workflow(name="WfB", scope="customer", customer_id=b.id))
        s.commit()
        bound_a = SimpleNamespace(
            id=11, email="ling_a", role="linguist", org_id=None, customer_id=a.id
        )
        bound_b = SimpleNamespace(
            id=12, email="ling_b", role="linguist", org_id=None, customer_id=b.id
        )
        # THE BUG: an A-bound user must NOT see B's scoped workflow.
        assert onboarding._has_workflow(s, bound_a) is False, (
            "customer-bound user must not read a FOREIGN customer's workflow (isolation)"
        )
        assert onboarding._has_workflow(s, bound_b) is True, (
            "customer-bound user should see its OWN customer's workflow"
        )
        # A GLOBAL (shared) workflow is legitimately visible to every bound user.
        s.add(Workflow(name="WfGlobal", scope="global", customer_id=None))
        s.commit()
        assert onboarding._has_workflow(s, bound_a) is True, (
            "a global (shared) workflow should be visible to a bound user"
        )
        # _has_customer mirror: a bound user is confined to its own customer ring —
        # a user bound to a NON-existent customer must read False even though other
        # customers (IsoA/IsoB) exist (no cross-customer existence leak).
        assert onboarding._has_customer(s, bound_a) is True, (
            "bound user's own customer exists -> True"
        )
        ghost = SimpleNamespace(
            id=13, email="ling_ghost", role="linguist", org_id=None, customer_id=999999
        )
        assert onboarding._has_customer(s, ghost) is False, (
            "bound user must not read another customer's existence (confined to own ring)"
        )
    print(
        "  isolation: customer-bound _has_workflow/_has_customer confined to own "
        "ring (foreign scoped rows invisible; globals shared)"
    )


def test_dismissed_short_circuit():
    """Reviewer finding #2: a dismissed, non-forced dashboard GET must not run the
    four done-state queries. Patch the _has_* predicates to explode; a dismissed
    admin's dashboard_panel(force=False) must return None WITHOUT calling any."""
    with Session(engine) as s:
        admin = s.exec(select(User).where(User.email == "admin")).first()
        if admin.onboarding_dismissed_at is None:
            admin.onboarding_dismissed_at = datetime.utcnow()
            s.add(admin)
            s.commit()

    def boom(*a, **k):
        raise AssertionError("done-state predicate ran for a dismissed, non-forced GET")

    saved = (
        onboarding._has_customer,
        onboarding._has_project,
        onboarding._has_workflow,
        onboarding._has_live_provider,
    )
    onboarding._has_customer = boom
    onboarding._has_project = boom
    onboarding._has_workflow = boom
    onboarding._has_live_provider = boom
    try:
        with Session(engine) as s:
            admin = s.exec(select(User).where(User.email == "admin")).first()
            assert onboarding.dashboard_panel(s, admin, force=False) is None, (
                "dismissed non-forced GET should render no card"
            )
        # And forced re-summon DOES need them (sanity: patched -> raises -> proves
        # the queries run on the force path).
        raised = False
        try:
            with Session(engine) as s:
                admin = s.exec(select(User).where(User.email == "admin")).first()
                onboarding.dashboard_panel(s, admin, force=True)
        except AssertionError:
            raised = True
        assert raised, "forced re-summon must compute done-state (queries expected)"
    finally:
        (
            onboarding._has_customer,
            onboarding._has_project,
            onboarding._has_workflow,
            onboarding._has_live_provider,
        ) = saved
    print(
        "  short-circuit: dismissed non-forced GET runs zero done-state queries "
        "(forced re-summon still computes them)"
    )


def test_ineligible_roles_forced():
    """Reviewer finding #3: ?tour=1 (the forced re-summon) must render NO card for
    EVERY ineligible role — not just a translator's plain GET. Covers manager /
    reviewer / viewer (created here) + translator (user1)."""
    security.create_user("mgr", "Mgr", Role.MANAGER, "mgr", enforce_policy=False)
    security.create_user("rev", "Rev", Role.REVIEWER, "rev", enforce_policy=False)
    security.create_user("vwr", "Vwr", Role.VIEWER, "vwr", enforce_policy=False)
    for who, pw in (("mgr", "mgr"), ("rev", "rev"), ("vwr", "vwr"), ("user1", "user1")):
        c = TestClient(app)
        assert login(c, who, pw).status_code == 303, ("login", who)
        html = c.get("/?tour=1").text
        assert "data-onboarding-panel" not in html, (
            "forced ?tour=1 must render no card for ineligible role",
            who,
        )
        assert "data-tour-open" not in html and "/?tour=1" not in html, (
            "an ineligible role has no Get started entry",
            who,
        )
    print(
        "  ineligible roles: manager/reviewer/viewer/translator get NO card even "
        "under forced ?tour=1; no Get started entry"
    )


# --- Phase 3: parity-of-state (account show / reset-onboarding) ------------
def _mock_transport(token_client):
    """httpx.MockTransport forwarding the CLI --remote arm to the in-process
    TestClient (mirrors verify_ui_console/verify_headless_qa6_qa9). Preserves the
    X-Loc-Token header the RemoteClient sets, so the /api gate authenticates
    the same as a real call."""

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


def test_account_onboarding_parity():
    """SPEC-onboarding-tour Phase 3: the tour UX stays web-only, but the
    onboarding_dismissed_at flag is readable + resettable on the account
    surface (the `account show` precedent), on local + --remote:
      - GET /api/account carries `onboarding` additively; every pre-Phase-3
        field is byte-identical.
      - `account reset-onboarding` clears the flag; the dashboard card
        auto-shows again.
      - local CLI (--user) and --remote CLI (token = the account) agree.
      - RBAC: remote is strictly self-service (no --user); refused with exit 2.
    """
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    with Session(engine) as s:
        uid = s.exec(select(User.id).where(User.email == "admin")).first()
    _rearm("admin")  # clean eligible-and-not-dismissed starting state
    _row, raw = apikeys.create_key(uid, "onb-parity-test", scopes="api docs")
    hdr = {"X-Loc-Token": raw}

    # --- (1) /api/account: additive onboarding field + existing-fields parity ---
    before = c.get("/api/account", headers=hdr).json()
    pre_existing_keys = {"ok", "email", "name", "role", "ui_locale"}
    # ADR 0063 (2026-10-08) adds ui_theme, the per-account appearance; #751 adds
    # scopes, the calling key's own grants.
    assert set(before.keys()) == pre_existing_keys | {"onboarding", "ui_theme", "scopes"}, (
        "onboarding must be ADDITIVE only - no pre-Phase-3 key added/removed",
        before.keys(),
    )
    assert (
        before["ok"],
        before["email"],
        before["role"],
        before["ui_locale"],
    ) == (True, "admin", "admin", ""), (
        "existing /api/account fields must be byte-identical to pre-Phase-3",
        before,
    )
    assert "ui_skin" not in before and "skins" not in before, "the skin fields are gone"
    assert before["onboarding"] == {"dismissed_at": None, "eligible": True, "variant": "admin"}, (
        "a fresh, eligible, undismissed admin -> dismissed_at null",
        before,
    )
    print("  /api/account: onboarding is additive; every pre-Phase-3 field byte-identical")

    # --- (2) dismiss (the Phase-1 web route) -> the flag surfaces set ---
    csrf_for(c)
    assert c.post("/onboarding/dismiss", follow_redirects=False).status_code == 303
    after_dismiss = c.get("/api/account", headers=hdr).json()["onboarding"]
    assert after_dismiss["dismissed_at"] is not None, after_dismiss
    assert after_dismiss["eligible"] is True and after_dismiss["variant"] == "admin", (
        "eligible/variant are role-derived, unaffected by dismissal",
        after_dismiss,
    )
    assert "data-onboarding-panel" not in c.get("/").text, "dismissed card must not auto-show"
    print("  /api/account: dismissed_at surfaces once dismissed; eligible/variant unaffected")

    # --- (3) POST /api/account/onboarding-reset clears it; card returns ---
    r = c.post("/api/account/onboarding-reset", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    reset_body = r.json()
    assert reset_body["ok"] is True, reset_body
    assert reset_body["onboarding"] == {
        "dismissed_at": None,
        "eligible": True,
        "variant": "admin",
    }, reset_body
    assert _flag("admin") is None, "reset must clear onboarding_dismissed_at (NULL)"
    assert "data-onboarding-panel" in c.get("/").text, (
        "reset must re-arm the dashboard checklist (auto-shows again once NULL)"
    )
    print("  POST /api/account/onboarding-reset clears the flag; dashboard card returns")

    with Session(engine) as s:
        acts = [a.action for a in s.exec(select(AuditLog).where(AuditLog.actor == "admin")).all()]
    assert "account.reset_onboarding" in acts, ("reset must audit", acts)

    # --- (4) CLI in-process: `account show` surfaces onboarding; reset clears --
    csrf_for(c)
    c.post("/onboarding/dismiss", follow_redirects=False)  # re-dismiss for a clean re-test
    assert _flag("admin") is not None
    rc, out, err = run_cli(["--json", "account", "show", "--user", "admin"])
    assert rc == 0, (rc, err)
    local_show = _json.loads(out)
    assert local_show["onboarding"]["dismissed_at"] is not None, local_show
    assert local_show["onboarding"]["eligible"] is True, local_show
    assert local_show["onboarding"]["variant"] == "admin", local_show
    human_rc, human_out, _ = run_cli(["account", "show", "--user", "admin"])
    assert human_rc == 0 and "onboarding dismissed_at:" in human_out, human_out
    rc, out, err = run_cli(["--json", "account", "reset-onboarding", "--user", "admin"])
    assert rc == 0, (rc, err)
    local_reset = _json.loads(out)
    assert local_reset["onboarding"] == {
        "dismissed_at": None,
        "eligible": True,
        "variant": "admin",
    }, local_reset
    assert _flag("admin") is None, "in-process reset-onboarding must clear the flag"
    # missing --user in-process is a clean usage error (mirrors show).
    rc, _, err = run_cli(["account", "reset-onboarding"])
    assert rc == 2, ("in-process reset-onboarding needs --user", rc, err)
    print(
        "  CLI (in-process): `account show`/`reset-onboarding --user` parity + "
        "missing --user rejected"
    )

    # --- (5) CLI over --remote: token-scoped self-service parity + RBAC -------
    csrf_for(c)
    c.post("/onboarding/dismiss", follow_redirects=False)  # re-dismiss for the remote leg
    assert _flag("admin") is not None
    remote_api_state = c.get("/api/account", headers=hdr).json()["onboarding"]
    _remote._set_transport(_mock_transport(c))
    try:
        rc, out, err = run_cli(
            ["--json", "--remote", "http://127.0.0.1", "--token", raw, "account", "show"]
        )
        assert rc == 0, (rc, err)
        remote_show = _json.loads(out)
        # (3) remote arm parity: the --remote CLI must match the direct /api call.
        assert remote_show["onboarding"] == remote_api_state, (
            "--remote CLI onboarding must equal the direct /api/account call",
            remote_show["onboarding"],
            remote_api_state,
        )
        rc, out, err = run_cli(
            [
                "--json",
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "account",
                "reset-onboarding",
            ]
        )
        assert rc == 0, (rc, err)
        remote_reset = _json.loads(out)
        assert remote_reset["onboarding"] == {
            "dismissed_at": None,
            "eligible": True,
            "variant": "admin",
        }, remote_reset
        assert _flag("admin") is None, "--remote reset-onboarding must clear the flag"
        # RBAC: remote is strictly self-service - --user is REFUSED (exit 2), never
        # silently acting on the caller, on BOTH the read and the reset leaf.
        rc, _, err = run_cli(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "account",
                "show",
                "--user",
                "someone@else.com",
            ]
        )
        assert rc == 2 and "in-process only" in err, ("remote show --user must refuse", rc, err)
        rc, _, err = run_cli(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                raw,
                "account",
                "reset-onboarding",
                "--user",
                "someone@else.com",
            ]
        )
        assert rc == 2 and "in-process only" in err, (
            "remote reset-onboarding --user must refuse",
            rc,
            err,
        )
    finally:
        _remote._set_transport(None)
    print(
        "  CLI (--remote): token-scoped show/reset-onboarding == /api/account "
        "(parity); --user REFUSED remotely on both leaves (RBAC self-service-only)"
    )


def main():
    init_db()
    # Pure-data / textual checks (no app state).
    test_migration_backfill()
    test_both_write_sites()
    test_vendored_driver()
    test_onboarding_js_no_cdn()
    test_close_label_unconditional()
    _target_pages_no_hardcoded_english()
    test_i18n_parity()
    # D7 runs while auth is still OFF.
    test_authoff_ineligible()
    # Turn the login wall on (admin/admin, linguist/linguist, user1/user1).
    security.seed_initial()
    test_admin_autoshow()
    test_linguist_variant()
    test_translator_none()
    test_ineligible_roles_forced()
    test_scope_isolation()
    test_showme_markup()
    test_target_anchors_served()
    test_ineligible_target_no_assets()
    test_spotlight_param_no_reflection()
    test_dismiss_and_resummon()
    # Run BEFORE test_donestate_and_autodismiss: that test drives admin's steps
    # to real 4/4, which would immediately D6-auto-re-dismiss any plain "/" GET
    # this test relies on to observe the card reappearing after a reset.
    test_account_onboarding_parity()
    test_donestate_and_autodismiss()
    test_dismissed_short_circuit()
    settings.set_many({"security_auth_required": "0"})
    print("ONBOARDING OK")


if __name__ == "__main__":
    main()
