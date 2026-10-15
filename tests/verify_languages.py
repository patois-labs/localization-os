"""Language sets + multilingual connector fan-out — offline.

- language-set CRUD + locale parsing
- effective_locales resolution: set > free-text, explicit override wins
- one request fans out to every locale in the set (Jira), and the connection's
  project becomes multilingual (a TargetLocale per language)
- /languages page + connection language selector routes
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Project, Segment, TargetLocale
from app.services import languages
from app.services.connectors import store as cstore


def jira_set_transport(posted: list):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [{"key": "LOC-1", "fields": {"summary": "Hello world"}}],
                },
            )
        if path.endswith("/comment") and req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "1"})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def main() -> None:
    init_db()

    # ---- CRUD + parsing ----
    assert languages.parse_locales("fr-FR, de-DE de-DE  es-ES") == ["fr-FR", "de-DE", "es-ES"]
    ls = languages.create("EMEA", "en-US", "fr-FR, de-DE, es-ES")
    assert ls.target_locales == ["fr-FR", "de-DE", "es-ES"]
    assert any(x.name == "EMEA" for x in languages.list_sets())
    languages.update(ls.id, "EMEA Tier 1", "en-US", "fr-FR, de-DE")
    assert languages.get(ls.id).target_locales == ["fr-FR", "de-DE"]
    print("  languages: create/list/update + locale parsing")

    # ---- effective_locales resolution ----
    conn = cstore.create(
        "jira",
        "J",
        {
            "base_url": "https://x.atlassian.net",
            "jql": "project = LOC",
            "language_set_id": str(ls.id),
        },
        {"email": "a@b.com", "api_token": "t"},
        actor="admin",
    )
    assert cstore.effective_locales(conn) == ["fr-FR", "de-DE"], cstore.effective_locales(conn)
    assert cstore.effective_locales(conn, "ja-JP") == ["ja-JP"], "override should win"
    conn2 = cstore.create(
        "jira",
        "J2",
        {"base_url": "https://x", "jql": "x", "target_locales": "it-IT, pt-BR"},
        {"email": "a", "api_token": "t"},
        actor="admin",
    )
    assert cstore.effective_locales(conn2) == ["it-IT", "pt-BR"], "free-text fallback"
    print("  effective_locales: set > free-text, explicit override wins")

    # ---- primary_locale: the single round-trip locale ----
    assert cstore.primary_locale(conn) == "fr-FR", cstore.primary_locale(conn)  # set's first
    assert cstore.primary_locale(conn2) == "it-IT", cstore.primary_locale(conn2)  # free-text first
    conn3 = cstore.create(
        "jira",
        "J3",
        {"base_url": "https://x", "jql": "x", "target_locale": "nl-NL"},
        {"email": "a", "api_token": "t"},
        actor="admin",
    )
    assert cstore.primary_locale(conn3) == "nl-NL", (
        "legacy target_locale should still win when no set"
    )
    conn4 = cstore.create(
        "jira",
        "J4",
        {
            "base_url": "https://x",
            "jql": "x",
            "target_locale": "es-ES",
            "target_locales": "de-DE, ja-JP",
            "language_set_id": str(ls.id),
        },
        {"email": "a", "api_token": "t"},
        actor="admin",
    )
    assert cstore.primary_locale(conn4) == "es-ES", (
        "explicit target_locale must keep winning the round-trip even with a set/target_locales"
    )
    print(
        "  primary_locale: blank target_locale -> set/free-text first; explicit target_locale wins"
    )

    # ---- pull on a set-configured connection seeds segments in the set's first locale ----
    ok, msg, doc_id = cstore.pull_connection(
        conn.id, actor="auto", transport=jira_set_transport([])
    )
    assert ok and doc_id, msg
    with Session(engine) as s:
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
    assert segs and all(sg.target_locale == "fr-FR" for sg in segs), [
        (sg.target_locale, sg.source_text) for sg in segs
    ]
    print("  pull via set: segments created in the set's first locale (fr-FR)")

    # ---- one request fans out to the whole set; project is multilingual ----
    posted = []
    ok, msg, summary = cstore.jira_translate(
        conn.id, actor="auto", transport=jira_set_transport(posted)
    )
    assert ok, msg
    assert summary["locales"] == ["fr-FR", "de-DE"] and summary["written"] == 2, summary
    assert len(posted) == 2, posted
    with Session(engine) as s:
        c = s.get(Connection, conn.id)
        locs = {
            tl.locale
            for tl in s.exec(
                select(TargetLocale).where(TargetLocale.project_id == c.project_id)
            ).all()
        }
    assert {"fr-FR", "de-DE"} <= locs, locs
    print("  jira_translate via set: 1 request -> 2 languages; project went multilingual")

    # ---- UI ----
    c = TestClient(app)
    page = c.get("/languages").text
    assert "Language sets" in page and "EMEA Tier 1" in page, "languages page missing data"
    assert "Language sets" in c.get("/").text, "nav link missing"
    r = c.post(
        "/languages",
        data={"name": "CJK2", "source_locale": "en-US", "target_locales": "ja-JP, ko-KR"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and any(x.name == "CJK2" for x in languages.list_sets())

    detail = c.get(f"/connectors/connection/{conn.id}").text
    assert "Target languages" in detail, "connection language card missing"
    r = c.post(
        f"/connectors/connection/{conn2.id}/languages",
        data={"language_set_id": str(ls.id), "target_locales": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (cstore.get(conn2.id).config or {}).get("language_set_id") == str(ls.id)
    print("  routes: /languages renders + create; connection language selector applies a set")

    # ---- apply a Language Set to a regular (non-connector) project ----
    with Session(engine) as s:
        proj = Project(
            name="Plain Project", source_locale="en-US", default_provider="mock", created_by="admin"
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        proj_id = proj.id
        # Pre-existing locale that must survive untouched (additive merge).
        s.add(TargetLocale(project_id=proj_id, locale="pt-BR"))
        s.commit()

    r = c.post(
        f"/projects/{proj_id}/languages",
        data={"language_set_id": str(ls.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "langs_added=2" in r.headers["location"], r.headers.get(
        "location"
    )
    with Session(engine) as s:
        locs = {
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == proj_id)).all()
        }
    assert locs == {"pt-BR", "fr-FR", "de-DE"}, locs
    print("  apply set to plain project: TargetLocale rows created for every set locale")

    # Re-apply: idempotent, no duplicate rows, no error.
    r = c.post(
        f"/projects/{proj_id}/languages",
        data={"language_set_id": str(ls.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "langs_added=0" in r.headers["location"], r.headers.get(
        "location"
    )
    with Session(engine) as s:
        rows = s.exec(select(TargetLocale).where(TargetLocale.project_id == proj_id)).all()
        locs2 = [tl.locale for tl in rows]
    assert sorted(locs2) == sorted(["pt-BR", "fr-FR", "de-DE"]), locs2
    assert len(locs2) == len(set(locs2)) == 3, "re-apply must not create duplicate rows"
    print("  re-apply same set: idempotent, no duplicates")

    # Additive merge: pre-existing pt-BR row (and its id) must be untouched.
    with Session(engine) as s:
        pt_rows = s.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == proj_id, TargetLocale.locale == "pt-BR"
            )
        ).all()
    assert len(pt_rows) == 1, "existing locale row must survive untouched, not be duplicated"
    print("  additive merge: pre-existing TargetLocale row untouched")

    # Unknown language set id -> friendly error redirect, no crash.
    r = c.post(
        f"/projects/{proj_id}/languages", data={"language_set_id": "999999"}, follow_redirects=False
    )
    assert r.status_code == 303 and "err=langset" in r.headers["location"]
    print("  apply unknown set: friendly error redirect")

    print("LANGUAGES OK")


if __name__ == "__main__":
    main()
