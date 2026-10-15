"""Page errors, a11y train T6 (WCAG 3.3.1 error identification, 3.3.3 error suggestion).

Hermetic (throwaway LOC_TMS_DATA_DIR, no provider keys, no network). Proves:

  1. RESOLVER (app/web/form_errors.py): a known code resolves to its en sentence plus the
     field it marks; no + phrase code is left in the map (T7 made them snake_case); an
     unknown code-shaped value shows the generic sentence (with the value as a reference)
     AND logs a WARNING; free text is whitespace-collapsed and capped at MAX_TEXT; de falls
     back to the en sentence; the resolver order is explicit > ?err > ?error > page alias.
  2. REDIRECT HELPER: error_url / redirect_with_error round-trip #, &, %, + and spaces
     exactly through the query string (the old unquoted f-strings truncated at # and &),
     append &err= to a URL that already has a query, add #anchor, keep known codes
     byte-identical on the wire, and never build an off-site target (open redirect).
  3. RENDERED SUMMARY: role="alert", tabindex="-1", id="page-error", the sentence, a
     "Go to the field" link, and the "Error:" title prefix; the mapped field carries
     aria-invalid="true" + aria-describedby naming its inline message; <script> in ?err=
     is inert (autoescaped) and a code never prints raw.
  4. PROJECT PAGE: no project code renders raw anywhere (the deleted Target languages
     catch-all), each renders its sentence once in the summary.
  5. LOGIN REGRESSION (the SAML button vanished and the username was cleared after one
     failed password): after a wrong password the SAML and SSO buttons are still there,
     the username is echoed back, the password is never echoed, both fields are
     aria-invalid, and the page focuses the summary (form_errors.js is loaded).
  6. Core pages: glossary detail (blank_term code + field), account alias (pwfail),
     settings alias (srx_error), setup + accept re-renders (echo, field marked).
  8. T7 pages, each driven through its real route where one exists: vendors (a code with a
     field, and free text), data export (bad_scope links its section, scope_needs_id marks
     the id select), run
     view (free text once, no "Could not complete:"), Setup AI spend cap, reports dates,
     project delete confirmation, and the New project dialog (summary in main AND in
     the dialog). Each: role=alert summary, sentence once, no class name, field marked.

Final line: FORM ERRORS OK.
"""

import logging
import os
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_form_errors_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.i18n import loader as i18n
from app.kernel import security, settings
from app.main import app
from app.models import Customer, Project
from app.web import form_errors as fe
from tests._csrf import csrf_for, record_message, session_data

_WRONG_PW = "definitely-not-the-password-9431"  # pregate: allow-secret throwaway wrong password
_SEED_PW = "admin"  # pregate: allow-secret the dev seed account from security.seed_initial
_PW_A = "long-enough-pw-1"  # pregate: allow-secret throwaway invite password
_PW_B = "different-pw-22"  # pregate: allow-secret throwaway mismatched confirmation


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        _fail(msg)


def _login_next(c, nxt):
    """Password sign-in as the seed admin with a caller-supplied next (no follow)."""
    from tests._csrf import csrf_value

    tok = csrf_value(c)
    return c.post(
        "/auth/login",
        data={"email": "admin", "password": _SEED_PW, "next": nxt, "csrf_token": tok},
        follow_redirects=False,
    )


def _allow(c, value, name="err"):
    """Record free text for c's session (see tests/_csrf.py::record_message)."""
    return record_message(c, value, name)


class _Text(HTMLParser):
    """Visible text nodes (script/style/title excluded) and every element's attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self.elems: list[tuple[str, dict]] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self.elems.append((tag, dict(attrs)))
        if tag in ("script", "style", "title"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "title") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.texts.append(" ".join(data.split()))


def parse(html: str) -> _Text:
    p = _Text()
    p.feed(html)
    return p


def message_dom(html: str) -> str:
    """Inspect content and attributes without mistaking asset mtimes for message text."""

    class MessageDOM(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_starttag(self, tag, attrs):
            self.parts.append(tag)
            for name, value in attrs:
                value = value or ""
                if (tag, name) in (("link", "href"), ("script", "src")) and re.fullmatch(
                    r"/static/[A-Za-z0-9_./-]+\?v=[0-9]+", value
                ):
                    value = urlsplit(value).path
                self.parts.extend((name, value))

        def handle_data(self, data):
            self.parts.append(data)

        def handle_comment(self, data):
            self.parts.append(data)

    dom = MessageDOM()
    dom.feed(html)
    return "\n".join(dom.parts)


def test_message_dom():
    asset = '<link rel="stylesheet" href="/static/style.css?v=1791175550">'
    check("555" not in message_dom(asset), "asset timestamp is not message content")
    for echoed in (
        "<p>555 0100</p>",
        '<input value="555">',
        '<a href="/call/555">Call</a>',
        '<div aria-label="555"></div>',
        '<script>const phone = "555";</script>',
        '<script src="https://example.test/script.js?v=555"></script>',
        "<p>&#53;&#53;&#53;</p>",
    ):
        check("555" in message_dom(asset + echoed), "message DOM must detect echoed content")


def elem(html: str, ident: str) -> dict:
    for _tag, attrs in parse(html).elems:
        if attrs.get("id") == ident:
            return attrs
    _fail(f"no element with id={ident!r}")
    return {}


def title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return " ".join(m.group(1).split()) if m else ""


def summary_text(html: str) -> str:
    m = re.search(r'<p id="page-error-msg">(.*?)</p>', html, re.S)
    return m.group(1) if m else ""


def en(key: str, **kw) -> str:
    return str(i18n.translate("en", key, **kw))


# --------------------------------------------------------------------------- #
# 1. resolver
# --------------------------------------------------------------------------- #
def test_resolver():
    e = fe.resolve("format")
    check(e and e.text == en("errors.format") and e.field == "doc-upload-file", e)
    check(e.code == "format", e)
    # T7: the + phrase codes are snake_case now (a key survives the query parser as is).
    check(not any("+" in k or " " in k for k in fe.ERRORS), "no + phrase code keys left")
    # T7 gate LOW 1: an old bookmark still carrying a retired phrase code (decoded by the
    # query parser, or raw with +) resolves to the new code's sentence, never an echo.
    for old, new in fe.LEGACY_PHRASES.items():
        check(new in fe.ERRORS, f"legacy {old!r} -> unknown code {new!r}")
        for form in (old, old.replace(" ", "+"), old.upper()):
            e = fe.resolve(form)
            check(e and e.code == new and e.text == en(fe.ERRORS[new].key), (form, e))
    e = fe.resolve("not found")  # too vague to place: the generic sentence
    check(e.text == en("errors.generic", ref="not found"), e)
    check(fe.resolve("") is None and fe.resolve(None) is None and fe.resolve("  ") is None, "blank")

    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = _H(level=logging.WARNING)
    logging.getLogger("loc_tms").addHandler(h)
    try:
        e = fe.resolve("no_such_code_xyz")
    finally:
        logging.getLogger("loc_tms").removeHandler(h)
    check(e.text == en("errors.generic", ref="no_such_code_xyz"), e)
    check("reference: no_such_code_xyz" in e.text, e.text)
    check(
        any("no_such_code_xyz" in r.getMessage() and r.levelno == logging.WARNING for r in records),
        "an unknown code must log a WARNING",
    )

    long = "word " * 200
    e = fe.resolve(long)
    check(len(e.text) <= fe.MAX_TEXT and e.text.endswith("..."), (len(e.text), e.text[-10:]))
    e = fe.resolve("  The   file\n is broken. ")
    check(e.text == "The file is broken." and e.code is None and e.field is None, e)

    check(fe.resolve("format", "de").text == en("errors.format"), "de must fall back to en")

    q = {"err": "format", "error": "parse", "saved": "pwfail"}
    check(fe.raw_value(q, "/account", explicit="x") == "x", "explicit wins")
    check(fe.raw_value(q, "/account") == "format", "?err before ?error")
    check(fe.raw_value({"error": "parse"}, "/x") == "parse", "?error")
    check(fe.raw_value({"saved": "pwfail"}, "/account") == "pwfail", "account alias")
    check(fe.raw_value({"saved": "name"}, "/account") is None, "a success value is no error")
    check(fe.raw_value({"saved": "pwfail"}, "/projects") is None, "alias is per page")
    check(fe.raw_value({"srx_error": "bad"}, "/settings") == "bad", "settings alias")

    # every mapped sentence exists in en.json and has no em dash
    for code, spec in fe.ERRORS.items():
        text = en(spec.key)
        check(text != spec.key, f"missing en sentence for {code} ({spec.key})")
        check(chr(0x2014) not in text, f"em dash in {spec.key}")
    for key in ("summary_title", "title_prefix", "go_to_field", "go_to_section", "generic"):
        check(en(f"errors.{key}") != f"errors.{key}", key)
    print("  1. resolver: codes, phrase codes, unknown code + WARNING, cap, fallback, order OK")


# --------------------------------------------------------------------------- #
# 2. redirect helper
# --------------------------------------------------------------------------- #
def _roundtrip(url: str, param: str = "err") -> tuple[str, str, str]:
    parts = urlsplit(url)
    return parts.path, parse_qs(parts.query).get(param, [""])[0], parts.fragment


def test_redirect_helper():
    for msg in (
        "Name has a # in it",
        "Tom & Jerry",
        "100% done",
        "a+b=c?d",
        "spaces   and\ttabs",
        "quote ' and \" chars",
        "Ünïcödé text",
    ):
        path, value, frag = _roundtrip(fe.error_url("/glossaries/7", msg))
        check(path == "/glossaries/7" and value == msg and frag == "", (msg, path, value, frag))
    url = fe.error_url("/glossaries?customer=3", "Tom & Jerry")
    check(url.startswith("/glossaries?customer=3&err="), url)
    check(parse_qs(urlsplit(url).query) == {"customer": ["3"], "err": ["Tom & Jerry"]}, url)
    url = fe.error_url("/glossaries/7", "candidate_not_pending", anchor="candidates")
    check(url == "/glossaries/7?err=candidate_not_pending#candidates", url)
    check(
        fe.error_url("/vendors", "vendor_name_required") == "/vendors?err=vendor_name_required",
        "wire",
    )
    url = fe.error_url("/x", "databricks_setup_failed", param="error")
    check(url == "/x?error=databricks_setup_failed", url)
    url = fe.error_url("/settings/enterprise", "a#b", anchor="feature-s3", param="error")
    check(_roundtrip(url, "error") == ("/settings/enterprise", "a#b", "feature-s3"), url)
    for evil in (
        "//evil.example/x",
        "https://evil.example/",
        "/\\evil.example",
        "evil",
        "",
        "/a\nb",
    ):
        url = fe.error_url(evil, "x y")
        check(url.startswith("/?err="), (evil, url))
    r = fe.redirect_with_error("/routing", "Department is required.", param="error")
    check(r.status_code == 303, r.status_code)
    check(r.headers["location"] == "/routing?error=Department%20is%20required.", r.headers)
    print(
        "  2. redirect helper: #, &, %, +, spaces round-trip; & joins; anchors; no open redirect OK"
    )


# --------------------------------------------------------------------------- #
# seed + client
# --------------------------------------------------------------------------- #
def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )  # pregate: allow-secret throwaway fixture login
    if r.status_code == 303:
        csrf_for(c)
    return r


def _seed_project() -> tuple[int, int]:
    with Session(engine) as s:
        cust = Customer(name="Form Errors Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(name="Form Errors Proj", source_locale="en-US", customer_id=cust.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id, cust.id


# --------------------------------------------------------------------------- #
# 3 + 4. rendered summary; project page never prints a raw code
# --------------------------------------------------------------------------- #
_PROJECT_CODES = (
    "format",
    "parse",
    "toolarge_doc",
    "export",
    "export_held",
    "export_quarantined",
    "langset",
    "status",
    "tmx",
    "tmx_locales",
    "toolarge_tmx",
    "tm_source",
    "glossary",
    "toolarge_glossary",
    "glossary_add",
    "glossary_pin",
)


def test_summary_and_project(c, pid):
    r = c.get(f"/projects/{pid}?err=format")
    check(r.status_code == 200, r.status_code)
    html = r.text
    box = elem(html, "page-error")
    check(box.get("role") == "alert" and box.get("tabindex") == "-1", box)
    check("error-summary" in box.get("class", ""), box)
    check(summary_text(html) == en("errors.format"), summary_text(html))
    check(title(html).startswith(en("errors.title_prefix") + " "), title(html))
    check('<a href="#doc-upload-file">' in html, "go-to-field link")
    field = elem(html, "doc-upload-file")
    check(field.get("aria-invalid") == "true", field)
    check("doc-upload-file-error" in field.get("aria-describedby", "").split(), field)
    check('id="doc-upload-file-error"' in html, "inline message id")
    # summary is the FIRST child of <main>
    main = html[html.index('id="main-content"') :]
    check(
        main[main.index(">") + 1 :].lstrip().startswith('<div class="error error-summary"'), "order"
    )
    check("/static/form_errors.js" in html, "form_errors.js loaded by base.html")

    clean = c.get(f"/projects/{pid}").text
    check('id="page-error"' not in clean and not title(clean).startswith("Error:"), "no error")
    check("aria-invalid" not in elem(clean, "doc-upload-file"), "clean field")

    clean_texts = Counter(parse(clean).texts)
    # Gate fix-auth-easy round 1 LOW-2: an unknown code from a link is not shown at all
    # (not even as the generic sentence's reference) unless the server redirected this
    # session there; record it to exercise the resolver's generic sentence on the page.
    _allow(c, "some_unknown_code")
    for code in (*_PROJECT_CODES, "some_unknown_code"):
        html = c.get(f"/projects/{pid}?err={code}").text
        # Text the error ADDED to the page (a word such as "glossary" is page copy anyway).
        added = list((Counter(parse(html).texts) - clean_texts).elements())
        # the generic sentence names an unknown code as its reference, on purpose
        added = [t for t in added if not t.startswith("Something went wrong (reference:")]
        check(code not in added, f"project page prints the raw code {code!r}: {added}")
        if "_" in code:
            check(not any(code in t for t in added), f"raw code {code!r} inside page text")
        want = (
            en(fe.ERRORS[code].key)
            if code in fe.ERRORS
            else en("errors.generic", ref="some_unknown_code")
        )
        check(summary_text(html) == want.replace("'", "&#39;"), (code, summary_text(html)))
        check(html.count('id="page-error"') == 1, f"{code}: one summary")
        spec = fe.ERRORS.get(code)
        if spec and spec.field and f'id="{spec.field}"' in html:
            attrs = elem(html, spec.field)
            check(attrs.get("aria-invalid") == "true", (code, attrs))

    # The lifecycle error's inline message is referenced by the status controls (the
    # buttons of the Edit status form), and that form is shown open.
    html = c.get(f"/projects/{pid}?err=status").text
    check('id="project-lifecycle-error"' in html, "lifecycle inline message")
    status_btns = [
        a for tag, a in parse(html).elems if tag == "button" and a.get("name") == "status"
    ]
    check(status_btns, "status buttons rendered for an admin")
    for a in status_btns:
        check("project-lifecycle-error" in a.get("aria-describedby", "").split(), a)
    check(
        re.search(r'<details class="inline-edit"[^>]* open>\s*<summary[^>]*>Edit status', html),
        "the Edit status form is open while its error shows",
    )
    clean_btns = [
        a for tag, a in parse(clean).elems if tag == "button" and a.get("name") == "status"
    ]
    check(clean_btns and all("aria-describedby" not in a for a in clean_btns), clean_btns)

    # free text: escaped, inert, capped, shown once
    payload = _allow(c, "<script>alert(1)</script> & more")
    html = c.get(f"/projects/{pid}", params={"err": payload}).text
    check("<script>alert(1)</script>" not in html, "script in ?err= must be inert")
    check(
        summary_text(html) == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more", summary_text(html)
    )
    check(html.count("alert(1)") == 1, "free text renders once (the catch-all is gone)")
    html = c.get(f"/projects/{pid}", params={"err": _allow(c, "x" * 1000)}).text
    check(len(summary_text(html)) <= fe.MAX_TEXT, len(summary_text(html)))
    print("  3. summary: role=alert, tabindex=-1, first in main, Error: title, field aria OK")
    print(f"  4. project page: {len(_PROJECT_CODES)} codes + an unknown one never print raw OK")


# --------------------------------------------------------------------------- #
# 5. login regression
# --------------------------------------------------------------------------- #
def test_login_regression():
    settings.set_many({"saml_enabled": "1", "sso_enabled": "1", "sso_provider": "okta"})
    try:
        c = TestClient(app)
        normal = c.get("/auth/login").text
        check(
            'href="/auth/saml/login"' in normal and 'href="/auth/oidc/login?' in normal,
            "GET affordances",
        )
        check('action="/auth/sso"' not in normal, "OIDC must navigate without a form submission")
        r = _login(c, "admin", _WRONG_PW)
        check(r.status_code == 401, r.status_code)
        html = r.text
        check("/auth/saml/login" in html, "SAML button must survive one failed password")
        check('href="/auth/oidc/login?' in html, "SSO link must survive one failed password")
        check('action="/auth/sso"' not in html, "failed login must not restore the OIDC POST form")
        user = elem(html, "login-username")
        check(user.get("value") == "admin", ("username must be echoed", user))
        check(_WRONG_PW not in html, "the password must never be echoed")
        pw = elem(html, "login-password")
        check("value" not in pw, pw)
        for attrs in (user, pw):
            check(attrs.get("aria-invalid") == "true", attrs)
            check("page-error-msg" in attrs.get("aria-describedby", ""), attrs)
        box = elem(html, "page-error")
        check(box.get("role") == "alert" and box.get("tabindex") == "-1", box)
        check(summary_text(html) == en("errors.login_invalid"), summary_text(html))
        check(title(html).startswith("Error: "), title(html))
        check("/static/form_errors.js" in html, "login loads form_errors.js (focus on load)")
        clean = c.get("/auth/login").text
        check('id="page-error"' not in clean and "aria-invalid" not in clean, "clean login")
        # the username echo is autoescaped
        r = _login(c, '"><script>x()</script>', _WRONG_PW)
        check("<script>x()</script>" not in r.text, "echoed username must be escaped")
    finally:
        settings.set_many({"saml_enabled": "0", "sso_enabled": "0"})
    print("  5. login: SAML + SSO buttons kept, username echoed, password never, fields invalid OK")


# --------------------------------------------------------------------------- #
# 6. other core pages
# --------------------------------------------------------------------------- #
def test_core_pages(c, cid):
    from app.kernel import glossary_module as gm

    with Session(engine) as s:
        g = gm.create(s, "Form Errors Glossary", customer_id=cid)
        gid = g.id
    r = c.post(f"/glossaries/{gid}/terms/add", data={"source_term": "   "}, follow_redirects=False)
    check(r.status_code == 303, r.status_code)
    check(r.headers["location"] == f"/glossaries/{gid}?err=blank_term", r.headers["location"])
    html = c.get(r.headers["location"]).text
    check(summary_text(html) == en("errors.blank_term"), summary_text(html))
    check(elem(html, "glossary-add-term").get("aria-invalid") == "true", "glossary field")
    check("Could not complete:" not in html and "blank_term" not in parse(html).texts, "raw")

    # "Go to the section" for candidate_not_pending targets a real, focusable section
    # heading, not an empty <a id> that cannot take focus.
    html = c.get(f"/glossaries/{gid}?err=candidate_not_pending").text
    check('<a href="#candidates">' in html, "go-to-section link")
    tags = [tag for tag, a in parse(html).elems if a.get("id") == "candidates"]
    check(tags == ["h2"], f"#candidates must be the section heading, got {tags}")

    html = c.get("/account?saved=pwfail").text
    check(summary_text(html).startswith("Your password was not changed."), summary_text(html))
    check(elem(html, "account-current-password").get("aria-invalid") == "true", "account field")
    check(html.count(summary_text(html)) == 1, "account error renders once")
    check('id="page-error"' not in c.get("/account?saved=name").text, "a success is no error")

    srx = _allow(c, "SRX file too large (max 1MB).", "srx_error")
    html = c.get("/settings", params={"srx_error": srx}).text
    check(summary_text(html) == "SRX file too large (max 1MB).", summary_text(html))
    check(html.count("SRX file too large") == 1, "settings srx error renders once")

    html = c.get("/security", params={"err": _allow(c, "LDAP needs ldap3 # & 100%")}).text
    check(summary_text(html) == "LDAP needs ldap3 # &amp; 100%", summary_text(html))
    check(html.count("LDAP needs ldap3") == 1, "security error renders once")

    _allow(c, "Plain sentence here.")
    for path in ("/glossaries", "/content-types", "/users"):
        html = c.get(path, params={"err": "Plain sentence here."}).text
        check(summary_text(html) == "Plain sentence here.", (path, summary_text(html)))
        check(html.count("Plain sentence here.") == 1, f"{path}: renders once")
    html = c.get("/users", params={"error": _allow(c, "No such customer.", "error")}).text
    check(html.count("No such customer.") == 1 and 'id="page-error"' in html, "users ?error=")

    # a real route that used to put unquoted text in the URL now round-trips it
    r = c.post("/routing/budgets", data={"department": ""}, follow_redirects=False)
    check(r.status_code == 303, r.status_code)
    loc = r.headers["location"]
    check(loc == "/routing?error=Department%20is%20required.", loc)
    check(parse_qs(urlsplit(loc).query)["error"] == ["Department is required."], loc)
    # #248: the redirect was recorded for THIS session, so the page shows the sentence.
    check(summary_text(c.get(loc).text) == "Department is required.", "recorded redirect")
    print("  6. core pages: glossary detail, account, settings, security, lists, users OK")


# --------------------------------------------------------------------------- #
# 8. T7 pages: the rest of the product renders its errors through the one summary
# --------------------------------------------------------------------------- #
_CLASS_NAME = re.compile(r"\b[A-Z][A-Za-z]+(?:Error|Exception)\b")


def _t7_page(html: str, want: str, what: str, *, field: str | None = None) -> None:
    """One T7 page with an error: the role=alert summary carries the sentence exactly once,
    no raw code or Python exception class name is visible, and (when the error belongs to
    a field) the field is aria-invalid, described by the summary sentence, and linked."""
    box = elem(html, "page-error")
    check(box.get("role") == "alert" and box.get("tabindex") == "-1", (what, box))
    check(summary_text(html) == want, (what, summary_text(html), want))
    texts = parse(html).texts
    check(sum(t.count(want) for t in texts) == 1, f"{what}: the sentence must render once")
    check("Could not complete:" not in html, f"{what}: the old raw banner prefix is back")
    check(not any(_CLASS_NAME.search(t) for t in texts), f"{what}: exception class name shown")
    check(title(html).startswith(en("errors.title_prefix") + " "), (what, title(html)))
    if field:
        attrs = elem(html, field)
        check(attrs.get("aria-invalid") == "true", (what, field, attrs))
        check("page-error-msg" in attrs.get("aria-describedby", "").split(), (what, attrs))
        check(f'<a href="#{field}">' in html, f"{what}: summary must link to #{field}")


def _loc(r) -> str:
    check(r.status_code == 303, (r.status_code, r.text[:200]))
    return r.headers["location"]


def test_t7_pages(c, pid, cid):
    from datetime import datetime, timedelta

    from app.models import RunStatus, WorkflowRun

    settings.set_many(
        {
            "data_export_enabled": "1",
        }
    )
    settings.invalidate_cache()

    # Vendors: the required name is a field error (code + field); free text from the
    # service still renders once as a sentence.
    loc = _loc(c.post("/vendors", data={"name": "  "}, follow_redirects=False))
    check(loc == "/vendors?err=vendor_name_required", loc)
    _t7_page(
        c.get(loc).text, en("errors.vendor_name_required"), "vendors", field="vendor-create-name"
    )
    archived = _allow(c, "That vendor is archived. Unarchive it first.")
    html = c.get("/vendors", params={"err": archived}).text
    _t7_page(html, "That vendor is archived. Unarchive it first.", "vendors free text")

    # Data export: bad scope links to the scope section, a missing id marks the id select.
    loc = _loc(c.post("/exports", data={"scope": "galaxy"}, follow_redirects=False))
    check(loc == "/exports?error=bad_scope", loc)
    html = c.get(loc).text
    _t7_page(html, en("errors.bad_scope"), "exports scope")
    check('<a href="#export-scope">' in html, "exports: section link")
    loc = _loc(c.post("/exports", data={"scope": "customer", "ref": ""}, follow_redirects=False))
    check(loc == "/exports?error=scope_needs_id", loc)
    _t7_page(c.get(loc).text, en("errors.scope_needs_id"), "exports id", field="export-ref")

    # Run view: a refused hand-off return lands here as free text; it renders once in the
    # summary (the old "Could not complete:" banner is gone).
    doc = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("t7.txt", b"Hello there.\n", "text/plain")},
        follow_redirects=False,
    )
    did = int(_loc(doc).split("?")[0].rstrip("/").split("/")[-1])
    wid = int(
        _loc(c.post("/workflows", data={"name": "T7 flow"}, follow_redirects=False))
        .split("?")[0]
        .rstrip("/")
        .split("/")[-1]
    )
    with Session(engine) as s:
        now = datetime(2026, 9, 24, 12)
        run = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale="fr-FR",
            status=RunStatus.FAILED,
            started_at=now,
            finished_at=now + timedelta(seconds=5),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
    msg = "That file was never sent for this leg. Upload the file that was sent, then try again."
    _allow(c, msg)
    _t7_page(c.get(f"/runs/{rid}", params={"err": msg}).text, msg, "run view")

    # Setup AI: a spend-cap refusal marks and links the spend cap field.
    # T11 (HECVAT APPL-09): negative and non-numeric values are refused the same way.
    for bad in ("inf", "-5", "abc"):
        loc = _loc(c.post("/setup-ai/spend", data={"spend_cap_usd": bad}, follow_redirects=False))
        check(loc.startswith("/setup-ai?spend_error="), loc)
        want = (
            "The spend cap must be an amount in US dollars, 0 or more, such as 50 or 42.50. "
            "Leave it blank or enter 0 for no cap."
        )
        _t7_page(c.get(loc).text, want, "setup ai", field="spend-cap-input")

    # Reports: a bad date re-renders with the date fields marked.
    html = c.get("/reports/run", params={"kind": "cost", "from": "yesterday-ish"}).text
    want = "A date is not valid. Enter dates as YYYY-MM-DD, then run the report again."
    _t7_page(html, want, "reports", field="report-from")
    check(elem(html, "report-to").get("aria-invalid") == "true", "reports: both dates")

    # Project delete: a typed name that does not match is a field error.
    loc = _loc(
        c.post(f"/projects/{pid}/delete", data={"confirm_name": "nope"}, follow_redirects=False)
    )
    check(loc == f"/projects/{pid}/delete?err=delete_name_mismatch", loc)
    _t7_page(
        c.get(loc).text,
        en("errors.delete_name_mismatch"),
        "project delete",
        field="project-delete-confirm",
    )

    # New project dialog: the error shows in main AND in the dialog (no-JS users see the
    # main one); both are alerts, only the dialog's is focused by its script.
    html = c.get("/projects", params={"err": _allow(c, "Pick a customer for the project.")}).text
    check(summary_text(html) == "Pick a customer for the project.", summary_text(html))
    np_box = elem(html, "np-error")
    check(np_box.get("role") == "alert" and np_box.get("tabindex") == "-1", np_box)
    check('<p id="np-error-msg">Pick a customer for the project.</p>' in html, "dialog copy")
    check("focusDialogError()" in html, "dialog focuses its own summary")
    check('id="np-error"' not in c.get("/glossaries", params={"err": "x y"}).text, "np only")
    print("  8. T7 pages: vendors, exports, run view,")
    print("     setup ai, reports, project delete, new project dialog OK")


# --------------------------------------------------------------------------- #
# 9. security review 2026-09 (#248, #270): free text in the query string
# --------------------------------------------------------------------------- #
_PHISH = "Your password expired. Reset it at https://evil.example/reset or call 555 0100"


def test_query_guard(c, pid, cid):
    # (a) The sign-in page ignores free text and unknown codes (the a11y T6 guarantee the
    # TOTP train reopened); known codes (mfa_expired) still resolve there.
    anon = TestClient(app)
    for params in (
        {"err": _PHISH},
        {"error": _PHISH},
        {"err": "call_the_it_desk_on_555_0100"},
        {"err": _PHISH, "error": _PHISH},
    ):
        html = anon.get("/auth/login", params=params).text
        check(
            "evil.example" not in html and "555" not in message_dom(html), ("login echoed", params)
        )
        check('id="page-error"' not in html and not title(html).startswith("Error:"), params)
    html = anon.get("/auth/login", params={"err": "mfa_expired"}).text
    check(summary_text(html) == en("errors.mfa_expired"), summary_text(html))
    # even a value this session HAS been sent to is not shown on an /auth page
    _allow(anon, _PHISH)
    check("evil.example" not in anon.get("/auth/login", params={"err": _PHISH}).text, "auth")

    # (b) The wall quotes the path into next=, so an encoded & cannot add ?err= to the
    # sign-in page URL (r1-identity-03 A2/A3, r1-webui-07).
    r = anon.get("/projects%26err%3DSession%20ended.%20Call%20555", follow_redirects=False)
    check(r.status_code == 303, r.status_code)
    loc = r.headers["location"]
    check(loc.startswith("/auth/login?next=/projects%26err%3DSession"), loc)
    check(set(parse_qs(urlsplit(loc).query)) == {"next"}, loc)
    html = anon.get(loc).text  # (the hidden next= field keeps the path, as a value only)
    check('id="page-error"' not in html, "injected err shown after the wall")
    check(not any("Session ended" in t for t in parse(html).texts), "injected text visible")

    # (c) Signed in: an unrecorded free-text message renders nowhere (r1-webui-03), on
    # ?err= / ?error= and the other message parameters (#270 ?notice= / ?msg=).
    for path in ("/", f"/projects/{pid}", "/account", "/help", "/glossaries", "/my-work"):
        for name in ("err", "error"):
            html = c.get(path, params={name: _PHISH}).text
            check("evil.example" not in html, (path, name, "echoed"))
            check('id="page-error"' not in html, (path, name, "summary shown"))
    wid = int(
        _loc(c.post("/workflows", data={"name": "Guard flow"}, follow_redirects=False))
        .split("?")[0]
        .rstrip("/")
        .split("/")[-1]
    )
    html = c.get(f"/workflows/{wid}", params={"notice": _PHISH}).text
    check("evil.example" not in html, "workflow ?notice= echoed")
    from app.web import page_guard

    raw = f"msg={_PHISH}&ok=1&notice=Hi%20there&err=format&saved=1".encode()
    kept = page_guard.filter_query(raw, "/connectors/zendesk", {})
    check(kept == b"ok=1&err=format&saved=1", kept)
    check(page_guard.filter_query(b"err=format&x=%E9", "/p", {}) == b"err=format&x=%E9", "bytes")

    # (d) A recorded message belongs to ONE session: another signed-in client following
    # the same Location sees nothing.
    r = c.post("/routing/budgets", data={"department": ""}, follow_redirects=False)
    loc = _loc(r)
    other = TestClient(app)
    check(_login(other, "admin", _SEED_PW).status_code == 303, "second session")
    check('id="page-error"' not in other.get(loc).text, "a recorded message crossed sessions")
    check(summary_text(c.get(loc).text) == "Department is required.", "own session")

    # (e) The connector OAuth callback (a GET anyone can link to) no longer reflects the
    # provider's ?error= text, and its message is percent-encoded (no &ok=1 smuggling).
    r = c.get(
        "/connectors/oauth/callback",
        params={"error": "Your token expired, re-enter it at evil.example&ok=1"},
        follow_redirects=False,
    )
    loc = _loc(r)
    check("evil.example" not in loc and "&ok=" not in loc, loc)
    check(
        parse_qs(urlsplit(loc).query)
        == {"msg": ["OAuth was cancelled or failed: not a standard error code"]},
        loc,
    )
    # (f) Gate fix-auth-easy round 1 MEDIUM-1: a real sign-in must not launder a message
    # carried in the caller-supplied next= (password, MFA, SAML, password change and the
    # expired return all share oidc._sanitize_next).
    from app.kernel import oidc, saml

    phish_next = f"/projects/{pid}?tab=tm&err=" + quote(_PHISH) + "&notice=Hi%20there#x"
    check(oidc._sanitize_next(phish_next) == f"/projects/{pid}?tab=tm#x", "sanitizer")
    check(saml.sanitize_next(phish_next) == f"/projects/{pid}?tab=tm#x", "saml shares it")
    check(oidc._sanitize_next("/p?err=a&key_error=b&srx_msg=c") == "/p", "all message params")
    # Security review 2026-10 web-01: browsers drop TAB/CR/LF inside a URL, so
    # '/<TAB>/evil.example' is '//evil.example' once rendered into a link. Any control
    # character gives the safe default, on every sanitizer that shares the test.
    from types import SimpleNamespace

    from app.web import content_types as _ct_web
    from app.web import vendors as _vendors_web

    for bad in (
        "/\t/evil.example/x",
        "/\n/evil.example/x",
        "/\r/evil.example/x",
        "\t//evil.example",
        "/\x0b/evil.example",
        "/\x7f/evil.example",
        "/ok\x00",
        "/\\evil.example",
        "//evil.example",
        "https://evil.example",
    ):
        check(oidc._sanitize_next(bad) == "/", ("control/unsafe next kept", bad))
        check(saml.sanitize_next(bad) == "/", ("saml kept", bad))
        check(_vendors_web._safe_next(bad, "/vendors/1") == "/vendors/1", ("vendor next", bad))
        req = SimpleNamespace(query_params={"back": bad})
        check(_ct_web._back(req) == "/content-types", ("content-type back", bad))
    check(oidc._sanitize_next("/%09/x") == "/%09/x", "an encoded tab stays a path")
    check(_vendors_web._safe_next("/vendors/2#h", "/v") == "/vendors/2#h", "vendor next ok")
    req = SimpleNamespace(query_params={"back": "/customers/3"})
    check(_ct_web._back(req) == "/customers/3", "content-type back ok")
    victim = TestClient(app)
    page = victim.get("/auth/login", params={"next": phish_next}).text
    check("evil.example" not in page and "err=" not in page, "sign-in page kept the message")
    r = _login_next(victim, phish_next)
    check(r.headers["location"] == f"/projects/{pid}?tab=tm#x", r.headers["location"])
    check(not session_data(victim).get("qm"), "a sign-in redirect was recorded")
    check("evil.example" not in victim.get(f"/projects/{pid}", params={"err": _PHISH}).text, "P1")
    # ... and the guard alone refuses to record what an /auth POST redirects to, even if a
    # future sanitizer let a message through.
    real = oidc._sanitize_next
    oidc._sanitize_next = lambda v: v if str(v or "").startswith("/") else "/"
    try:
        victim2 = TestClient(app)
        r = _login_next(victim2, f"/projects/{pid}?err=" + quote(_PHISH))
        check("err=" in r.headers["location"], r.headers["location"])
        check(not session_data(victim2).get("qm"), "guard recorded an /auth redirect")
        check("evil.example" not in victim2.get(r.headers["location"]).text, "guard P1")
    finally:
        oidc._sanitize_next = real
    # the expired / stale-token form return (Referer path + query) never keeps a message
    r = c.post(
        "/projects",
        data={"name": "x"},
        headers={"Referer": f"http://testserver/projects/{pid}?err=" + quote(_PHISH)},
        follow_redirects=False,
    )
    check(r.status_code in (303, 403) and "evil" not in r.headers.get("location", ""), r.headers)

    # (g) LOW-1: counts render as numbers; locale_removed / jpv are recorded-only messages.
    for path, params in (
        (f"/projects/{pid}", {"locale_removed": _PHISH}),
        (f"/projects/{pid}", {"langs_added": _PHISH}),
        (f"/projects/{pid}", {"tm_imported": _PHISH}),
    ):
        check("evil.example" not in c.get(path, params=params).text, (path, params))
    # (h) LOW-2: short code-shaped values from a link print nowhere.
    for path, params in (
        ("/workflows", {"notice": "call_555_0100_to_restore_access"}),
        ("/workflows", {"msg": "call_555_0100_to_restore_access"}),
        (f"/projects/{pid}", {"err": "reset-at:evil.example"}),
    ):
        html = c.get(path, params=params).text
        check("call_555" not in html and "evil.example" not in html, (path, params))
    # (i) Security review 2026-10 web-02 (the rest of #270): success banners that print the
    # query value itself. Unrecorded free text renders nowhere; the real POST redirect still
    # shows its banner; the same names stay plain flags on other pages.
    spoof_cases = [
        ("/users", "invited"),
        ("/users", "revoked"),
        ("/users", "deleted"),
        ("/users", "erased"),
        ("/settings/enterprise", "saved"),
    ]
    for path, name in spoof_cases:
        params = {name: _PHISH}
        r = c.get(path, params=params)
        check(r.status_code == 200, (path, name, r.status_code))
        check("evil.example" not in r.text, (path, name, "free text shown in a banner"))
    tok = security.create_invite("guard-revoke@example.test", "translator", "admin")
    inv_id = getattr(tok, "id", None)
    check(inv_id, "invite id")
    r = c.post(f"/users/invite/{inv_id}/revoke", follow_redirects=False)
    loc = _loc(r)
    check(loc.startswith("/users?revoked="), loc)
    html = c.get(loc).text
    check("Invitation revoked for <strong>guard-revoke@example.test</strong>" in html, "revoked")
    raw = b"saved=" + quote(_PHISH).encode() + b"&deleted=1"
    check(page_guard.filter_query(raw, "/projects/1/keys", {}) == raw, "flags elsewhere kept")
    # Gate i430: the key grid prints ?ok= verbatim in a green banner (keys_common._back).
    raw = b"ok=" + quote(_PHISH).encode() + b"&q=x"
    check(page_guard.filter_query(raw, "/projects/1/keys", {}) == b"q=x", "keys ok= dropped")
    sess = {}
    page_guard._record_url(sess, "/projects/1/keys?ok=" + quote(_PHISH))
    check(page_guard.filter_query(raw, "/projects/1/keys", sess) == raw, "recorded keys ok= kept")
    check(page_guard.filter_query(b"ok=1", "/connectors/connection/1", {}) == b"ok=1", "ok flag")
    check(page_guard.filter_query(b"ok=1", "/projects/1/keys/x", {}) == b"ok=1", "keys sub-path")
    with Session(engine) as s:
        kp = Project(
            name="Form Errors Keys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cid,
            project_type="key_based",
        )
        s.add(kp)
        s.commit()
        s.refresh(kp)
        kpid = kp.id
    r = c.get(f"/projects/{kpid}/keys", params={"ok": _PHISH})
    check(r.status_code == 200, ("key grid", r.status_code))
    check("evil.example" not in r.text, "keys ok= free text shown in a banner")
    r = c.post(f"/projects/{kpid}/keys/autounverify", data={"on": "1"}, follow_redirects=False)
    loc = _loc(r)
    check(loc.startswith(f"/projects/{kpid}/keys?ok="), loc)
    check('<p class="ok">Saved auto-unverify setting.</p>' in c.get(loc).text, "real keys flash")
    print("  9i. banner params (users, enterprise, keys)")
    print("  9. query guard: sign-in codes only, quoted wall next=, unrecorded text dropped,")
    print("     recorded text per session, OAuth callback no longer reflects ?error= OK")


def test_setup_and_accept():
    c = TestClient(app)
    tok = security.create_invite("invitee@example.test", "translator", "admin")
    token = getattr(tok, "token", tok)
    csrf_for(c)
    r = c.post(
        f"/auth/accept/{token}",
        data={"name": "Ivy", "password": _PW_A, "confirm": _PW_B},
    )
    check(r.status_code == 400, r.status_code)
    html = r.text
    check(elem(html, "accept-confirm").get("aria-invalid") == "true", "confirm marked")
    check("aria-invalid" not in elem(html, "accept-password"), "password not marked")
    check(elem(html, "accept-name").get("value") == "Ivy", "name echoed")
    check(_PW_A not in html and _PW_B not in html, "no password echo")
    check(title(html).startswith("Error: ") and 'role="alert"' in html, title(html))
    check("/static/form_errors.js" in html, "accept loads form_errors.js")
    # 3.1.1: the accept page's copy is hardcoded English, so it declares en even when the
    # UI locale is de (the errors.* sentences fall back to en as well).
    settings.set_many({"default_ui_locale": "de"})
    try:
        html = TestClient(app).get(f"/auth/accept/{token}").text
        check('<html lang="en">' in html, "accept page must declare en under a de UI locale")
    finally:
        settings.set_many({"default_ui_locale": "en"})
    print("  7. accept: mismatch marks confirm, echoes name, never passwords, lang en OK")


def main():
    test_message_dom()
    test_resolver()
    test_redirect_helper()
    init_db()
    check(security.seed_initial() is True, "seed")
    pid, cid = _seed_project()
    c = TestClient(app)
    check(_login(c, "admin", _SEED_PW).status_code == 303, "admin login")
    test_summary_and_project(c, pid)
    test_core_pages(c, cid)
    test_t7_pages(c, pid, cid)
    test_query_guard(c, pid, cid)
    test_login_regression()
    test_setup_and_accept()
    print(
        "collected ids: resolver redirect_helper summary project_page login core_pages "
        "t7_pages query_guard accept"
    )
    print("FORM ERRORS OK")


if __name__ == "__main__":
    main()
