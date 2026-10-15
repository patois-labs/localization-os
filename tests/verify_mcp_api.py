"""Inbound document API for the Claude Desktop / MCP front door (offline).

Phase A, Increment 1 — the API foundation (no `mcp` SDK dependency; every path
exercised via FastAPI TestClient):

  - scoped tokens: a plain "api" lsu_ key is 403 on every document route; a
    docs-scoped key is accepted; the shared lsk_ connector token is rejected
    (403/401) on every new document route
  - upload: happy path returns document_id/project_id/format/segments;
    UnsupportedFormat -> 415; an oversize body -> 413 (bounded read)
  - mock-engine HARD-FAIL: default provider "claude" with NO key resolves to the
    Mock placeholder -> POST translate returns 409 and enqueues ZERO jobs; with a
    live (stub) provider the same call fans out one job per target (202)
  - multi-locale fan-out count; object-level authz on translate + download + job
    poll (owner reads own translate job, a different user's docs-key cannot)
  - per-format download MIME (docx + json) with content round-trip
  - discovery: /api/languages + /api/formats shapes
"""

import os
import sys
import time
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpapi_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app, connector_api_token
from app.models import Job, JobStatus, Role
from app.services import apikeys, audit, security, settings
from app.services.providers import Provider, register_provider


def dx(paras):
    d = Docx()
    for p in paras:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


class LiveStubProvider(Provider):
    """A 'configured' engine for the happy path — provider_is_live() is True for
    any registered override, so this flips the mock-hard-fail off without a key."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "translate":
            payload = payload or {}
            return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip()


def _jobs_for_doc(doc_id):
    with Session(engine) as s:
        return s.exec(select(Job).where(Job.kind == "translate_document")).all()


def main() -> None:
    init_db()
    security.seed_initial()  # admin/admin + linguist + user1, auth ON
    settings.set_many({"audit_enabled": "1"})
    c = TestClient(app)

    admin = security.get_by_email("admin")
    user1 = security.get_by_email("user1")

    # Three credentials: a plain "api" key, a docs-scoped key (both admin's), and
    # the shared connector token. Keys are minted through the service so we never
    # need to fish a raw key out of a page.
    _k, api_only = apikeys.create_key(admin.id, "plain", scopes="api")
    _k, docs_key = apikeys.create_key(admin.id, "docs", scopes="api docs")
    _k, u1_docs = apikeys.create_key(user1.id, "u1 docs", scopes="api docs")
    conn_tok = connector_api_token()
    H_API = {"X-Loc-Token": api_only}
    H_DOCS = {"X-Loc-Token": docs_key}
    H_U1 = {"X-Loc-Token": u1_docs}
    H_CONN = {"X-Loc-Token": conn_tok}

    # scope persisted?
    assert "docs" in apikeys.list_keys(admin.id)[0].scopes or any(
        "docs" in k.scopes for k in apikeys.list_keys(admin.id)
    ), "docs scope not stored"
    print("  scoped token: create_key(scopes='api docs') persists the scope")

    # --- scope enforcement on every new document route --------------------
    doc_routes = [
        ("post", "/api/documents"),
        ("post", "/api/documents/1/translate"),
        ("get", "/api/documents/1/download?locale=fr-FR"),
        ("get", "/api/languages"),
        ("get", "/api/formats"),
    ]
    for method, path in doc_routes:
        # plain "api" key -> 403 (docs scope missing)
        r = getattr(c, method)(path, headers=H_API)
        assert r.status_code == 403, f"api-only key must 403 on {path}, got {r.status_code}"
        # shared connector token -> 403 (kind != user)
        r = getattr(c, method)(path, headers=H_CONN)
        assert r.status_code == 403, f"connector token must 403 on {path}, got {r.status_code}"
        # no token -> 401
        r = getattr(c, method)(path)
        assert r.status_code == 401, f"no token must 401 on {path}, got {r.status_code}"
    print("  scope gate: api-only -> 403, connector token -> 403, no token -> 401 (all doc routes)")

    # discovery works with a docs key
    r = c.get("/api/formats", headers=H_DOCS)
    assert r.status_code == 200 and r.json()["ok"], r.text
    fmts = {f["ext"] for f in r.json()["formats"]}
    assert "docx" in fmts and "json" in fmts and all("name" in f for f in r.json()["formats"]), (
        r.text
    )
    r = c.get("/api/languages", headers=H_DOCS)
    assert r.status_code == 200 and r.json()["ok"], r.text
    body = r.json()
    assert isinstance(body["language_sets"], list) and isinstance(body["locales"], list), body
    print(
        f"  discovery: /api/formats ({len(fmts)} exts) + /api/languages "
        f"({len(body['language_sets'])} sets) with a docs key -> 200"
    )

    # --- upload: happy path -----------------------------------------------
    files = {
        "file": (
            "hello.docx",
            dx(["First line.", "Second line."]),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    r = c.post("/api/documents", headers=H_DOCS, files=files)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] and body["document_id"] and body["project_id"], body
    assert body["format"] == "docx" and body["segments"] >= 2, body
    doc_id, project_id = body["document_id"], body["project_id"]
    print(
        f"  upload: docx -> document_id={doc_id} project_id={project_id} segments={body['segments']}"  # noqa: E501
    )

    # inbox project is per-identity, created_by the actor
    from app.models import Project

    with Session(engine) as s:
        proj = s.get(Project, project_id)
        assert proj.created_by == "admin" and "MCP Inbox" in proj.name, proj.name
    print("  inbox: no project_id -> per-identity 'MCP Inbox (admin)' created")

    # --- upload: unsupported format -> 415 --------------------------------
    r = c.post(
        "/api/documents",
        headers=H_DOCS,
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    assert r.status_code == 415 and r.json()["ok"] is False, (r.status_code, r.text)
    print("  upload: unsupported format -> 415 {ok:false}")

    # --- upload: oversize -> 413 ------------------------------------------
    import app.config as appcfg

    orig_cap = appcfg.MAX_UPLOAD_BYTES
    appcfg.MAX_UPLOAD_BYTES = 1024
    try:
        big = ("x" * 5000).encode()
        r = c.post("/api/documents", headers=H_DOCS, files={"file": ("big.txt", big, "text/plain")})
        assert r.status_code == 413 and r.json()["ok"] is False, (r.status_code, r.text)
    finally:
        appcfg.MAX_UPLOAD_BYTES = orig_cap
    print("  upload: body over MAX_UPLOAD_BYTES -> 413 {ok:false}")

    # --- mock-engine HARD-FAIL --------------------------------------------
    # The inbox project's default_provider is config.DEFAULT_PROVIDER ("claude"),
    # and there is NO key in this offline run, so it resolves to Mock -> 409 with
    # ZERO jobs enqueued.
    before = len(_jobs_for_doc(doc_id))
    r = c.post(
        f"/api/documents/{doc_id}/translate", headers=H_DOCS, json={"targets": ["fr-FR", "de-DE"]}
    )
    assert r.status_code == 409, (r.status_code, r.text)
    assert "translation engine" in r.json()["error"].lower(), r.text
    after = len(_jobs_for_doc(doc_id))
    assert after == before, f"mock-hard-fail must enqueue ZERO jobs (before={before} after={after})"
    print("  mock-hard-fail: default 'claude' + no key -> 409, zero jobs enqueued")

    # --- live provider: fan-out one job per target (202) ------------------
    register_provider("claude", LiveStubProvider())  # now provider_is_live -> True
    try:
        r = c.post(
            f"/api/documents/{doc_id}/translate",
            headers=H_DOCS,
            json={"targets": ["fr-FR", "de-DE", "ja-JP"]},
        )
        assert r.status_code == 202, (r.status_code, r.text)
        out = r.json()
        assert out["ok"] and len(out["jobs"]) == 3, out
        locs = {j["locale"] for j in out["jobs"]}
        assert locs == {"fr-FR", "de-DE", "ja-JP"}, out
        assert all(j["status_url"] == f"/api/jobs/{j['job_id']}" for j in out["jobs"]), out
        print(
            f"  fan-out: live engine -> 202 with {len(out['jobs'])} jobs "
            f"(one per target: {sorted(locs)})"
        )

        # PII NO LONGER forced on the API (behavior change 2026-07-03): the API
        # dropped the force_pii=True stamp. A translate_document job the API enqueues
        # must NOT force PII in its payload; inbound submissions are now scrubbed only
        # when the global pii_redaction setting is on (which it is not in this run),
        # and PII is a per-step choice on the workflow path (WorkflowStep.redact_pii).
        assert not settings.get("pii_redaction") == "1", "test assumes toggle off"  # noqa: SIM201
        with Session(engine) as s:
            api_jobs = s.exec(select(Job).where(Job.kind == "translate_document")).all()
            api_jobs = [j for j in api_jobs if (j.payload or {}).get("document_id") == doc_id]
        assert api_jobs, "no translate jobs found for the API-submitted doc"
        assert all((j.payload or {}).get("force_pii") is None for j in api_jobs), (
            "API translate jobs must NOT force PII (force_pii stamp was dropped)"
        )
        print(
            "  PII not forced: API translate job payloads carry no force_pii (blanket "
            "global toggle governs; per-step redact on the workflow path)"
        )

        # let one job finish so download has content
        fr_job = next(j for j in out["jobs"] if j["locale"] == "fr-FR")["job_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            d = c.get(f"/api/jobs/{fr_job}", headers=H_DOCS).json()
            if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            time.sleep(0.05)
        assert d["status"] == JobStatus.COMPLETED, d
        print("  job poll: owner reads own translate_document job via GET /api/jobs/{id}")

        # object-level authz on the job: user1's docs-key must NOT read admin's job
        r = c.get(f"/api/jobs/{fr_job}", headers=H_U1)
        assert r.status_code == 404, f"cross-user job read must 404, got {r.status_code}"
        print("  job authz: a different user's docs-key -> 404 on the owner's job")

        # --- per-format download MIME + round-trip (docx) -----------------
        r = c.get(f"/api/documents/{doc_id}/download?locale=fr-FR", headers=H_DOCS)
        assert r.status_code == 200, (r.status_code, r.text)
        ct = r.headers["content-type"]
        assert "wordprocessingml.document" in ct, f"docx MIME wrong: {ct}"
        assert "attachment" in r.headers["content-disposition"], r.headers
        assert r.content[:2] == b"PK", "docx download is not a zip/docx"
        print(f"  download docx: MIME {ct.split(';')[0]} + attachment + valid content")

        # the shared MIME map must cover every filter format so exports/downloads
        # never fall back to octet-stream for a supported type. XLSX shipped a
        # filter but was initially missing from the map (QA finding, 2026-07-02).
        from app.web.deps import mime_for_fmt

        assert (
            mime_for_fmt("xlsx")
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ), f"xlsx MIME missing/wrong: {mime_for_fmt('xlsx')}"
        assert mime_for_fmt("docx").startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml"
        ), "docx MIME"
        print("  MIME map covers xlsx (no octet-stream fallback for a supported format)")

        # object-level authz on download: with project_scoping ON, user1 (a
        # non-owner, non-member TRANSLATOR) is genuinely scoped out -> 404.
        settings.set_many({"project_scoping": "1"})
        try:
            r = c.get(f"/api/documents/{doc_id}/download?locale=fr-FR", headers=H_U1)
            assert r.status_code == 404, f"scoped-out download must 404, got {r.status_code}"
        finally:
            settings.set_many({"project_scoping": "0"})
        # missing locale -> 404
        r = c.get(f"/api/documents/{doc_id}/download?locale=zz-ZZ", headers=H_DOCS)
        assert r.status_code == 404, r.status_code
        print("  download authz: scoped-out -> 404; unknown locale -> 404")

        # --- authz consistency (Fix 1): a project MEMBER can download ------
        # user1 is NOT the owner and NOT a manager, but IS a ProjectMember of
        # admin's inbox project -> can_view_project True -> download works even
        # with project_scoping ON (was a spurious 404 under the old created_by-
        # only rule). Also: with scoping OFF (the default), a plain authenticated
        # user could already view, so the API must match the UI either way.
        from app.models import ProjectMember

        with Session(engine) as s:
            s.add(ProjectMember(project_id=project_id, user_id=user1.id, added_by="admin"))
            s.commit()
        settings.set_many({"project_scoping": "1"})
        try:
            r = c.get(f"/api/documents/{doc_id}/download?locale=fr-FR", headers=H_U1)
            assert r.status_code == 200, (
                f"member download must 200 (Fix 1), got {r.status_code}: {r.text}"
            )
            assert r.content[:2] == b"PK", "member download is not a valid docx"
        finally:
            settings.set_many({"project_scoping": "0"})
        print("  authz consistency: a non-owner project MEMBER can download (was 404)")

        # --- per-format download MIME (json) + round-trip -----------------
        jdata = b'{"greeting": "Hello world", "farewell": "Goodbye"}'
        r = c.post(
            "/api/documents",
            headers=H_DOCS,
            files={"file": ("strings.json", jdata, "application/json")},
        )
        assert r.status_code == 200, r.text
        jdoc = r.json()["document_id"]
        r = c.post(
            f"/api/documents/{jdoc}/translate", headers=H_DOCS, json={"target_locales": ["es-ES"]}
        )  # alias key accepted
        assert r.status_code == 202, r.text
        jjob = r.json()["jobs"][0]["job_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            d = c.get(f"/api/jobs/{jjob}", headers=H_DOCS).json()
            if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            time.sleep(0.05)
        assert d["status"] == JobStatus.COMPLETED, d
        r = c.get(f"/api/documents/{jdoc}/download?locale=es-ES", headers=H_DOCS)
        assert r.status_code == 200, (r.status_code, r.text)
        ct = r.headers["content-type"]
        assert ct.startswith("application/json"), f"json MIME wrong: {ct}"
        assert b"[es-ES]" in r.content, f"translated json missing stub marker: {r.content[:120]!r}"
        print(f"  download json: MIME {ct.split(';')[0]} + translated content round-trips")
    finally:
        # restore the offline (mock) engine so nothing leaks into later suites
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)

    # --- translate object-level authz (Fix 1: member vs. scoped-out) -------
    # A second translator with NO membership is the genuinely-scoped-out case.
    user2 = security.create_user("user2", "Standard User 2", Role.TRANSLATOR, "user2-pw-1234")
    _k, u2_docs = apikeys.create_key(user2.id, "u2 docs", scopes="api docs")
    H_U2 = {"X-Loc-Token": u2_docs}
    # (re-register live provider briefly so we reach the authz check, not 409)
    register_provider("claude", LiveStubProvider())
    try:
        # A genuinely scoped-out user (auth on + project_scoping on + not a member)
        # still gets 404 -- no privilege escalation.
        settings.set_many({"project_scoping": "1"})
        try:
            r = c.post(
                f"/api/documents/{doc_id}/translate", headers=H_U2, json={"targets": ["it-IT"]}
            )
            assert r.status_code == 404, f"scoped-out translate must 404, got {r.status_code}"
        finally:
            settings.set_many({"project_scoping": "0"})
        print("  translate authz: a genuinely scoped-out user -> 404 on the owner's doc")

        # A non-owner project MEMBER can translate the same doc even with
        # project_scoping ON (was a spurious 404 under created_by-only authz).
        settings.set_many({"project_scoping": "1"})
        try:
            r = c.post(
                f"/api/documents/{doc_id}/translate", headers=H_U1, json={"targets": ["it-IT"]}
            )
            assert r.status_code == 202, (
                f"member translate must 202 (Fix 1), got {r.status_code}: {r.text}"
            )
            assert r.json()["ok"] and len(r.json()["jobs"]) == 1, r.text
        finally:
            settings.set_many({"project_scoping": "0"})
        print("  authz consistency: a non-owner project MEMBER can translate (was 404)")

        # --- dedupe (Fix 2): duplicate targets + in-flight overlap guard ---
        # A fresh doc keeps this isolated from doc_id's in-flight it-IT job.
        r = c.post(
            "/api/documents",
            headers=H_DOCS,
            files={
                "file": (
                    "dedupe.docx",
                    dx(["Only line."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, r.text
        ddoc = r.json()["document_id"]
        # (a) {"targets": ["fr-FR", "fr-FR"]} -> exactly ONE job, no skips.
        r = c.post(
            f"/api/documents/{ddoc}/translate", headers=H_DOCS, json={"targets": ["fr-FR", "fr-FR"]}
        )
        assert r.status_code == 202, r.text
        out = r.json()
        assert len(out["jobs"]) == 1 and out["jobs"][0]["locale"] == "fr-FR", out
        assert out.get("skipped") == [], out
        print("  dedupe: {targets:['fr-FR','fr-FR']} -> ONE job, none skipped")

        # (b) a second call for the same (doc, locale) while a translate job is
        # QUEUED/RUNNING enqueues NONE and reports it skipped. To make this
        # deterministic (the LiveStub finishes almost instantly), seed a QUEUED
        # translate_document job for a NEW doc directly in the DB -- it is never
        # put on the in-memory worker queue, so it stays QUEUED and the overlap
        # guard reliably sees it as active.
        r = c.post(
            "/api/documents",
            headers=H_DOCS,
            files={
                "file": (
                    "overlap.docx",
                    dx(["Only line."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, r.text
        odoc = r.json()["document_id"]
        with Session(engine) as s:
            s.add(
                Job(
                    kind="translate_document",
                    payload={"document_id": odoc, "locale": "de-DE"},
                    status=JobStatus.QUEUED,
                    created_by="admin",
                )
            )
            s.commit()
        n_before = len(_jobs_for_doc(odoc))
        r = c.post(f"/api/documents/{odoc}/translate", headers=H_DOCS, json={"targets": ["de-DE"]})
        assert r.status_code == 202, r.text
        out = r.json()
        n_after = len(_jobs_for_doc(odoc))
        assert n_after == n_before, (
            f"overlap guard must enqueue 0 new de-DE jobs (before={n_before} after={n_after})"
        )
        assert out["jobs"] == [] and out.get("skipped") == ["de-DE"], out
        print("  dedupe: second call while (doc,de-DE) active -> 0 jobs, skipped=['de-DE']")
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)

    # --- orphan doc on ingest failure (Fix 3) -----------------------------
    # Force the post-commit work to fail (storage.write_upload raises) AFTER the
    # Document row has committed. ingest_document must clean the just-created doc
    # (+ any seeded segments) up before re-raising, so no orphan row is left.
    from app.models import Document
    from app.services import storage as _storage

    def _boom(path, data):
        raise OSError("disk full (forced)")

    with Session(engine) as s:
        docs_before = len(s.exec(select(Document.id)).all())
    _orig_write = _storage.write_upload
    _storage.write_upload = _boom
    try:
        r = c.post(
            "/api/documents",
            headers=H_DOCS,
            files={
                "file": (
                    "orphan.docx",
                    dx(["Will fail to store."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        # The API's corrupt/failed-ingest handler maps the raise to 422.
        assert r.status_code == 422 and r.json()["ok"] is False, (r.status_code, r.text)
    finally:
        _storage.write_upload = _orig_write
    with Session(engine) as s:
        docs_after = len(s.exec(select(Document.id)).all())
    assert docs_after == docs_before, (
        f"ingest failure left an orphan Document row (before={docs_before} after={docs_after})"
    )
    print("  orphan: ingest failure after doc-commit leaves NO orphan Document row")

    # =====================================================================
    # AUDIT + SIZE-CAP REGRESSION (Phase B chunk 6)
    #
    # Confirms the Phase-A audit/size-cap guarantees stay intact:
    #   - every API upload/translate/download writes an audit row that carries BOTH
    #     the actor email AND a non-empty ip (§6b item 15's "GETs are never audited"
    #     is stale — the download route audits)
    #   - the 413 size cap still fires on an oversize upload body
    # These ran earlier in the happy path; here we assert the audit side-effects.
    # =====================================================================
    def _rows(action):
        return [r for r in audit.iter_export(action=action)]  # noqa: C416

    for action in ("document.upload", "document.translate", "document.download"):
        rows = _rows(action)
        assert rows, f"no audit rows for {action} (upload/translate/download must audit)"
        for r in rows:
            assert (r["actor"] or "").strip(), f"{action} audit row missing actor: {r}"
            assert r["actor"] != "system", (
                f"{action} audit actor should be the key holder, not system: {r}"
            )
            assert (r["ip"] or "").strip(), f"{action} audit row has empty ip: {r}"
    # the actor on these rows is the docs-key holder ("admin"), not "system"
    up = _rows("document.upload")
    assert any(r["actor"] == "admin" for r in up), ("upload actor should be the key holder", up)
    print("  audit regression: upload/translate/download rows carry actor AND non-empty ip")

    # size cap still fires (413) — re-assert independently of the happy path
    import app.config as _cfg

    _orig = _cfg.MAX_UPLOAD_BYTES
    _cfg.MAX_UPLOAD_BYTES = 512
    try:
        r = c.post(
            "/api/documents",
            headers=H_DOCS,
            files={"file": ("big2.txt", ("y" * 4000).encode(), "text/plain")},
        )
        assert r.status_code == 413 and r.json()["ok"] is False, (r.status_code, r.text)
    finally:
        _cfg.MAX_UPLOAD_BYTES = _orig
    print("  size-cap regression: oversize upload body -> 413 (bounded read intact)")

    settings.set_many({"security_auth_required": "0"})
    print("MCP_API OK")


if __name__ == "__main__":
    main()
