"""#478 (apiauth-07): text copied from hidden projects stays hidden -- hermetic.

THE BUG: a customer-level object carries sentences copied from documents, and their read
paths did not check that the caller can see the source project:
  * glossary term candidates (`context_snippet`, `source_document_id`) on
    GET /api/glossaries/{gid}/candidates (MCP glossary tools) and the web glossary page;
So a translator who is not on a project read its sentences.
(The Lab dataset case was an Evaluations Lab check; that paid feature is not in the open core.)

Pinned (auth ON, project_scoping ON):
  1. /api candidates: a non-member gets the term but a blank snippet and no document id;
     a member gets both.
  2. Web glossary page: the non-member page has no snippet; the member page has it.

  4. (#541) accepting a candidate (API, web, web batch) as a customer-bound linguist who
     cannot see the source project (it sits under another customer) keeps the term but copies
     NO snippet into the entry's usage example; a see-all linguist still gets it.
  5. (#541) the extraction picker and `visible_document` never offer or accept a document in
     a project the caller cannot see (service level: the same calls the routes make).

Final line: "HIDDEN SOURCE SNIPPETS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_hiddensnip_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Glossary,
    GlossaryEntry,
    Project,
    ProjectMember,
    Role,
    TermCandidate,
    User,
)
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

init_db()

_PW = "hiddensnip-fixture-pass-1"
_SNIP = "SM_HIDDEN_SNIPPET from secret doc"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _seed():
    with Session(engine) as s:
        cust = Customer(name="Cust A")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = Project(name="secret", customer_id=cust.id, created_by="admin@x.com")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(project_id=proj.id, filename="secret.txt", fmt="txt")
        s.add(doc)
        g = Glossary(name="Shared glossary", customer_id=cust.id)
        s.add(g)
        s.commit()
        s.refresh(doc)
        s.refresh(g)
        s.add(
            TermCandidate(
                glossary_id=g.id,
                source_term="SM_TERM",
                context_snippet=_SNIP,
                source_document_id=doc.id,
                source_locale="en-US",
                target_locale="fr-FR",
            )
        )
        cust_b = Customer(name="Cust B")
        s.add(cust_b)
        s.commit()
        s.refresh(cust_b)
        proj_b = Project(name="elsewhere", customer_id=cust_b.id, created_by="admin@x.com")
        s.add(proj_b)
        s.commit()
        s.refresh(proj_b)
        doc_b = Document(project_id=proj_b.id, filename="elsewhere.txt", fmt="txt")
        s.add(doc_b)
        s.commit()
        s.refresh(doc_b)
        for n in range(2, 8):
            s.add(
                TermCandidate(
                    glossary_id=g.id,
                    source_term=f"SM_TERM{n}",
                    proposed_target=f"SM_TGT{n}",
                    context_snippet=_SNIP,
                    source_document_id=doc_b.id,
                    source_locale="en-US",
                    target_locale="fr-FR",
                )
            )
        s.commit()
        ids = {"pid": proj.id, "gid": g.id, "cust": cust.id}
    for who in ("outsider", "member", "lingout", "lingmem"):
        role = Role.LINGUIST if who.startswith("ling") else Role.TRANSLATOR
        u = security.create_user(f"{who}@x.com", who, role, _PW)
        ids[who] = apikeys.create_key(u.id, who, scopes="api docs")[1]
        if who == "lingout":
            with Session(engine) as s:
                row = s.get(User, u.id)
                row.customer_id = ids["cust"]
                s.add(row)
                s.commit()
        if who == "member":
            with Session(engine) as s:
                s.add(ProjectMember(project_id=ids["pid"], user_id=u.id))
                s.commit()
    return ids


def _login(c, who):
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": f"{who}@x.com", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    if r.status_code != 303:
        _fail(f"login {who} failed: {r.status_code}")
    csrf_for(c)


def _cands(c, ids, who):
    r = c.get(f"/api/glossaries/{ids['gid']}/candidates", headers={"X-Loc-Token": ids[who]})
    if r.status_code != 200:
        _fail(f"{who} candidates must be 200, got {r.status_code} {r.text[:200]}")
    return r.json()["candidates"]


def _usage_example(term):
    with Session(engine) as s:
        row = s.exec(select(GlossaryEntry).where(GlossaryEntry.source_term == term)).first()
    if row is None:
        _fail(f"{term} was not accepted into the glossary")
    return (row.meta or {}).get("usage_example", "")


def _cand_id(term):
    with Session(engine) as s:
        return s.exec(select(TermCandidate).where(TermCandidate.source_term == term)).first().id


def _check_accept_and_picker(ids, c):
    gid = ids["gid"]
    # API accept: the outsider linguist keeps the term, loses the snippet.
    r = c.post(
        f"/api/glossaries/{gid}/candidates/{_cand_id('SM_TERM2')}/accept",
        headers={"X-Loc-Token": ids["lingout"]},
        json={},
    )
    if r.status_code != 200:
        _fail(f"outsider linguist accept must work, got {r.status_code} {r.text[:200]}")
    if _usage_example("SM_TERM2"):
        _fail("API accept copied a hidden project's snippet into the glossary entry")
    r = c.post(
        f"/api/glossaries/{gid}/candidates/{_cand_id('SM_TERM3')}/accept",
        headers={"X-Loc-Token": ids["lingmem"]},
        json={},
    )
    if r.status_code != 200 or _usage_example("SM_TERM3") != _SNIP:
        _fail(f"a member linguist accept must keep the snippet, got {r.status_code} {r.text[:200]}")
    # Web accept + batch accept as the outsider linguist.
    w = TestClient(app)
    _login(w, "lingout")
    r = w.post(
        f"/glossaries/{gid}/candidates/{_cand_id('SM_TERM4')}/accept", follow_redirects=False
    )
    if r.status_code != 303 or _usage_example("SM_TERM4"):
        _fail(f"web accept leaked the hidden snippet (status {r.status_code})")
    r = w.post(
        f"/glossaries/{gid}/candidates/batch",
        data={
            "action": "accept",
            "candidate_id": [str(_cand_id("SM_TERM5")), str(_cand_id("SM_TERM6"))],
        },
        follow_redirects=False,
    )
    if r.status_code != 303:
        _fail(f"web batch accept failed: {r.status_code}")
    for term in ("SM_TERM5", "SM_TERM6"):
        if _usage_example(term):
            _fail(f"web batch accept leaked the hidden snippet for {term}")
    m = TestClient(app)
    _login(m, "lingmem")
    m.post(f"/glossaries/{gid}/candidates/{_cand_id('SM_TERM7')}/accept", follow_redirects=False)
    if _usage_example("SM_TERM7") != _SNIP:
        _fail("a member linguist web accept must keep the snippet")
    print("  4. accept/batch: hidden snippet dropped for the outsider, kept for the member: OK")

    from app.services import term_extraction as tex

    with Session(engine) as s:
        g = s.get(Glossary, gid)
        doc_a = s.exec(select(Document).where(Document.filename == "secret.txt")).first()
        out_user = s.exec(select(User).where(User.email == "outsider@x.com")).first()
        mem_user = s.exec(select(User).where(User.email == "member@x.com")).first()
        names = lambda user: {  # noqa: E731
            d["filename"] for d in tex.documents_for_glossary(s, g, user=user)
        }
        if "secret.txt" in names(out_user):
            _fail("the extraction picker listed a hidden project's document to a non-member")
        if "secret.txt" not in names(mem_user):
            _fail("the extraction picker must list the document to a member")
        if "secret.txt" not in {d["filename"] for d in tex.documents_for_glossary(s, g)}:
            _fail("the operator (CLI) picker must stay unrestricted")
        if tex.visible_document(s, g, doc_a.id, user=out_user) is not None:
            _fail("visible_document returned a hidden project's document to a non-member")
        if tex.visible_document(s, g, doc_a.id, user=mem_user) is None:
            _fail("visible_document must return the document to a member")
        if tex.visible_document(s, g, doc_a.id) is None:
            _fail("visible_document must stay unrestricted for the operator")
    print("  5. extraction picker + visible_document: hidden project's document refused: OK")


def main():
    ids = _seed()
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        out = _cands(c, ids, "outsider")
        if not out or out[0]["source_term"] != "SM_TERM":
            _fail(f"non-member must still see the candidate term, got {out}")
        if out[0]["context_snippet"] or out[0]["source_document_id"] is not None:
            _fail(f"non-member must get no snippet and no document id, got {out[0]}")
        mem = _cands(c, ids, "member")
        if mem[0]["context_snippet"] != _SNIP or mem[0]["source_document_id"] is None:
            _fail(f"member must get the snippet and the document id, got {mem[0]}")
        print("  1. /api candidates: non-member term only, member snippet: OK")

        w, m = TestClient(app), TestClient(app)
        _login(w, "outsider")
        _login(m, "member")
        page = w.get(f"/glossaries/{ids['gid']}")
        if page.status_code != 200 or "SM_TERM" not in page.text:
            _fail(f"non-member glossary page must list the term, got {page.status_code}")
        if _SNIP in page.text:
            _fail("non-member glossary page must not show the hidden snippet")
        if _SNIP not in m.get(f"/glossaries/{ids['gid']}").text:
            _fail("member glossary page must show the snippet")
        print("  2. web glossary page: snippet hidden from the non-member only: OK")

        _check_accept_and_picker(ids, c)
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
        settings.invalidate_cache()
    print("HIDDEN SOURCE SNIPPETS OK")


if __name__ == "__main__":
    main()
