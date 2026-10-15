"""Issue #563: document translate names its language scope, document and project
status follow the real lifecycle, and project counts are labelled (offline, hermetic).

Covers:
  - the document page's translate buttons name the selected locale, each action has a
    one-line hint, and a multi-language document offers "Translate all N languages"
  - POST /documents/{id}/translate with locale=all enqueues one job per locale
  - document status is DERIVED from its segments (new, in translation, in review, done)
    on the web page, GET /api/documents/{id}/segments and `doc inspect`
  - a project with no content does not read IN TRANSLATION, and the project counts say
    "segments x languages"

Final line: "DOC SCOPE STATUS OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_docscope_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Document, Segment, SegStatus
from app.services import lifecycle
from tests._cli_runner import run_cli

init_db()
client = TestClient(app)


def post(path, data, files=None):
    r = client.post(path, data=data, files=files, follow_redirects=False)
    assert r.status_code == 303, (path, r.status_code, r.text[:200])
    return r.headers["location"].split("?")[0].rstrip("/").split("/")[-1]


post("/customers", {"name": "Scope Customer"})
project = post(
    "/projects",
    {
        "name": "Scope project",
        "source_locale": "en-US",
        "target_locales": ["fr-FR", "de-DE", "ja-JP"],
        "default_provider": "mock",
        "customer_id": "1",
    },
)
empty_project = post(
    "/projects",
    {
        "name": "Empty project",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "default_provider": "mock",
        "customer_id": "1",
    },
)
doc_id = int(
    post(
        f"/projects/{project}/documents",
        {"mode": "segmented"},
        {"file": ("scope.txt", b"Hello world.\n\nSecond paragraph here.", "text/plain")},
    )
)


def segs(locale=None):
    with Session(engine) as s:
        q = select(Segment).where(Segment.document_id == doc_id)
        rows = s.exec(q).all()
    return [x for x in rows if locale is None or x.target_locale == locale]


def set_segments(locale, status, text="x"):
    with Session(engine) as s:
        for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if seg.target_locale == locale:
                seg.status = status
                seg.target_text = text if status != SegStatus.NEW else ""
                s.add(seg)
        s.commit()


def status_now():
    with Session(engine) as s:
        return lifecycle.document_status(s, s.get(Document, doc_id))


# --- 1. The translate buttons name their scope and carry a one-line hint -------------
page = client.get(f"/documents/{doc_id}?locale=fr-FR").text
assert 'id="translateBtn"' in page
btn = re.search(r'<button[^>]*id="translateBtn"[^>]*>(.*?)</button>', page, re.S).group(1)
assert "fr-FR" in btn, ("live button must name the selected locale", btn)
assert re.search(r"Translate fr-FR in background", page), "background button names the locale"
assert re.search(r"Apply 100% TM matches to fr-FR", page), "TM button names the locale"
for hint_id in ("hint-translate-live", "hint-translate-background", "hint-apply-tm"):
    assert f'id="{hint_id}"' in page, ("one-line hint missing", hint_id)
assert "Translate all 3 languages" in page, "multi-language document offers translate-all"
all_form = re.search(
    rf'<form[^>]*action="/documents/{doc_id}/translate"[^>]*>(?:(?!</form>).)*value="all"(?:(?!</form>).)*</form>',
    page,
    re.S,
)
assert all_form, "translate-all must post locale=all to the translate route"
assert 'id="hint-translate-all"' in page

# A one-language document does not offer translate-all.
single_doc = int(
    post(
        f"/projects/{empty_project}/documents",
        {"mode": "segmented"},
        {"file": ("one.txt", b"Only one.", "text/plain")},
    )
)
single_page = client.get(f"/documents/{single_doc}").text
assert "Translate all" not in single_page, "single-language document hides translate-all"

# --- 2. locale=all enqueues one job per locale ---------------------------------------
from app.web import documents_document_level_context_o as docs_mod

submitted = []
real_submit = docs_mod.jobs.submit


def fake_submit(kind, payload, actor="", **_kw):
    submitted.append((kind, dict(payload)))
    return 4000 + len(submitted)


real_submit_unique = docs_mod.jobs.submit_unique
_live = {}


def fake_submit_unique(kind, payload, actor="", dedupe_key="connection_id", **_kw):
    # Mirrors the real contract: an identical (document, locale) job already in flight is
    # returned, not stacked.
    key = (payload["document_id"], payload["locale"])
    if key in _live:
        return _live[key], False
    submitted.append((kind, dict(payload)))
    _live[key] = 4000 + len(submitted)
    return _live[key], True


docs_mod.jobs.submit = fake_submit
docs_mod.jobs.submit_unique = fake_submit_unique
try:
    r = client.post(
        f"/documents/{doc_id}/translate", data={"locale": "all"}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    assert sorted(p["locale"] for _k, p in submitted) == ["de-DE", "fr-FR", "ja-JP"], submitted
    assert all(k == "translate_document" for k, _p in submitted)
    # A double click while those jobs are still queued enqueues nothing more (no doubled spend).
    r = client.post(
        f"/documents/{doc_id}/translate", data={"locale": "all"}, follow_redirects=False
    )
    assert r.status_code == 303 and len(submitted) == 3, ("double dispatch", submitted)
    submitted.clear()
    r = client.post(
        f"/documents/{doc_id}/translate", data={"locale": "fr-FR"}, follow_redirects=False
    )
    assert r.status_code == 303 and [p["locale"] for _k, p in submitted] == ["fr-FR"], submitted
finally:
    docs_mod.jobs.submit = real_submit
    docs_mod.jobs.submit_unique = real_submit_unique

# --- 3. Document status follows the lifecycle ----------------------------------------
assert status_now() == "new", status_now()
set_segments("fr-FR", SegStatus.MT, "bonjour")
assert status_now() == "in_translation", ("one locale of three translated", status_now())
for loc in ("de-DE", "ja-JP"):
    set_segments(loc, SegStatus.MT, "x")
assert status_now() == "in_review", ("all translated, none approved", status_now())
set_segments("fr-FR", SegStatus.APPROVED, "bonjour")
assert status_now() == "in_review", ("approved in one locale only", status_now())
for loc in ("de-DE", "ja-JP"):
    set_segments(loc, SegStatus.APPROVED, "x")
assert status_now() == "done", status_now()
set_segments("ja-JP", SegStatus.NEEDS_REVIEW, "x")
assert status_now() == "in_review", ("a flagged segment keeps it in review", status_now())
set_segments("ja-JP", SegStatus.APPROVED, "x")

with Session(engine) as s:
    assert lifecycle.document_status_map(s, [doc_id, single_doc]) == {
        doc_id: "done",
        single_doc: "new",
    }
    empty = Document(project_id=int(project), filename="empty.txt", fmt="txt")
    s.add(empty)
    s.commit()
    s.refresh(empty)
    assert lifecycle.document_status(s, empty) == "new", "a document with no segments is new"

page = client.get(f"/documents/{doc_id}?locale=fr-FR").text
fact = re.search(
    r'<span class="fact-lbl">Status</span>\s*<span class="fact-val">(.*?)</span></div>', page, re.S
)
assert fact and "Done" in fact.group(1) and "New" not in fact.group(1), fact and fact.group(1)

code, out, err = run_cli(["--json", "doc", "show", str(doc_id)])
assert code == 0 and '"status": "done"' in out, (code, out[:300], err[:300])

code, out, err = run_cli(["--json", "doc", "list", project])
assert code == 0, (code, out[:300], err[:300])
import json as _json

listed = {d["filename"]: d["status"] for d in _json.loads(out)["documents"]}
assert listed["scope.txt"] == "done" and listed["empty.txt"] == "new", listed
code, out, err = run_cli(["--json", "project", "show", project])
assert code == 0, (code, out[:300], err[:300])
shown = {d["filename"]: d["status"] for d in _json.loads(out)["documents"]}
assert shown == listed, (shown, listed)

# The project page lists documents with the derived status too.
proj_page = client.get(f"/projects/{project}").text
assert re.search(r"scope\.txt.*?Done", proj_page, re.S), (
    "project document list shows derived status"
)

# Performance: the status map is ONE grouped segment query however many documents it covers
# (the project page lists every document; it must not query per document).
from sqlalchemy import event

with Session(engine) as s:
    extra = [
        Document(project_id=int(empty_project), filename=f"bulk{i}.txt", fmt="txt")
        for i in range(40)
    ]
    s.add_all(extra)
    s.commit()
    bulk_ids = [d.id for d in extra]
seg_queries = []


def _count(conn, cursor, statement, parameters, context, executemany):
    if "FROM segment" in statement and "GROUP BY" in statement:
        seg_queries.append(statement)


event.listen(engine, "before_cursor_execute", _count)
try:
    with Session(engine) as s:
        seg_queries.clear()
        lifecycle.document_status_map(s, [doc_id])
        one = len(seg_queries)
        seg_queries.clear()
        got = lifecycle.document_status_map(s, [doc_id, single_doc, *bulk_ids])
        many = len(seg_queries)
finally:
    event.remove(engine, "before_cursor_execute", _count)
assert one == 1 and many == 1, ("status map must be one grouped query", one, many)
assert got[doc_id] == "done" and all(got[i] == "new" for i in bulk_ids), got

# --- 4. Project status and count labels ----------------------------------------------
empty_id = post(
    "/projects",
    {
        "name": "Nothing yet",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "default_provider": "mock",
        "customer_id": "1",
    },
)
row_page = client.get("/projects").text
row = re.search(r"Nothing yet.*?</tr>", row_page, re.S).group(0)
assert "In translation" not in row, ("a project with no content is not in translation", row)
assert "No content yet" in row, row
detail = client.get(f"/projects/{empty_id}").text
life = re.search(r'id="project-lifecycle".*?</div>', detail, re.S).group(0)
assert "In translation" not in life and "No content yet" in life, life

# Counts say what they count: segments x languages.
scope_row = re.search(r"Scope project.*?</tr>", row_page, re.S).group(0)
assert "segments x 3 languages" in scope_row, scope_row
assert "6 translations" in scope_row, scope_row
assert "approved" in scope_row
assert re.search(r"segments x 3 languages", detail) is None
detail_scope = client.get(f"/projects/{project}").text
assert "2 segments x 3 languages" in detail_scope, "project page labels the count"
assert "2 documents" in detail_scope

# --- 5. The API and the project API carry the derived status too (last: a key creates a user)
from app.services import apikeys, security

_user = security.create_user("scope@test.invalid", "Scope admin", "admin", "scope-password-1234")
_, _raw = apikeys.create_key(_user.id, "scope", scopes="api docs admin")
_hdr = {"X-Loc-Token": _raw}
api = client.get(f"/api/documents/{doc_id}/segments", headers=_hdr).json()
assert api["status"] == "done", api
proj_api = client.get(f"/api/projects/{project}", headers=_hdr).json()
by_name = {d["filename"]: d["status"] for d in proj_api["documents"]}
assert by_name["scope.txt"] == "done", by_name
assert by_name["empty.txt"] == "new", by_name
# the remote `doc list` door (GET /api/projects/{id}/documents) agrees with the in-process one
docs_api = client.get(f"/api/projects/{project}/documents", headers=_hdr).json()
by_name = {d["filename"]: d["status"] for d in docs_api["documents"]}
assert by_name["scope.txt"] == "done" and by_name["empty.txt"] == "new", by_name

print("DOC SCOPE STATUS OK")
