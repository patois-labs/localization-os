"""Glossary suggest: a paid AI call moved from GET to a CSRF-guarded POST.

Security review 2026-10, finding webapp-02 (GitHub #388), the sibling of the #283
live-translate fix. `GET /documents/{id}/glossary-suggest` checked project visibility
but neither a write role nor a CSRF token before calling `glossary.build_from_text`,
which fires a paid provider call through `spend.complete_direct` on every hit. So:

  * a read-only viewer MEMBER could spend the configured provider budget (a viewer is
    blocked by the mutating-request middleware's write-role gate only for POST/PUT/
    PATCH/DELETE, never for a GET); and
  * a cross-site page could induce the same action for a signed-in user: a top-level
    link carries the SameSite=Lax session cookie, and the middleware's Origin/CSRF
    checks applied only to mutating methods, so a GET with a foreign Origin succeeded.

Proves, with the login wall ON:
  * the old GET is refused (405) and starts NO provider call;
  * a viewer member POST -> 403 (the write-role gate), no provider call;
  * a translator member POST with a foreign Origin -> 403, no provider call;
  * a translator member POST with no CSRF token -> refused, no provider call;
  * a translator member POST (same origin + CSRF) -> 200 and exactly one provider call;
  * the Document page starts it with a POST form carrying the CSRF include, not a link.

The provider call is counted at the REAL `spend.complete_direct` seam (the exact place
the finding says budget is consumed) while still calling the offline MockProvider; no
guard is mocked or skipped. Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII
output. Final line: GLOSSARY SUGGEST POST OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_glosssug_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Document, ProjectMember, Role, User
from app.services import content, security, settings, spend
from tests._csrf import csrf_for

PW = "glosssug-pw-123456789"
CSRF_HEADER = "X-CSRF-Token"

# Count every paid provider call the route can trigger. `build_from_text` reaches the
# provider through `spend.complete_direct`; instrument that real seam (still calling the
# real offline MockProvider) so a refused request that never reaches the provider is
# proven to spend nothing.
_CALLS = []
_real_complete_direct = spend.complete_direct


def _counting_complete_direct(*args, **kwargs):
    _CALLS.append(kwargs.get("task") or (args[1] if len(args) > 1 else None))
    return _real_complete_direct(*args, **kwargs)


spend.complete_direct = _counting_complete_direct


def _setup():
    init_db()
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    security.create_user("adm@x.com", "adm", Role.ADMIN, PW)
    with Session(engine) as s:
        pid = content.project_create(
            s, "GlossProj", source_locale="en-US", target_locales="de-DE"
        ).id
        doc = Document(
            project_id=pid,
            filename="gloss.txt",
            fmt="txt",
            skeleton={"units": [{"text": "The quarterly financial report ships Tuesday."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return pid, doc.id


def _member(email, role, pid):
    """A signed-in client for a new user who is a member of project `pid`."""
    security.create_user(email, email, role, PW)
    with Session(engine) as s:
        uid = s.exec(select(User).where(User.email == email)).first().id
        s.add(ProjectMember(project_id=pid, user_id=uid))
        s.commit()
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def _suggest(c, doc_id, **kw):
    return c.post(f"/documents/{doc_id}/glossary-suggest", follow_redirects=False, **kw)


def main():
    pid, doc_id = _setup()

    # --- the old GET is dead: 405, and it starts NO provider call -------------------
    tr = _member("tr@x.com", Role.TRANSLATOR, pid)
    before = len(_CALLS)
    r = tr.get(f"/documents/{doc_id}/glossary-suggest", follow_redirects=False)
    assert r.status_code == 405, f"GET glossary-suggest: {r.status_code}"
    assert len(_CALLS) == before, "the dead GET still triggered a provider call"
    print("  #388: GET glossary-suggest -> 405, no provider call")

    # --- a viewer member cannot spend the budget ------------------------------------
    vmember = _member("vmember@x.com", Role.VIEWER, pid)
    before = len(_CALLS)
    r = _suggest(vmember, doc_id)
    assert r.status_code == 403, f"viewer member POST: {r.status_code}"
    r = vmember.get(f"/documents/{doc_id}/glossary-suggest", follow_redirects=False)
    assert r.status_code == 405, f"viewer member GET: {r.status_code}"
    assert len(_CALLS) == before, "a viewer member spent the provider budget"
    print("  #388: viewer member -> 403 (POST) / 405 (GET), no provider call")

    # --- the exact probe: viewer + foreign Origin + no CSRF + a GET -----------------
    before = len(_CALLS)
    r = vmember.get(
        f"/documents/{doc_id}/glossary-suggest",
        headers={"Origin": "https://attacker.invalid"},
        follow_redirects=False,
    )
    assert r.status_code == 405, f"cross-site GET: {r.status_code}"
    assert len(_CALLS) == before, "a cross-site GET spent the provider budget"
    print("  #388: viewer + foreign Origin + no CSRF -> refused, no provider call")

    # --- a cross-site POST cannot start it (foreign Origin) -------------------------
    before = len(_CALLS)
    r = _suggest(tr, doc_id, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403, f"cross-origin POST: {r.status_code}"
    assert len(_CALLS) == before, "a foreign Origin started a provider call"
    print("  #388: foreign-Origin POST -> 403, no provider call")

    # --- a POST without the CSRF token is refused -----------------------------------
    saved_csrf = tr.headers.pop(CSRF_HEADER, None)
    try:
        before = len(_CALLS)
        r = _suggest(tr, doc_id)
        assert r.status_code in (303, 403), f"POST without CSRF: {r.status_code}"
        assert len(_CALLS) == before, "a POST with no CSRF token spent the budget"
    finally:
        if saved_csrf is not None:
            tr.headers.update({CSRF_HEADER: saved_csrf})
    print("  #388: POST without the CSRF token -> refused, no provider call")

    # --- the translator member's same-origin POST works (one provider call) ----------
    before = len(_CALLS)
    r = _suggest(tr, doc_id)
    assert r.status_code == 200, f"translator POST: {r.status_code} {r.text[:200]}"
    assert len(_CALLS) == before + 1, f"translator POST provider calls: {len(_CALLS) - before}"
    print("  #388: translator same-origin POST -> 200, exactly one provider call")

    # --- the Document page starts it with a POST form, never a link -----------------
    with open(os.path.join(_ROOT, "app", "templates", "document.html"), encoding="utf-8") as f:
        page = f.read()
    assert 'href="/documents/{{ doc.id }}/glossary-suggest"' not in page, (
        "the page still links to glossary-suggest with a GET"
    )
    start = page.index('action="/documents/{{ doc.id }}/glossary-suggest"')
    form = page[page.rindex("<form", 0, start) : page.index("</form>", start)]
    assert 'method="post"' in form, "the glossary-suggest start is not a POST form"
    assert "_csrf.html" in form, "the glossary-suggest form omits the CSRF include"
    print("  Document page: POST form + CSRF include, no GET link")

    print("GLOSSARY SUGGEST POST OK")


if __name__ == "__main__":
    main()
