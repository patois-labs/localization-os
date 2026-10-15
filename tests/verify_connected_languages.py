"""Connected projects T2: a bound project's languages drive its runs (offline).

2026-10-13 (decision 4): a project that has a content source (a ProjectAutomation) changes
its target languages over time. Adding one means the next run includes it (only the new pairs);
removing one means runs stop pulling, translating and writing it from the next run on, and NOTHING
is deleted: the language is STOPPED (TargetLocale.stopped_at), its segments and TM stay, what was
written to the external system is left alone, and the project's claims on it are released. The
project's language list is the ONLY source of truth: the run connection follows it.

Proves, against the fake Zendesk tenant (the shared flow engine) and a fake Jira (a kind with its
own loop):

- a bound run uses the project's languages, not the connection's own list (add -> only the new
  pairs are written; the old pairs are not redone)
- stop -> the next run does not translate or write the language; segments, TM and the external
  system's translations stay; claims are released; another project can take the language
- re-add -> the pairs run again (the stale per-pair hash from before the stop is forgotten), the
  stopped segments are reused
- an unbound connection is byte-identical to before (its own list, and plain projects still
  delete a removed language)
- last-active rule, the permanence rule does not apply to a bound project, language sets bring a
  stopped language back, new documents and default batches skip a stopped language, completion
  ignores it
- Jira (own loop) honours the project list; Airtable columns of a stopped language are skipped
- the four surfaces: /api add-locale, remove-locale (no typed confirmation for a bound project),
  show; the web confirm page and project page; the CLI; the MCP action
"""

import json
import os
import sys
import time
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_connlang_")

import _flow_parity_fixtures as fx
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    Job,
    JobStatus,
    ProjectBindingClaim,
    Segment,
    SegmentTM,
    TargetLocale,
)
from app.services import content, content_projects, lifecycle, tm
from app.services import project_automation as pa
from app.services.connectors import store as cstore
from app.services.connectors import store_common


def mkproj(name, locales="fr"):
    with Session(engine) as s:
        return content_projects.project_create(s, name, "en-US", locales, created_by="t@x.co").id


def wait_job(jid, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            j = s.get(Job, jid)
            if j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish")


def audit_rows(action, needle=""):
    with Session(engine) as s:
        return [
            a
            for a in s.exec(select(AuditLog).where(AuditLog.action == action)).all()
            if needle in (a.target or "") + (a.detail or "")
        ]


def zd(name, **cfg):
    fz = fx.FakeZendesk(
        articles=[
            fx._zd_article("100", edited_at="2026-07-01T00:00:00Z"),
            fx._zd_article("101", edited_at="2026-07-02T00:00:00Z"),
        ]
    )
    return fx._zd_conn(name, cfg), fz


def patch_flow(holder):
    def run_flow(cid, actor="", transport=None):
        return cstore.zendesk_translate(cid, actor=actor, transport=holder["fz"].transport())

    set_pieces("app.services.connectors.store", "translate_now", run_flow)


def written(fz):
    """The (article, locale) pairs written so far (creates + updates)."""
    return [(a, loc) for a, loc, _b in fz.creates] + [(a, loc) for a, loc, _b in fz.updates]


def rows(pid, include_stopped=True):
    with Session(engine) as s:
        q = select(TargetLocale).where(TargetLocale.project_id == pid)
        return [
            (t.locale, t.stopped_at is not None)
            for t in s.exec(q.order_by(TargetLocale.id)).all()
            if include_stopped or t.stopped_at is None
        ]


def seg_count(pid, locale):
    with Session(engine) as s:
        docs = [d.id for d in s.exec(select(Document).where(Document.project_id == pid)).all()]
        return len(
            [
                1
                for sg in s.exec(select(Segment).where(Segment.document_id.in_(docs))).all()
                if sg.target_locale.lower() == locale.lower()
            ]
        )


def claims_for(pid):
    with Session(engine) as s:
        return {
            (c.external_id, c.locale)
            for c in s.exec(
                select(ProjectBindingClaim).where(ProjectBindingClaim.project_id == pid)
            ).all()
        }


def run(pid):
    j = wait_job(pa.run_now(pid, actor="m@x.co")[0])
    assert j.status == JobStatus.COMPLETED, (j.status, j.result)
    return j


# ---------------------------------------------------------------------------------------
def test_add_language_runs_only_new_pairs():
    real = cstore.translate_now
    # the connection itself names de AND fr; the project only has fr: the PROJECT decides
    conn, fz = zd("CL add", target_locales="de, fr")
    holder = {"fz": fz}
    patch_flow(holder)
    try:
        pid = mkproj("CL add", "fr")
        pa.bind(pid, conn.id, is_admin=True)
        run(pid)
        assert sorted(written(fz)) == [("100", "fr"), ("101", "fr")], (
            "a bound run uses the project's languages, not the connection's own list",
            written(fz),
        )
        cfg = cstore.get(conn.id).config
        assert cfg["target_locales"] == "fr", ("the run connection follows the project", cfg)
        with Session(engine) as s:
            v = pa.view(s, pa.get(s, pid))
        assert v["languages"] == ["fr"] and v["stopped_languages"] == [], v

        # add de on the project: the next run writes ONLY the new pairs
        with Session(engine) as s:
            assert content.add_target_locale(s, pid, "de", actor="m@x.co") is True
        assert cstore.get(conn.id).config["target_locales"] == "fr, de"
        assert len(audit_rows("automation.locale_added", f"project:{pid}")) == 1
        before = list(written(fz))
        run(pid)
        new = written(fz)[len(before) :]
        assert sorted(new) == [("100", "de"), ("101", "de")], ("only the new pairs", new)
        # and a further run redoes nothing
        n = len(written(fz))
        run(pid)
        assert len(written(fz)) == n, "unchanged pairs are not redone"
        assert claims_for(pid) == {
            ("100", "fr"),
            ("101", "fr"),
            ("100", "de"),
            ("101", "de"),
        }, claims_for(pid)
        print("  add: the project's list drives the run, only the new language's pairs are written")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_stop_language_keeps_everything_and_releases_claims():
    real = cstore.translate_now
    conn, fz = zd("CL stop")
    holder = {"fz": fz}
    patch_flow(holder)
    try:
        pid = mkproj("CL stop", "fr, de")
        pa.bind(pid, conn.id, is_admin=True)
        run(pid)
        assert len(written(fz)) == 4
        de_segments = seg_count(pid, "de")
        assert de_segments > 0 and ("100", "de") in claims_for(pid)
        with Session(engine) as s:
            s.add(
                SegmentTM(
                    project_id=pid,
                    source_locale="en-US",
                    target_locale="de",
                    source_text="Hello",
                    target_text="Hallo",
                    source_hash="h-cl-stop",
                )
            )
            s.commit()
            tm_before = tm.count(s, pid)
        assert tm_before >= 1, tm_before
        hashes_before = cstore.get(conn.id).config.get("zendesk_hashes") or {}
        assert any(k.endswith(":de") for k in hashes_before), hashes_before

        with Session(engine) as s:
            preview = content.remove_locale_preview(s, pid, "de")
            assert preview["mode"] == "stop" and not preview["blocked"], preview
            assert all(v == 0 for v in preview["deletes"].values()), preview["deletes"]
            assert preview["preserves"]["segments"] == de_segments
            deleted = content.remove_target_locale(s, pid, "de", actor="m@x.co")
            assert all(v == 0 for v in deleted.values()), deleted

        # nothing deleted: the row, segments and TM are all still there
        assert rows(pid) == [("fr", False), ("de", True)], rows(pid)
        assert seg_count(pid, "de") == de_segments
        with Session(engine) as s:
            assert tm.count(s, pid) == tm_before
            assert content.active_target_locales(s, pid) == ["fr"]
            assert content.stopped_target_locales(s, pid) == ["de"]
        # the claims on de are released, fr's stay; per-pair state for de is forgotten
        assert claims_for(pid) == {("100", "fr"), ("101", "fr")}, claims_for(pid)
        assert not any(
            k.endswith(":de") for k in (cstore.get(conn.id).config.get("zendesk_hashes") or {})
        )
        assert cstore.get(conn.id).config["target_locales"] == "fr"
        assert audit_rows("automation.locale_stopped", f"project:{pid}")
        assert audit_rows("project.remove_locale", f"project {pid} de")

        # the next run (the source changed, so fr is processed again) never touches de
        fz.articles["100"]["edited_at"] = "2026-07-09T00:00:00Z"
        fz.articles["100"]["title"] = "Reset password now"
        before = list(written(fz))
        run(pid)
        new = written(fz)[len(before) :]
        assert new and all(loc == "fr" for _a, loc in new), ("no de write", new)
        assert ("100", "de") not in claims_for(pid)
        # what was written for de earlier is still in the external system
        assert ("100", "de") in fz.translations and ("101", "de") in fz.translations

        # another project may take the language now
        other = mkproj("CL stop other", "de")
        pa.bind(other, conn.id, is_admin=True)
        j = run(other)
        assert not j.result.get("claim_conflicts"), j.result
        assert ("100", "de") in claims_for(other)
        # the other project runs on a private lane: its languages are its own, not the picked
        # connection's (the lane sync that runs before every run must not reset them)
        with Session(engine) as s:
            lane_id = pa.get(s, other).run_connection_id
        assert lane_id != conn.id
        assert cstore.get(lane_id).config["target_locales"] == "de"
        assert store_common.effective_locales(cstore.get(lane_id)) == ["de"]
        print("  stop: nothing deleted, claims released, no further de writes, another project can")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_readd_runs_the_pairs_again():
    real = cstore.translate_now
    conn, fz = zd("CL readd")
    holder = {"fz": fz}
    patch_flow(holder)
    try:
        pid = mkproj("CL readd", "fr, de")
        pa.bind(pid, conn.id, is_admin=True)
        run(pid)
        de_segments = seg_count(pid, "de")
        with Session(engine) as s:
            content.remove_target_locale(s, pid, "de", actor="m@x.co")
        run(pid)  # a quiet run while stopped
        n = len(written(fz))
        # a run still in flight when the language was stopped can write the pair's hash back:
        # put a stale one in and prove the re-add forgets it too
        cstore.patch_config_merge(conn.id, "zendesk_hashes", {"100:de": "stale"})
        with Session(engine) as s:
            assert content.add_target_locale(s, pid, "de", actor="m@x.co") is True
        assert rows(pid) == [("fr", False), ("de", False)], rows(pid)
        assert seg_count(pid, "de") == de_segments, (
            "the stopped segments are reused, not duplicated"
        )
        hashes = cstore.get(conn.id).config.get("zendesk_hashes") or {}
        assert not any(k.endswith(":de") for k in hashes), hashes
        assert audit_rows("automation.locale_added", f"project:{pid}")[-1].detail.endswith(
            "the next run includes it"
        )
        run(pid)
        new = written(fz)[n:]
        assert sorted(new) == [("100", "de"), ("101", "de")], (
            "re-added: the pairs run again, nothing else",
            new,
        )
        assert cstore.get(conn.id).config["target_locales"] == "fr, de"
        print("  re-add: same row and segments, stale hash forgotten, only its pairs run again")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_unbound_connection_and_plain_project_unchanged():
    # an unbound connection: its own list, whatever projects exist
    conn, _fz = zd("CL plain", target_locales="de, fr")
    assert store_common.effective_locales(conn) == ["de", "fr"]
    assert store_common.effective_locales(conn, "ja-JP") == ["ja-JP"]
    assert store_common.bound_project_locales(conn) is None
    assert store_common.locale_is_active(conn, "anything")
    # a bound one: the project's list, in the connection's own spelling, override can narrow
    pid = mkproj("CL spelled", "fr, de")
    conn2, _ = zd("CL spelled", target_locales="de, fr")
    pa.bind(pid, conn2.id, is_admin=True)
    conn2 = cstore.get(conn2.id)
    assert store_common.effective_locales(conn2) == ["fr", "de"]
    assert store_common.effective_locales(conn2, "de, ja-JP") == ["de"]
    assert not store_common.locale_is_active(conn2, "ja-JP")
    # a plain project still deletes a removed language, with its permanence rule
    plain = mkproj("CL plain project", "fr, de")
    with Session(engine) as s:
        pv = content.remove_locale_preview(s, plain, "de")
        assert pv["mode"] == "delete" and not pv["blocked"], pv
        content.remove_target_locale(s, plain, "de", actor="t")
    assert rows(plain) == [("fr", False)]
    assert not audit_rows("automation.locale_stopped", f"project:{plain}")
    print("  unbound: connection list and plain-project removal behave exactly as before")


def test_rules_around_a_stopped_language():
    real = cstore.translate_now
    conn, fz = zd("CL rules")
    holder = {"fz": fz}
    patch_flow(holder)
    try:
        pid = mkproj("CL rules", "fr, de")
        pa.bind(pid, conn.id, is_admin=True)
        run(pid)  # both languages now have translated content
        with Session(engine) as s:
            # the permanence rule is for deleting; a bound project only stops
            assert content.translated_target_locales(s, pid)
            assert content.remove_locale_preview(s, pid, "fr")["mode"] == "stop"
            content.remove_target_locale(s, pid, "de", actor="m@x.co")
            # the last ACTIVE language cannot be stopped
            last = content.remove_locale_preview(s, pid, "fr")
            assert last["blocked"] and "only target locale" in last["blocked_reason"], last
            try:
                content.remove_target_locale(s, pid, "fr", actor="m@x.co")
            except content.LocaleRemovalBlocked:
                pass
            else:
                raise AssertionError("the last active language was stopped")
            # stopping again is not a second stop
            assert content.remove_locale_preview(s, pid, "de")["mode"] == "delete"

        # a language set brings the stopped language back
        with Session(engine) as s:
            lang_set = SimpleNamespace(target_locales=["de", "ja-JP"])
            assert content.apply_language_set_to_project(s, pid, lang_set, actor="t") == 2
        assert rows(pid) == [("fr", False), ("de", False), ("ja-JP", False)], rows(pid)
        with Session(engine) as s:
            content.remove_target_locale(s, pid, "ja-JP", actor="t")
            content.remove_target_locale(s, pid, "de", actor="t")

        # a new document is seeded for the active languages only
        with Session(engine) as s:
            from app.models import Project, TransMode
            from app.services.ingest import ingest_document

            doc = ingest_document(
                s, s.get(Project, pid), "extra.txt", b"One line of text.\n", TransMode.SEGMENTED
            )
            seeded = {
                sg.target_locale
                for sg in s.exec(select(Segment).where(Segment.document_id == doc.id)).all()
            }
        assert seeded == {"fr"}, seeded

        # default batch locales skip a stopped language (an explicit one is still allowed)
        from app.services import batch_runs_hub

        with Session(engine) as s:
            stopped = batch_runs_hub._stopped_locale_norms(s, pid)
        assert stopped == {"de", "ja-jp"}, stopped

        # completion ignores a stopped language's open segments
        with Session(engine) as s:
            for d in s.exec(select(Document).where(Document.project_id == pid)).all():
                for sg in s.exec(select(Segment).where(Segment.document_id == d.id)).all():
                    if sg.target_locale == "fr":
                        sg.target_text = sg.target_text or "x"
                    else:
                        sg.target_text = ""
                    s.add(sg)
            s.commit()
            assert lifecycle._all_segments_translated(s, pid), (
                "stopped languages must not hold completion back"
            )
        print("  rules: last active, no permanence, set restart, new docs/batches/completion skip")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_jira_own_loop_honours_project_languages():
    posts = []

    def handler(req):
        path = req.url.path
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [{"key": "LOC-1", "fields": {"summary": "Hello world"}}],
                },
            )
        if req.method == "POST" and path.endswith("/comment"):
            import json as _json

            posts.append(_json.loads(req.content))
            return httpx.Response(201, json={"id": str(len(posts))})
        return httpx.Response(404, json={"path": path})

    transport = httpx.MockTransport(handler)
    cfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary",
        "target_mode": "comment",
        "target_locales": "es-MX, de-DE, fr-FR",  # the connection names three languages
        "source_locale": "en-US",
    }
    conn = cstore.create(
        "jira", "CL jira", cfg, {"email": "b@x.com", "api_token": "tok"}, actor="a"
    )
    pid = mkproj("CL jira", "es-MX, de-DE")
    pa.bind(pid, conn.id, is_admin=True)

    ok, msg, _res = cstore.jira_translate(conn.id, actor="t", transport=transport)
    assert ok, msg
    first = len(posts)
    assert first == 2, ("two project languages, not three", first, posts)

    with Session(engine) as s:
        content.remove_target_locale(s, pid, "de-DE", actor="m@x.co")
    assert not any(
        k.endswith(":de-de") or k.endswith(":de-DE")
        for k in (cstore.get(conn.id).config.get("jira_hashes") or {})
    )
    cstore.patch_config(conn.id, {"jira_hashes": {}})  # force a full re-look
    ok, msg, _res = cstore.jira_translate(conn.id, actor="t", transport=transport)
    assert ok, msg
    assert len(posts) - first == 1, ("only the remaining language is written", posts[first:])

    with Session(engine) as s:
        content.add_target_locale(s, pid, "fr-FR", actor="m@x.co")
    before = len(posts)
    ok, msg, _res = cstore.jira_translate(conn.id, actor="t", transport=transport)
    assert ok, msg
    assert len(posts) - before >= 1, "the added language is written on the next run"
    print("  jira (own loop): the run reads the project's active languages")


def test_airtable_columns_of_a_stopped_language_are_skipped():
    conn = cstore.create(
        "airtable",
        "CL airtable",
        {"base_id": "appX", "table": "T", "source_field": "Name", "target_locales": "fr"},
        {"api_token": "pat"},
        actor="a",
    )
    pid = mkproj("CL airtable", "fr, de")
    pa.bind(pid, conn.id, is_admin=True)
    conn = cstore.get(conn.id)
    assert store_common.locale_is_active(conn, "fr") and store_common.locale_is_active(conn, "de")
    with Session(engine) as s:
        content.remove_target_locale(s, pid, "de", actor="m@x.co")
    conn = cstore.get(conn.id)
    assert store_common.locale_is_active(conn, "fr")
    assert not store_common.locale_is_active(conn, "de"), "its column is skipped on the next run"
    assert not store_common.locale_is_active(conn, "ja-JP"), "a language not on the project too"
    print("  airtable: columns of a stopped (or unlisted) language are not written")


def test_surfaces():
    real = cstore.translate_now
    conn, fz = zd("CL surfaces")
    holder = {"fz": fz}
    patch_flow(holder)
    try:
        pid = mkproj("CL surfaces", "fr, de")
        pa.bind(pid, conn.id, is_admin=True)
        run(pid)
        plain = mkproj("CL surfaces plain", "fr, de")

        # web (sign-in is off until the first user exists): the page and the confirm pages
        c = TestClient(app)
        html = c.get(f"/projects/{pid}").text
        assert 'id="connected-languages-note"' in html and 'id="stopped-locales"' not in html
        with Session(engine) as s:
            content.add_target_locale(s, pid, "ja-JP", actor="t")
        r = c.get(f"/projects/{pid}/remove-locale/ja-JP")
        assert r.status_code == 200
        assert "stops at the next run" in r.text and "confirm_locale" not in r.text, "stop page"
        r = c.post(
            f"/projects/{pid}/remove-locale", data={"locale": "ja-JP"}, follow_redirects=False
        )
        assert r.status_code == 303 and "locale_removed" in r.headers["location"], r.headers
        page = c.get(f"/projects/{pid}?locale_removed=ja-JP").text
        assert 'id="stopped-locales"' in page and "ja-JP" in page, "the stopped chip"
        assert "stops at the next run" in page and "Nothing was deleted" in page
        # the plain project's confirm page is still the typed one
        r = c.get(f"/projects/{plain}/remove-locale/fr")
        assert "confirm_locale" in r.text and "stops at the next run" not in r.text
        # the TM export links still cover the stopped language
        assert "target=ja-JP" in page, "TM exports keep covering a stopped language"

        # CLI (in-process)
        import io
        from contextlib import redirect_stdout

        from app.cli.__main__ import main as cli_main

        def cli(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_main(["--json", *argv])
            out = buf.getvalue().strip()
            return code, (json.loads(out.splitlines()[-1]) if out else {})

        code, out = cli("project", "remove-locale", str(pid), "de", "--yes")
        assert code == 0 and out["stopped"] is True, (code, out)
        code, out = cli("project", "show", str(pid))
        assert out["target_locales"] == ["fr"] and sorted(out["stopped_locales"]) == [
            "de",
            "ja-JP",
        ], out
        code, out = cli("project", "add-locale", str(pid), "de")
        assert code == 0 and out["added"] is True, out
        code, out = cli("project", "remove-locale", str(plain), "de", "--yes")
        assert code == 0 and out["stopped"] is False, out
        with open(
            os.path.join(_ROOT, "integrations", "mcp-server", "server.py"), encoding="utf-8"
        ) as fh:
            text = fh.read()
        # MCP stays add-only on purpose (an agent never removes or stops a language); its
        # add-locale reaches the same service, so a bound project's next run includes it
        assert (
            '"remove-locale"' not in text and '"add-locale": ("/api/projects/%d/add-locale"' in text
        )

        # /api (an api key from here on: creating the user turns sign-in on)
        from app.models import Role
        from app.services import apikeys, security, settings

        settings.set_many({"project_scoping": "0"})
        admin = security.create_user("cl-admin@x.co", "CL Admin", Role.ADMIN, "pw-fixture-4417")
        _k, key = apikeys.create_key(admin.id, "cl admin", scopes="api docs")
        H = {"X-Loc-Token": key}
        # a bound project's removal needs no typed confirmation and says stopped
        r = c.post(f"/api/projects/{pid}/remove-locale", headers=H, json={"locale": "de"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stopped"] is True and all(v == 0 for v in body["deleted"].values()), body
        show = c.get(f"/api/projects/{pid}", headers=H).json()
        assert show["target_locales"] == ["fr"] and "de" in show["stopped_locales"], show
        # the same call on a plain project still demands the typed confirmation
        r = c.post(f"/api/projects/{plain}/remove-locale", headers=H, json={"locale": "fr"})
        assert r.status_code == 400 and "confirm_locale" in r.json()["error"], r.text
        # the last active language is refused on a bound project too
        r = c.post(f"/api/projects/{pid}/remove-locale", headers=H, json={"locale": "fr"})
        assert r.status_code == 400, r.text
        # add brings it back
        r = c.post(f"/api/projects/{pid}/add-locale", headers=H, json={"locale": "de"})
        assert r.status_code == 200 and r.json()["added"] is True, r.text
        assert "de" not in c.get(f"/api/projects/{pid}", headers=H).json()["stopped_locales"]
        print("  surfaces: web confirm + page + exports, cli, api, mcp (add only) agree")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_key_project_stopped_language_stays_exportable():
    """Review gate: a key project's stopped language keeps its values readable and exportable
    (the file export and the delta feed), while new work (active_locales) skips it."""
    from datetime import datetime, timedelta

    from app.models import Project, ProjectType
    from app.services import key_files, settings
    from app.services import keys as keys_svc

    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        pid = content.project_create(
            s,
            "CL keys stop",
            source_locale="en-US",
            target_locales="de-DE, fr-FR",
            project_type=ProjectType.KEY_BASED,
        ).id
    with Session(engine) as s:
        project = s.get(Project, pid)
        key = keys_svc.create_key(
            s, project, "greet", namespace="app", base_value="Hello", actor="fixture"
        )
        keys_svc.set_value(
            s, key, "de-DE", "Hallo gestoppt", origin=keys_svc.ORIGIN_HUMAN, actor="fixture"
        )
    with Session(engine) as s:
        for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid)).all():
            if tl.locale == "de-DE":
                tl.stopped_at = datetime.utcnow()
                s.add(tl)
        s.commit()
    with Session(engine) as s:
        assert keys_svc.active_locales(s, pid) == ["fr-FR"], "new work skips a stopped language"
        project = s.get(Project, pid)
        data = key_files.export_locale(s, project, "de-DE", fmt="json", namespace="app")
        assert b"Hallo gestoppt" in data, "a stopped language's values stay exportable"
        since = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        out = keys_svc.changes_modified_after(s, pid, since, locale="de-DE")
        assert any(e.get("text") == "Hallo gestoppt" for e in out.get("entries", [])), out
    print("  ok key project: a stopped language stays exportable and readable")


# ---------------------------------------------------------------------------------------
# FIX-locales (10-13 sweep: verify_akeneo / gdocs / msgraph / servicenow red after T4)
# ---------------------------------------------------------------------------------------
def _active(pid):
    return [loc for loc, stopped in rows(pid) if not stopped]


def _project_of(cid):
    from app.models import ProjectAutomation

    with Session(engine) as s:
        row = s.exec(
            select(ProjectAutomation).where(ProjectAutomation.run_connection_id == cid)
        ).first()
        return row.project_id if row else None


def _refused(fn, *a, **k):
    try:
        fn(*a, **k)
    except cstore.ConnectorError as exc:
        return exc
    raise AssertionError("expected a refusal")


def test_one_row_per_language_on_every_path():
    """Two spellings of one language (pt-br, pt-BR) are ONE TargetLocale row and ONE run entry,
    whatever path adds it: the connector registration, the MCP translate door, a legacy
    duplicate already in the database."""
    from app.models import DocStatus, Project, TransMode
    from app.models import Document as Doc
    from app.web import api_discovery

    pid = mkproj("CL one row", "fr-FR, pt-BR")
    with Session(engine) as s:
        store_common._add_missing_locales(s, pid, ["pt-br", "PT_br", "fr-fr", "de"])
        s.commit()
    assert rows(pid) == [("fr-FR", False), ("pt-BR", False), ("de", False)], (
        "the connector registration compares by the catalog-normalized language",
        rows(pid),
    )
    # the MCP / jobs translate door registers a language through its own seam
    with Session(engine) as s:
        doc = Doc(
            project_id=pid,
            filename="a.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status=DocStatus.NEW,
            skeleton={},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(Segment(document_id=doc.id, target_locale="pt-BR", order_index=0, source_text="Hi"))
        s.commit()
        assert api_discovery.prepare_translate_locale(s, doc, "pt-br") is None
    assert [loc for loc, _ in rows(pid)].count("pt-BR") == 1 and len(rows(pid)) == 3, rows(pid)
    # a legacy duplicate already in the database runs once, in the connection's spelling
    conn, _ = zd("CL one row", target_locales="pt-br, fr")
    legacy = mkproj("CL legacy dup", "pt-BR")
    with Session(engine) as s:
        s.add(TargetLocale(project_id=legacy, locale="pt-br"))
        s.add(TargetLocale(project_id=legacy, locale="fr-FR"))
        s.commit()
        assert s.get(Project, legacy) is not None
    pa.bind(legacy, conn.id, is_admin=True)
    conn = cstore.get(conn.id)
    assert store_common.bound_project_locales(conn) == ["pt-BR", "fr-FR"], (
        store_common.bound_project_locales(conn)
    )
    # bind rewrote the run connection's settings to the project's list, one entry per language,
    # keeping the connection's own (wire) spelling of a language it already named (Review gate)
    assert cstore.get(conn.id).config["target_locales"] == "pt-br, fr-FR", cstore.get(
        conn.id
    ).config
    assert store_common.effective_locales(conn) == ["pt-br", "fr-FR"], (
        "one run entry per language",
        store_common.effective_locales(conn),
    )
    # and a connection still naming its own wire spelling keeps it, once per language
    cfg = {**conn.config, "target_locales": "pt-br, PT-BR, fr-fr"}
    wire = SimpleNamespace(id=conn.id, config=cfg)
    assert store_common.effective_locales(wire) == ["pt-br", "fr-fr"], (
        store_common.effective_locales(wire)
    )
    print("  one row per language: registration, translate door, legacy duplicates run once")


def test_first_pull_wire_spelling_makes_one_row():
    """A non-Zendesk connection whose config names a language in wire spelling (pt-br): its first
    pull creates a connected project with ONE pt-BR row (catalog spelling), and every later run
    lists the language once (verify_gdocs / verify_msgraph / verify_akeneo regression)."""
    fz = fx.FakeD360(enabled_langs=("en", "de", "pt-br"), articles=[fx._d360_md_article("100")])
    conn = fx._d360_conn("CL wire", {"target_locales": "de, pt-br"})
    ok, msg, res = cstore.document360_translate(conn.id, actor="t", transport=fz.transport())
    assert ok, (msg, res)
    pid = _project_of(conn.id)
    assert pid is not None, "the first pull made a connected project bound to the connection"
    assert rows(pid) == [("de", False), ("pt-BR", False)], rows(pid)
    conn = cstore.get(conn.id)
    eff = store_common.effective_locales(conn)
    assert len(eff) == 2 and len({e.lower() for e in eff}) == 2, eff
    ok, msg, res2 = cstore.document360_translate(conn.id, actor="t", transport=fz.transport())
    assert ok, (msg, res2)
    assert seg_count(pid, "pt-br") == seg_count(pid, "de") > 0, (
        "one segment set per language",
        seg_count(pid, "pt-br"),
        seg_count(pid, "de"),
    )
    print("  first pull: wire spelling pt-br gives one pt-BR row and one run entry")


def test_connection_language_edit_flows_into_adopted_project():
    """The adopted case (a connector's first pull made the project, run_connection_id ==
    connection_id): changing the CONNECTION's languages changes the PROJECT's list through the
    add / stop service, and the connection follows the project (verify_servicenow regression)."""
    real = cstore.translate_now
    conn, fz = zd("CL adopted", target_locales="fr")
    try:
        ok, msg, _r = cstore.zendesk_translate(conn.id, actor="t", transport=fz.transport())
        assert ok, msg
        pid = _project_of(conn.id)
        assert pid is not None and _active(pid) == ["fr"], (pid, rows(pid))
        n = len(written(fz))

        # add de on the connection (the config-patch path the suites use)
        cstore.patch_config(conn.id, {"target_locales": "fr, de"}, actor="t")
        assert _active(pid) == ["fr", "de"], ("the project gains the language", rows(pid))
        assert audit_rows("automation.locale_added", f"project:{pid}"), "through the add service"
        assert audit_rows("automation.languages_from_connection", f"project:{pid}")
        cstore.zendesk_translate(conn.id, actor="t", transport=fz.transport())
        assert sorted(written(fz)[n:]) == [("100", "de"), ("101", "de")], written(fz)[n:]

        # remove fr through update() (web form, /api, CLI): STOPPED, never deleted
        segs_fr = seg_count(pid, "fr")
        cstore.update(conn.id, "", {"target_locales": "de"}, None, actor="t")
        assert rows(pid) == [("fr", True), ("de", False)], rows(pid)
        assert seg_count(pid, "fr") == segs_fr > 0, "nothing deleted"
        assert cstore.get(conn.id).config["target_locales"] == "de"

        # adding it back on the connection restarts the stopped row (no second row)
        cstore.patch_config(conn.id, {"target_locales": "de, FR"}, actor="t")
        assert rows(pid) == [("fr", False), ("de", False)], rows(pid)
        # the connection follows the project's list, in the spelling the person typed (Review gate)
        assert cstore.get(conn.id).config["target_locales"] == "FR, de", "connection follows"

        # an empty list changes nothing on the project; the connection is set back to it
        cstore.update(conn.id, "", {"target_locales": ""}, None, actor="t")
        assert _active(pid) == ["fr", "de"]
        assert cstore.get(conn.id).config["target_locales"] == "fr, de"

        # refusals: nothing saved, nothing changed on the project
        exc = _refused(cstore.patch_config, conn.id, {"target_locales": "fr, de, xx-bogus"})
        assert "xx-bogus" in str(exc) and getattr(exc, "status", 0) == 400, exc
        exc = _refused(cstore.update, conn.id, "", {"target_locales": "en-us"}, None)
        assert "at least one language" in str(exc), exc
        exc = _refused(
            cstore.patch_config, conn.id, {"target_locales": "fr, de, ja"}, can_add_languages=False
        )
        assert "manager" in str(exc) and getattr(exc, "status", 0) == 403, exc
        assert _active(pid) == ["fr", "de"] and cstore.get(conn.id).config["target_locales"] == (
            "fr, de"
        )
        # a stop-only change needs no manager
        cstore.patch_config(conn.id, {"target_locales": "fr"}, can_add_languages=False)
        assert _active(pid) == ["fr"]

        # the in-process CLI goes through the same store.update
        import io
        from contextlib import redirect_stdout

        from app.cli.__main__ import main as cli_main

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(
                ["--json", "connector", "update", str(conn.id), "--config", "target_locales=fr,it"]
            )
        assert code == 0, buf.getvalue()
        assert _active(pid) == ["fr", "it"], rows(pid)

        # a plain connection (no project automation) saves exactly as before
        plain, _ = zd("CL plain edit", target_locales="fr")
        cstore.patch_config(plain.id, {"target_locales": "fr, xx-anything"})
        assert cstore.get(plain.id).config["target_locales"] == "fr, xx-anything"
        print("  adopted connection: language edits flow into the project (add / stop), refusals")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_feeder_connection_language_edit_refused():
    """A connection that only FEEDS other projects (each through its own copy) does not change
    their languages: the change is refused, naming the project, and nothing is saved."""
    conn, _fz = zd("CL feeder", target_locales="de, fr")
    pid = mkproj("CL feeder project", "fr")
    pa.bind(pid, conn.id, is_admin=True, clone=True)
    from app.models import ProjectAutomation

    with Session(engine) as s:
        row = s.exec(select(ProjectAutomation).where(ProjectAutomation.project_id == pid)).first()
        lane = row.run_connection_id
    assert lane != conn.id, "the project runs on its own copy of the connection"
    before = dict(cstore.get(conn.id).config)
    exc = _refused(cstore.patch_config, conn.id, {"target_locales": "de, fr, ja"})
    assert "managed by project" in str(exc) and "CL feeder project" in str(exc), exc
    assert getattr(exc, "status", 0) == 409, exc
    exc = _refused(cstore.update, conn.id, "", {"target_locales": "de"}, None)
    assert "managed by project" in str(exc), exc
    assert cstore.get(conn.id).config["target_locales"] == before["target_locales"]
    assert _active(pid) == ["fr"], rows(pid)
    # other settings of the feeder still save
    cstore.update(conn.id, "CL feeder renamed", {"max_articles": "5"}, None)
    assert cstore.get(conn.id).name == "CL feeder renamed"
    # the project's own copy (lane) is the project's: an edit there flows into the project
    cstore.patch_config(lane, {"target_locales": "fr, de"})
    assert _active(pid) == ["fr", "de"], rows(pid)
    print("  feeder: a language edit on a connection other projects copy is refused, named")


def test_connection_language_web_card():
    """The connection page's Languages card (sign-in off here): a bound connection's change
    reaches the project; a feeder's is refused with the reason in the page message."""
    c = TestClient(app)
    conn, _ = zd("CL web card", target_locales="fr")
    pid = mkproj("CL web card", "fr")
    pa.bind(pid, conn.id, is_admin=True)
    r = c.post(
        f"/connectors/connection/{conn.id}/languages",
        data={"language_set_id": "", "target_locales": "fr, de"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers
    assert _active(pid) == ["fr", "de"], rows(pid)
    feeder, _ = zd("CL web feeder", target_locales="fr")
    fpid = mkproj("CL web feeder project", "fr")
    pa.bind(fpid, feeder.id, is_admin=True, clone=True)
    r = c.post(
        f"/connectors/connection/{feeder.id}/languages",
        data={"language_set_id": "", "target_locales": "fr, de"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "ok=0" in r.headers["location"], r.headers
    assert "managed%20by%20project" in r.headers["location"], r.headers["location"]
    assert cstore.get(feeder.id).config["target_locales"] == "fr"
    print("  web: the connection Languages card follows the same rule")


def test_connection_language_api_roles():
    """/api (and so the remote CLI): editing a connection is admin or linguist (unchanged); a
    language ADDED through it on a bound connection needs a manager or higher (decision 8), so a
    linguist may stop one but not add one."""
    from app.models import Role
    from app.services import apikeys, security, settings

    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        _api_roles_body(apikeys, security, Role)
    finally:
        settings.set_many({"security_auth_required": "0"})


def _api_roles_body(apikeys, security, Role):
    c = TestClient(app)
    admin = security.create_user("clfix-admin@x.co", "Fix Admin", Role.ADMIN, "pw-fixture-5521")
    ling = security.create_user("clfix-ling@x.co", "Fix Ling", Role.LINGUIST, "pw-fixture-5522")
    HA = {"X-Loc-Token": apikeys.create_key(admin.id, "fix admin", scopes="api docs")[1]}
    HL = {"X-Loc-Token": apikeys.create_key(ling.id, "fix ling", scopes="api docs")[1]}
    conn, _ = zd("CL api roles", target_locales="fr, de")
    pid = mkproj("CL api roles", "fr, de")
    pa.bind(pid, conn.id, is_admin=True)
    url = f"/api/connectors/{conn.id}/update"
    r = c.post(url, headers=HL, json={"config": {"target_locales": "fr, de, ja"}})
    assert r.status_code == 403 and "manager" in r.json()["error"], r.text
    assert _active(pid) == ["fr", "de"]
    r = c.post(url, headers=HL, json={"config": {"target_locales": "fr"}})
    assert r.status_code == 200, r.text
    assert rows(pid) == [("fr", False), ("de", True)], rows(pid)
    r = c.post(url, headers=HA, json={"config": {"target_locales": "fr, de, ja"}})
    assert r.status_code == 200, r.text
    assert _active(pid) == ["fr", "de", "ja"], rows(pid)
    assert cstore.get(conn.id).config["target_locales"] == "fr, de, ja", "connection follows"
    feeder, _ = zd("CL api feeder", target_locales="fr")
    fpid = mkproj("CL api feeder project", "fr")
    pa.bind(fpid, feeder.id, is_admin=True, clone=True)
    r = c.post(
        f"/api/connectors/{feeder.id}/update",
        headers=HA,
        json={"config": {"target_locales": "fr, de"}},
    )
    assert r.status_code == 409 and "managed by project" in r.json()["error"], r.text
    print("  api: linguist stops but cannot add on a bound connection; feeder refused 409")


# ---------------------------------------------------------------------------------------
# Review gate (FIX-locales): wire spelling kept, translate door spelling, page note, /api field
# ---------------------------------------------------------------------------------------
def test_connection_keeps_its_wire_spelling():
    """A connection names its languages in its system's spelling (Akeneo fr_FR). When the
    project's list is written back to it (bind, a language edit on either side) a language it
    already names keeps that spelling; only a new one takes the project's."""
    conn, _ = zd("CL wire keep", target_locales="fr_FR")
    pid = mkproj("CL wire keep", "fr-FR")
    pa.bind(pid, conn.id, is_admin=True)
    assert cstore.get(conn.id).config["target_locales"] == "fr_FR", cstore.get(conn.id).config
    # a language edit on the connection: the project gains de-DE, the connection keeps fr_FR
    cstore.patch_config(conn.id, {"target_locales": "fr_FR, de_DE"}, actor="t")
    assert _active(pid) == ["fr-FR", "de-DE"], rows(pid)
    assert cstore.get(conn.id).config["target_locales"] == "fr_FR, de_DE", cstore.get(
        conn.id
    ).config
    # a language added on the PROJECT: the connection keeps its spellings, the new one is the
    # project's
    with Session(engine) as s:
        content.add_target_locale(s, pid, "it-IT", actor="t")
    assert cstore.get(conn.id).config["target_locales"] == "fr_FR, de_DE, it-IT", cstore.get(
        conn.id
    ).config
    assert store_common.effective_locales(cstore.get(conn.id)) == ["fr_FR", "de_DE", "it-IT"]
    print("  wire spelling: the connection keeps fr_FR / de_DE when the project's list comes back")


def test_translate_door_uses_the_project_spelling():
    """The /api translate door (the MCP translate_document path): a target the project already
    has in another spelling is seeded, enqueued and translated in the project's spelling, so no
    second segment set appears."""
    from app.models import DocStatus, Role, TransMode
    from app.models import Document as Doc
    from app.services import apikeys, jobs, security, settings
    from app.web import api_discovery

    pid = mkproj("CL door spelling", "pt-BR")
    with Session(engine) as s:
        doc = Doc(
            project_id=pid,
            filename="door.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status=DocStatus.NEW,
            skeleton={},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(Segment(document_id=did, target_locale="pt-BR", order_index=0, source_text="Hi"))
        s.commit()
        assert api_discovery.project_locale_spelling(s, pid, "pt_br") == "pt-BR"
        assert api_discovery.project_locale_spelling(s, pid, "de") == "de"
    seen = []
    real = jobs.submit_budgeted

    def fake_submit(kind, payload, **_k):
        seen.append(payload.get("locale"))
        return 999001

    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    jobs.submit_budgeted = fake_submit
    try:
        admin = security.create_user("cl-door@x.co", "Door", Role.ADMIN, "pw-fixture-5531")
        hdr = {"X-Loc-Token": apikeys.create_key(admin.id, "door", scopes="api docs")[1]}
        r = TestClient(app).post(
            f"/api/documents/{did}/translate",
            headers=hdr,
            json={"targets": ["pt-br", "PT_BR"], "provider": "mock"},
        )
        assert r.status_code == 202, r.text
        assert [j["locale"] for j in r.json()["jobs"]] == ["pt-BR"], r.text
    finally:
        jobs.submit_budgeted = real
        settings.set_many({"security_auth_required": "0"})
    assert seen == ["pt-BR"], seen
    with Session(engine) as s:
        spellings = {
            sg.target_locale for sg in s.exec(select(Segment).where(Segment.document_id == did))
        }
    assert spellings == {"pt-BR"}, ("one segment set", spellings)
    assert rows(pid) == [("pt-BR", False)], rows(pid)
    print("  translate door: pt-br on a pt-BR project is translated as pt-BR, one segment set")


def test_connection_page_and_api_name_the_language_owner():
    """The connection page says whose languages these are (bound: the project, with a link;
    feeder: the projects that copy it); /api connection rows carry languages_project_id."""
    c = TestClient(app)
    conn, _ = zd("CL owner note", target_locales="fr")
    pid = mkproj("CL owner note project", "fr")
    pa.bind(pid, conn.id, is_admin=True)
    page = c.get(f"/connectors/connection/{conn.id}").text
    assert 'id="connection-languages-owner"' in page, "the note is on a bound connection"
    note = page.split('id="connection-languages-owner"', 1)[1].split("</p>", 1)[0]
    assert f'href="/projects/{pid}"' in note and "CL owner note project" in note, note
    assert chr(0x2014) not in note, "no em dash"
    feeder, _ = zd("CL owner feeder", target_locales="fr")
    fpid = mkproj("CL owner feeder project", "fr")
    pa.bind(fpid, feeder.id, is_admin=True, clone=True)
    page = c.get(f"/connectors/connection/{feeder.id}").text
    note = page.split('id="connection-languages-owner"', 1)[1].split("</p>", 1)[0]
    assert f'href="/projects/{fpid}"' in note and "refused" in note, note
    plain, _ = zd("CL owner plain", target_locales="fr")
    assert 'id="connection-languages-owner"' not in c.get(f"/connectors/connection/{plain.id}").text

    from app.models import Role
    from app.services import apikeys, security, settings

    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        admin = security.create_user("cl-owner@x.co", "Owner", Role.ADMIN, "pw-fixture-5541")
        hdr = {"X-Loc-Token": apikeys.create_key(admin.id, "owner", scopes="api docs")[1]}
        r = c.post(f"/api/connectors/{conn.id}/update", headers=hdr, json={"name": "CL owner 2"})
        assert r.status_code == 200 and r.json()["languages_project_id"] == pid, r.text
        r = c.post(f"/api/connectors/{plain.id}/update", headers=hdr, json={"name": "CL plain 2"})
        assert r.status_code == 200 and r.json()["languages_project_id"] is None, r.text
        listed = {x["id"]: x for x in c.get("/api/connectors", headers=hdr).json()["connections"]}
        assert listed[conn.id]["languages_project_id"] == pid, listed[conn.id]
        assert listed[feeder.id]["languages_project_id"] is None, listed[feeder.id]
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  connection page note and /api languages_project_id name the project")


def main():
    init_db()
    test_add_language_runs_only_new_pairs()
    test_stop_language_keeps_everything_and_releases_claims()
    test_readd_runs_the_pairs_again()
    test_unbound_connection_and_plain_project_unchanged()
    test_rules_around_a_stopped_language()
    test_jira_own_loop_honours_project_languages()
    test_airtable_columns_of_a_stopped_language_are_skipped()
    test_one_row_per_language_on_every_path()
    test_first_pull_wire_spelling_makes_one_row()
    test_connection_language_edit_flows_into_adopted_project()
    test_feeder_connection_language_edit_refused()
    test_connection_language_web_card()
    test_surfaces()
    test_connection_language_api_roles()
    test_connection_keeps_its_wire_spelling()
    test_translate_door_uses_the_project_spelling()
    test_connection_page_and_api_name_the_language_owner()
    test_key_project_stopped_language_stays_exportable()
    print("verify_connected_languages OK")


if __name__ == "__main__":
    main()
