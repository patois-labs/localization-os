"""Every browser form posts a CSRF token (issue #535).

With the login wall ON, app/main.py::_security_audit bounces any mutating, non-/api request
that lacks the session synchronizer token to /auth/login?expired=1. The glossary
concept-editor forms shipped without the token, so with sign-in on the concept editor
silently did nothing.

GROUPS
  [A] static scan: every `<form method="post">` in app/templates includes `_csrf.html` (or a
      csrf_token field) unless it is cross-origin; every JS fetch/XHR POST in app/static and
      app/templates sends X-CSRF-Token.
  [B] live, auth ON: sign in as an admin, render /glossaries/{id} with the
      concept editor on, then submit EVERY post form exactly as the browser would (hidden
      fields only, NO X-CSRF-Token header) and require the request not to be bounced to
      sign-in.

Hermetic + offline. ASCII-only prints. Final line: "verify_form_csrf_tokens OK".
"""

import glob
import os
import re
import sys
from html.parser import HTMLParser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_formcsrf_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Role,
    Scope,
    StepType,
)
from app.services import glossary_concepts as gc
from app.services import glossary_module as gm
from app.services import security, settings
from app.services import workflows as W
from tests._csrf import csrf_value

init_db()

# Forms that deliberately post cross-origin (the signed token is the credential).
CROSS_ORIGIN_FORMS = ()


def _fail(msg):
    raise AssertionError(msg)


def _blank_comments(src):
    """`src` with every Jinja `{# #}` and HTML `<!-- -->` comment replaced by spaces
    (newlines kept, so offsets and line numbers still line up). A token that only
    appears in a comment is therefore not a token."""

    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))

    return re.sub(r"\{#.*?#\}|<!--.*?-->", blank, src, flags=re.S)


def _tag_end(src, start):
    """Index just past the `>` that closes the tag opened at `start`. Quoted attribute
    values and Jinja `{{ }}` / `{% %}` blocks are skipped, so a `>` inside either (an
    `{% if a > b %}` or `title="a>b"`) does not cut the tag short."""
    i, quote = start + 1, None
    while i < len(src):
        two = src[i : i + 2]
        if two in ("{{", "{%"):
            close = "}}" if two == "{{" else "%}"
            j = src.find(close, i + 2)
            if j == -1:
                return len(src)
            i = j + 2
            continue
        ch = src[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    return len(src)


_INCLUDE_CSRF_RE = re.compile(r"\{%-?\s*include\s+[\"']_csrf\.html[\"']\s*-?%\}")


def _has_csrf_field(body):
    """True when the form body (comments already blanked) carries the token for real:
    the `_csrf.html` include, or a hidden `<input>` named csrf_token."""
    if _INCLUDE_CSRF_RE.search(body):
        return True
    for m in re.finditer(r"<input\b", body, re.I):
        tag = body[m.start() : _tag_end(body, m.start())]
        if re.search(r"\bname\s*=\s*[\"']csrf_token[\"']", tag, re.I) and re.search(
            r"\btype\s*=\s*[\"']hidden[\"']", tag, re.I
        ):
            return True
    return False


def scan_post_forms(src):
    """1-based line numbers of every POST form in `src` that lacks a real CSRF field."""
    clean = _blank_comments(src)
    bad = []
    for m in re.finditer(r"<form\b", clean, re.I):
        tag_end = _tag_end(clean, m.start())
        if not re.search(r"method\s*=\s*[\"']?post", clean[m.start() : tag_end], re.I):
            continue
        end = clean.find("</form>", tag_end)
        body = clean[tag_end : end if end != -1 else len(clean)]
        if not _has_csrf_field(body):
            bad.append(src.count(chr(10), 0, m.start()) + 1)
    return bad


def check_scanner_fixtures():
    """The scan must FAIL a form whose token sits only in a comment, and must not be cut
    short by a `>` inside a Jinja block or an attribute value."""
    token = '<input type="hidden" name="csrf_token" value="x">'
    include = '{% include "_csrf.html" %}'
    must_fail = {
        "token in an html comment": f'<form method="post"><!-- {token} --></form>',
        "include in a jinja comment": '<form method="post">{# ' + include + " #}</form>",
        "word csrf_token in text": '<form method="post"><p>no csrf_token here</p></form>',
        "token as a visible text input": (
            '<form method="post"><input type="text" name="csrf_token"></form>'
        ),
        "token field of another form": (
            f'<form method="post"></form><form method="get">{token}</form>'
        ),
        "gt inside jinja in the tag": (
            '<form {% if a > b %}class="x"{% endif %} method="post"><p>x</p></form>'
        ),
    }
    for label, src in must_fail.items():
        if not scan_post_forms(src):
            _fail(f"CSRF scan accepted a bad form ({label})")
    must_pass = {
        "include": f'<form method="post">{include}</form>',
        "hidden input": f'<form method="post">{token}</form>',
        "gt inside jinja in the tag": (
            '<form {% if a > b %}class="x"{% endif %} method="post">' + include + "</form>"
        ),
        "gt inside an attribute": f'<form title="a>b" method="post">{token}</form>',
        "get form without a token": '<form method="get"><input name="q"></form>',
    }
    for label, src in must_pass.items():
        if scan_post_forms(src):
            _fail(f"CSRF scan rejected a good form ({label})")
    print("  [A0] the form scan rejects comment-only tokens and survives '>' in tags OK")


def check_static():
    bad = []
    for path in sorted(glob.glob("app/templates/**/*.html", recursive=True)):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        bad.extend(f"{path}:{line}" for line in scan_post_forms(src))
    if bad:
        _fail("post forms without a CSRF token: " + ", ".join(bad))
    # JS: a non-GET fetch must carry the header (headers object may be built just above).
    js_bad = []
    files = glob.glob("app/static/**/*.js", recursive=True) + glob.glob(
        "app/templates/**/*.html", recursive=True
    )
    for path in files:
        if "axe" in path or path.endswith(".min.js"):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for m in re.finditer(r"fetch\(", src):
            seg = src[m.start() : m.start() + 700]
            nxt = seg.find("fetch(", 6)
            if nxt > 0:
                seg = seg[:nxt]
            # a literal mutating method, OR a method held in a variable (cannot be proven safe
            # statically, so it must carry the header like a literal one)
            if re.search(r"method\s*:\s*['\"`]?\s*(POST|PUT|PATCH|DELETE)", seg, re.I) or re.search(
                r"method\s*:\s*[A-Za-z_$]", seg
            ):
                window = src[max(0, m.start() - 400) : m.start() + 700]
                if not re.search(r"csrf", window, re.I):
                    js_bad.append(f"{path}:{src.count(chr(10), 0, m.start()) + 1}")
    # other browser transports that can mutate: XHR and sendBeacon (none today; a new one
    # must carry the token or be added here with a reason)
    for path in files:
        if "axe" in path or path.endswith(".min.js"):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for m in re.finditer(r"XMLHttpRequest|sendBeacon", src):
            if not re.search(r"csrf", src[max(0, m.start() - 400) : m.start() + 700], re.I):
                js_bad.append(f"{path}:{src.count(chr(10), 0, m.start()) + 1} (xhr/beacon)")
    if js_bad:
        _fail("JS POST fetches without X-CSRF-Token: " + ", ".join(js_bad))
    print("  [A] every post form and JS POST carries the CSRF token OK")


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {
                "action": a.get("action", ""),
                "method": (a.get("method") or "get").lower(),
                "data": {},
            }
            self.forms.append(self._cur)
        elif self._cur is not None and tag in ("input", "select", "textarea"):
            name = a.get("name")
            if not name:
                return
            typ = (a.get("type") or "text").lower()
            if typ in ("checkbox", "radio") and "checked" not in a:
                return
            if typ in ("submit", "button"):
                return
            self._cur["data"][name] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def _forms(html):
    p = _Forms()
    p.feed(html)
    return [f for f in p.forms if f["method"] == "post"]


def _post_all(client, html, prefix, expect_min):
    forms = [f for f in _forms(html) if f["action"].startswith(prefix)]
    if len(forms) < expect_min:
        _fail(f"expected at least {expect_min} post forms under {prefix}, rendered {len(forms)}")
    for f in forms:
        r = client.post(f["action"], data=f["data"], follow_redirects=False)
        loc = r.headers.get("location", "")
        if "/auth/login" in loc or r.status_code == 403:
            _fail(f"form {f['action']} bounced with auth ON: {r.status_code} {loc}")
    return len(forms)


def check_live():
    settings.set_many(
        {
            "terminology_concept_editor_enabled": "1",
            "security_auth_required": "0",
        }
    )
    settings.invalidate_cache()
    with Session(engine) as s:
        cust = Customer(name="Csrf Co")
        s.add(cust)
        s.commit()
        cid = cust.id
        W.create_workflow(s, "csrf-wf", scope=Scope.CUSTOMER, customer_id=str(cid))
        g = gm.create(s, "csrf-glossary")
        gid = g.id
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        gc.create_concept(
            s,
            gid,
            definition="a billing document",
            terms=[
                {"term": "invoice", "locale": "en-US", "status": "preferred"},
                {"term": "factura", "locale": "es-ES", "status": "preferred"},
            ],
        )
    pw = "formcsrf-pw-123456"
    security.create_user("formcsrf@x.com", "Admin", Role.ADMIN, pw)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()

    client = TestClient(app)
    r = client.post(
        "/auth/login",
        data={
            "email": "formcsrf@x.com",
            "password": pw,
            "next": "/",
            "csrf_token": csrf_value(client),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, f"login failed: {r.status_code}"
    assert "x-csrf-token" not in {k.lower() for k in client.headers}, "no default header allowed"

    # glossary concept editor
    page = client.get(f"/glossaries/{gid}")
    assert page.status_code == 200, page.status_code
    n = _post_all(client, page.text, f"/glossaries/{gid}/concepts", 5)
    print(f"  [B] auth ON: {n} concept forms accepted OK")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()


if __name__ == "__main__":
    check_scanner_fixtures()
    check_static()
    check_live()
    _ = StepType
    print("verify_form_csrf_tokens OK")
