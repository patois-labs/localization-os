"""Accessibility contact setting and the /help Accessibility section (a11y train T10).

Offline and hermetic (throwaway data dir). Proves:

  V  the validator (settings.accessibility_contact_kind, registered for every write door):
     accepts a plain email, an https link and blank; refuses javascript:, data:, http:,
     mailto:, markup, quotes, surrounding spaces, control characters, credentials in the
     link and anything over 254 characters. The shipped default is blank and valid.
     set_many (the chokepoint every door funnels through) refuses a bad value too.
  R  the read accessor: email -> mailto link, https -> the link, blank -> nothing, and a
     hand-edited invalid database row -> treated as blank (never rendered as a link).
  C  CLI: config set refuses a bad value (usage exit, nothing written), stores a good one,
     config get reads it back, and the change is audit-logged.
  A  /api config: GET lists the key, POST stores a good value (audit-logged) and refuses a
     bad one with 400 and no write.
  W  the web Security page: admin-only; renders the field with its label and help; a bad
     value refuses the WHOLE save and comes back as the page error summary linked to the
     field (aria-invalid); a good value is stripped, saved and audit-logged; an omitted
     field leaves the value unchanged.
  H  /help: the Accessibility section and its TOC link; the contact rendered as a mailto
     link, an https link (escaped) or "Contact your administrator" when blank; no template
     hardcodes the contact; the stray "Workflow spend preview" paragraph now lives in the
     Workflows section; no em dash in the new section; every keyboard shortcut the section
     documents still has its handler in the source it names (a doc-truth pin).

Prints ASCII only; ends with A11Y CONTACT OK.
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_a11y_contact_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role, Setting
from app.services import apikeys, security, settings
from tests._cli_runner import run_cli
from tests._csrf import session_data

KEY = "accessibility_contact"
CONTACT = "accessibility@example.org"  # the shipped default is blank; the suite configures this one
ADMIN = "admin@x.com"
ADMIN_PW = "contact-suite-pass-2026"  # pregate: allow-secret throwaway fixture login
TRANSLATOR = "tr@x.com"
TRANSLATOR_PW = "contact-suite-tr-2026"  # pregate: allow-secret throwaway fixture login

GOOD = [
    "",
    CONTACT,
    "a11y@example.org",
    "first.last+tag@sub.example.co.uk",
    "https://example.com",
    "https://example.com/accessibility?x=1&y=2#report",
    "HTTPS://Example.com/a11y",
    "https://intranet/helpdesk",
    "https://example.com:8443/help/accessibility-report?lang=en",
]
BAD = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<b>x</b>",
    "http://example.com/a11y",
    "mailto:a11y@example.org",
    "ftp://example.com",
    "https://",
    "https:///path",
    "https://user:pw@example.com/",
    "https://trusted.example@evil.example/",
    "<script>alert(1)</script>",
    "<b>a11y@example.org</b>",
    '"a11y"@example.org',
    "a11y@example.org<script>",
    "a11y@example",
    "a11y@@example.org",
    "a11y example@example.org",
    'https://example.com/"onmouseover="alert(1)',
    "https://example.com/'x'",
    "https://example.com/<x>",
    "https://example.com/a b",
    "https://example.com\\@evil.example",
    " a11y@example.org",
    "a11y@example.org ",
    "a11y@example.org\n",
    "https://example.com/\x07",
    # a link is printable ASCII only: bidi overrides, look-alike letters, NBSP
    "https://example.com/‮gpj.exe",
    "https://exаmple.com/a11y",
    "https://example.com/café",
    "https://example.com/a b",
    # the port must parse
    "https://example.com:99999/",
    "https://example.com:x/",
    "just text",
    "a" * 64 + "@" + "b" * 60 + "." + "c" * 60 + "." + "d" * 60 + "." + "e" * 60 + ".com",
    "https://example.com/" + "p" * 240,
]


def _read(rel: str) -> str:
    """Source text with CRLF normalized (a Windows checkout may be autocrlf)."""
    with open(os.path.join(_ROOT, rel), "rb") as fh:
        return fh.read().decode("utf-8").replace("\r\n", "\n")


def _latest_audit(action: str):
    with Session(engine) as s:
        return s.exec(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())
        ).first()


def _raw_row(value: str) -> None:
    """A hand-edited row that bypasses every write door (and its validator)."""
    with Session(engine) as s:
        row = s.get(Setting, KEY)
        if row is None:
            s.add(Setting(key=KEY, value=value))
        else:
            row.value = value
            s.add(row)
        s.commit()
    settings.invalidate_cache()


def login(email=ADMIN, pw=ADMIN_PW) -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": pw, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303 and session_data(c).get("uid"), (r.status_code, r.text[:200])
    return c


def token_for(c) -> str:
    tok = session_data(c).get("csrf")
    if not tok:
        c.get("/account")
        tok = session_data(c).get("csrf") or ""
    assert tok, "no CSRF token"
    return tok


def check_validator() -> None:
    assert settings.DEFAULTS[KEY] == "", "the shipped default must be blank"
    assert KEY in settings.VALUE_VALIDATORS
    for good in GOOD:
        settings.validate_value(KEY, good)
    for bad in BAD:
        try:
            settings.validate_value(KEY, bad)
        except settings.InvalidSettingValue:
            continue
        raise AssertionError(f"accessibility_contact accepted {bad!r}")
    assert len(BAD[-2]) > settings.ACCESSIBILITY_CONTACT_MAX
    assert len(BAD[-1]) > settings.ACCESSIBILITY_CONTACT_MAX
    # the chokepoint: set_many refuses a bad value and writes nothing
    before = settings.get(KEY)
    try:
        settings.set_many({KEY: "javascript:alert(1)"})
    except settings.InvalidSettingValue:
        pass
    else:
        raise AssertionError("set_many stored a javascript: contact")
    assert settings.get(KEY) == before
    print(f"  V: {len(GOOD)} good values accepted, {len(BAD)} bad values refused; set_many too")


def check_accessor() -> None:
    settings.set_many({KEY: CONTACT})
    assert settings.accessibility_contact() == {
        "kind": "email",
        "value": CONTACT,
        "href": "mailto:" + CONTACT,
    }
    settings.set_many({KEY: "https://example.com/a11y"})
    assert settings.accessibility_contact() == {
        "kind": "url",
        "value": "https://example.com/a11y",
        "href": "https://example.com/a11y",
    }
    settings.set_many({KEY: ""})
    assert settings.accessibility_contact() == {"kind": "", "value": "", "href": ""}
    for raw in ("javascript:alert(1)", "<img src=x onerror=alert(1)>", "http://x.example"):
        _raw_row(raw)
        assert settings.get(KEY) == raw, "the raw row did not land"
        assert settings.accessibility_contact()["kind"] == "", raw
    settings.set_many({KEY: CONTACT})
    print("  R: email -> mailto, https -> link, blank -> none, a bad stored row -> none")


def check_cli() -> None:
    code, _out, _err = run_cli(["config", "set", KEY, "javascript:alert(1)"])
    assert code != 0 and settings.get(KEY) == CONTACT, (code, _err)
    code, _out, _err = run_cli(["config", "set", KEY, "http://example.com"])
    assert code != 0 and settings.get(KEY) == CONTACT, (code, _err)
    code, _out, err = run_cli(["config", "set", KEY, "help@example.org"])
    assert code == 0 and settings.get(KEY) == "help@example.org", (code, err)
    row = _latest_audit("config.set")
    assert row is not None and row.target == KEY and "help@example.org" in row.detail, row
    code, out, _err = run_cli(["config", "get", KEY])
    assert code == 0 and "help@example.org" in out, (code, out)
    code, out, _err = run_cli(["config", "list"])
    assert code == 0 and KEY in out, "config list does not show the key"
    code, _out, _err = run_cli(["config", "set", KEY, ""])
    assert code == 0 and settings.get(KEY) == "", (code, _err)
    settings.set_many({KEY: CONTACT})
    print("  C: config set refuses bad values, round-trips a good one, audit-logged")


def check_api() -> None:
    admin = security.get_by_email(ADMIN)
    _row, raw = apikeys.create_key(admin.id, "a11y-contact", scopes="api docs admin")
    h = {"X-Loc-Token": raw}
    settings.set_many({"remote_admin_enabled": "1"})
    c = TestClient(app)
    try:
        r = c.get("/api/admin/config", headers=h)
        assert r.status_code == 200 and r.json()["settings"][KEY] == CONTACT, r.text[:300]
        r = c.post("/api/admin/config", headers=h, json={"settings": {KEY: "javascript:alert(1)"}})
        assert r.status_code == 400 and settings.get(KEY) == CONTACT, (r.status_code, r.text)
        r = c.post(
            "/api/admin/config", headers=h, json={"settings": {KEY: "https://a11y.example.org"}}
        )
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json()["settings"][KEY] == "https://a11y.example.org"
        assert settings.get(KEY) == "https://a11y.example.org"
        with Session(engine) as s:
            rows = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).all()[:5]
        assert any("https://a11y.example.org" in (r_.detail or "") for r_ in rows), [
            (r_.action, r_.detail) for r_ in rows
        ]
    finally:
        settings.set_many({"remote_admin_enabled": "0", KEY: CONTACT})
    print("  A: /api config lists, stores (audited) and refuses (400, no write)")


def check_web() -> None:
    # translator: the page and the save are refused, nothing written
    t = login(TRANSLATOR, TRANSLATOR_PW)
    assert t.get("/security").status_code == 403
    r = t.post(
        "/security",
        data={KEY: "evil@example.org", "csrf_token": token_for(t)},
        follow_redirects=False,
    )
    assert r.status_code == 403 and settings.get(KEY) == CONTACT, r.status_code

    c = login()
    tok = token_for(c)
    page = c.get("/security").text
    assert 'id="accessibility-contact"' in page and f'value="{CONTACT}"' in page
    assert "<label>Accessibility contact" in page
    assert 'aria-describedby="accessibility-contact-help"' in page
    assert 'href="/help#accessibility"' in page
    base = {"security_auth_required": "1", "audit_enabled": "1", "csrf_token": tok}

    # a bad value refuses the WHOLE save (the idle limit in the same post is not written)
    idle_before = settings.get("session_idle_minutes")
    r = c.post(
        "/security",
        data={**base, KEY: "javascript:alert(1)", "session_idle_minutes": "33"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/security?err=a11y_contact", r.headers["location"]
    assert settings.get(KEY) == CONTACT
    assert settings.get("session_idle_minutes") == idle_before, "a refused save wrote"
    err_page = c.get("/security?err=a11y_contact").text
    assert 'role="alert"' in err_page and 'href="#accessibility-contact"' in err_page
    assert "accessibility contact must be an email address" in err_page
    field = re.search(r'<input[^>]*id="accessibility-contact"[^>]*>', err_page).group(0)
    assert 'aria-invalid="true"' in field, field
    assert 'aria-describedby="accessibility-contact-help page-error-msg"' in field, field
    assert "<title>Error:" in err_page

    # a good value is stripped, saved and audit-logged
    r = c.post(
        "/security",
        data={**base, KEY: "  a11y@example.org  "},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/security?saved=1", r.headers["location"]
    assert settings.get(KEY) == "a11y@example.org"
    row = _latest_audit("security.update")
    assert f"{KEY} old={CONTACT} new=a11y@example.org" in row.detail, row.detail

    # blank is allowed (the help page then says to contact the administrator). The browser
    # sends the card's hidden marker with the cleared field, which is what the page renders.
    assert 'name="accessibility_contact_sent" value="1"' in page
    sent = {"accessibility_contact_sent": "1"}
    r = c.post("/security", data={**base, **sent, KEY: ""}, follow_redirects=False)
    assert r.headers["location"] == "/security?saved=1" and settings.get(KEY) == ""
    r = c.post("/security", data={**base, **sent, KEY: "   "}, follow_redirects=False)
    assert r.headers["location"] == "/security?saved=1" and settings.get(KEY) == ""

    # an omitted field leaves the value alone
    settings.set_many({KEY: "keep@example.org"})
    r = c.post("/security", data=base, follow_redirects=False)
    assert r.headers["location"] == "/security?saved=1"
    assert settings.get(KEY) == "keep@example.org", "an omitted field reset the contact"
    settings.set_many({KEY: CONTACT})
    print("  W: admin-only; bad value refuses the whole save with a linked error; saved + audited")


def _section(html: str) -> str:
    m = re.search(
        r'<section class="doc-section" id="accessibility">(.*?)</section>', html, flags=re.S
    )
    assert m, "the Accessibility section is missing"
    return m.group(1)


def check_help() -> None:
    c = login()
    html = c.get("/help").text
    toc = re.search(r'<nav class="docs-toc"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert 'href="#accessibility">Accessibility</a>' in toc
    sec = _section(html)
    for heading in (
        "Keyboard",
        "Keyboard shortcuts",
        "Zoom and reflow",
        "Themes and motion",
        "Screen readers",
        "Forms and errors",
        "Signing out after inactivity",
        "Known issues",
        "How to report a problem",
    ):
        assert f">{heading}</h3>" in sec, heading
    assert "WCAG" in sec and "2.2" in sec and "AA" in sec
    assert 'href="#sessions"' in sec and 'id="sessions"' in html
    assert f'<a href="mailto:{CONTACT}">{CONTACT}</a>' in sec, "default contact not linked"
    assert "—" not in sec and "&mdash;" not in sec, "em dash in the section"

    settings.set_many({KEY: "https://example.com/a11y?x=1&y=2"})
    sec = _section(c.get("/help").text)
    assert 'href="https://example.com/a11y?x=1&amp;y=2"' in sec, "the link is not escaped"
    assert "mailto:" not in sec

    settings.set_many({KEY: ""})
    sec = _section(c.get("/help").text)
    assert "Contact your administrator." in sec and "mailto:" not in sec

    _raw_row("javascript:alert(1)")
    sec = _section(c.get("/help").text)
    assert "javascript:" not in sec and "Contact your administrator." in sec
    settings.set_many({KEY: CONTACT})

    # the contact is data: no template hardcodes it (or any mailto: address)
    tdir = os.path.join(_ROOT, "app", "templates")
    for name in sorted(os.listdir(tdir)):
        if name.endswith(".html"):
            src = _read(f"app/templates/{name}")
            assert "patoislabs" not in src.lower(), f"{name} hardcodes the vendor contact"
            assert "mailto:" not in src, f"{name} hardcodes a mailto: link"

    # the stray paragraph moved into the Workflows section
    src = _read("app/templates/help.html")
    # Only the page wrapper and its page_header may sit above the docs (UX branch 5).
    assert (
        '{% block content %}\n<div class="help-page">\n'
        "{% set help_meta %}<p>Guides for every module, plus the REST API reference.</p>"
        "{% endset %}\n"
        '{{ m.page_header(brand ~ " documentation", meta=help_meta) }}\n<div class="docs">'
    ) in src, "stray content above the docs"
    wf = re.search(r'<section class="doc-section" id="workflows">(.*?)</section>', src, re.S)
    assert wf and "Preview spend" in wf.group(1) and "--plan" in wf.group(1)
    assert src.count("Preview spend") == 1
    print("  H: section + TOC, mailto / https / blank / bad-row rendering, no hardcoded contact")


# Every shortcut the help section documents, and the source line that implements it. If a
# handler goes away, the help would be describing a shortcut that no longer exists.
SHORTCUT_PINS = [
    ("app/templates/editor.html", "if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {"),
    ("app/templates/editor.html", "} else if (e.key === 'Enter' && !e.shiftKey) {"),
    ("app/templates/editor.html", "} else if (isAltA(e)) {"),
    (
        "app/templates/editor.html",
        "if (onMac()) return e.ctrlKey && e.shiftKey && !e.altKey && e.code === 'KeyA';",
    ),
    ("app/templates/editor.html", "if (e.key === 'Escape' || isAltA(e)) {"),
    ("app/templates/editor.html", "else if (e.key === 'Home') next = 0;"),
    ("app/templates/editor.html", "else if (e.key === 'End') next = items.length - 1;"),
    ("app/templates/editor.html", "if (e.key === 'Enter') { e.preventDefault(); concordance(); }"),
    ("app/static/usermenu.js", 'if (e.key === "Enter" || e.key === " " || e.key === "Spacebar")'),
    ("app/static/usermenu.js", 'if (e.key === "ArrowDown") {'),
    ("app/static/usermenu.js", 'if (e.key === "Home") {'),
    ("app/static/usermenu.js", "typeahead(menu, list, current, e.key.toLowerCase())"),
    ("app/static/usermenu.js", "closeMenu(menu, true);"),
    ("app/static/mobilenav.js", 'if (e.key === "Escape" && isOpen()) {'),
    ("app/static/mobilenav.js", 'if (e.key === "Tab" && isOpen() && sidebar) {'),
    (
        "app/static/sidebar_ux.js",
        'if (e.key === "Enter") { e.preventDefault(); stepWidth("reset");',
    ),
    ("app/static/sidebar_ux.js", 'action === "ArrowLeft"'),
    ("app/static/sidebar_ux.js", "var WIDTH_STEP = 20;"),
    ("app/templates/workflows_new.html", "if (ev.key === 'ArrowRight')"),
    ("app/templates/workflows_new.html", "else if (ev.key === 'End') index = items.length - 1;"),
    ("app/static/wfcanvas.js", "if (ev.key === 'Escape' && drag) end(true);"),
    (
        "app/static/session_timeout.js",
        'if (mode === "warning" || mode === "out") { e.preventDefault(); }',
    ),
    ("app/static/busy.js", "if (e.key !== 'Escape' && e.key !== 'Esc') { return; }"),
    ("app/static/page_layout.js", 'if (editing && !pending && e.key === "Escape"'),
    # Command palette (UX branch 4): Ctrl+K, Cmd+K on a Mac.
    (
        "app/static/command_palette.js",
        "!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k' ||",
    ),
]


def check_shortcut_pins() -> None:
    for rel, needle in SHORTCUT_PINS:
        assert needle in _read(rel), f"{rel}: documented shortcut handler gone: {needle}"
    # The section documents exactly these key names; a new key in the section must come
    # with a pin above (and a handler).
    sec = _section(_read("app/templates/help.html"))
    keys = set(re.findall(r"<kbd>([^<]+)</kbd>", sec))
    assert keys == {
        "Tab",
        "Enter",
        "Shift+Enter",
        "Ctrl+Enter",
        "Cmd+Enter",
        "Ctrl+K",
        "Cmd+K",
        "Alt+A",
        "Ctrl+Shift+A",
        "Up",
        "Down",
        "Home",
        "End",
        "Escape",
        "Space",
        "Left",
        "Right",
    }, sorted(keys)
    print(f"  pins: {len(SHORTCUT_PINS)} documented shortcut handlers still in the source")
    # T11: the Mac alternates shortcut is not a VoiceOver command. Ctrl+Option is the
    # VoiceOver modifier (VO+A reads everything), so the page never receives it: the Mac
    # chord must hold Ctrl and Shift and must exclude Option, and nothing a person reads
    # (help, the Editor legend, the spoken hint) may name a Ctrl+Option shortcut.
    ed = _read("app/templates/editor.html")
    mac = re.search(r"if \(onMac\(\)\) return ([^;]*);", ed)
    assert mac, "editor.html: isAltA lost its Mac branch"
    expr = mac.group(1)
    assert "e.ctrlKey" in expr and "e.shiftKey" in expr and "!e.altKey" in expr, expr
    assert re.search(r"onMac\(\) \? 'Ctrl\+Shift\+A' : 'Alt\+A'", ed), "altAName() drifted"
    for rel in ("app/templates/help.html", "app/templates/editor.html"):
        assert not re.search(r"Ctrl\+Option\+[A-Za-z]</kbd>", _read(rel)), rel
    assert "Ctrl+Option+A" not in sec, "help still documents the VoiceOver chord"
    print("  mac shortcut: Ctrl+Shift+A, never Ctrl+Option (VoiceOver's modifier)")


def main() -> None:
    init_db()
    c = TestClient(app)
    r = c.post(
        "/auth/setup",
        data={"email": ADMIN, "name": "Admin", "password": ADMIN_PW},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    security.create_user(TRANSLATOR, "Translator", Role.TRANSLATOR, TRANSLATOR_PW)
    settings.set_many({"security_auth_required": "1"})
    check_validator()
    check_accessor()
    check_cli()
    check_api()
    check_web()
    check_help()
    check_shortcut_pins()
    print("A11Y CONTACT OK")


if __name__ == "__main__":
    main()
