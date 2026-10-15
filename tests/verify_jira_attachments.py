"""Jira OUTBOUND attachments verification (SPEC-attachments Phase A) — fully
offline via httpx.MockTransport + the deterministic MockProvider.

Covers the tunnel-free half only (the Airtable/inbound half is Phase B, gated):

  1. download -> filter-translate -> upload round-trip on DOCX and HTML fixtures;
     translated file re-attached as {stem}.{locale}.{ext}, additive.
  2. the X-Atlassian-Token: no-check header + multipart Content-Type on upload.
  3. extension allowlist (attachment_types) + MAX_ATTACHMENT_BYTES size guard +
     unsupported-format skip (skip-not-fail, never written blind).
  4. content-hash suppression: an unchanged attachment is not re-translated.
  5. WriteBudget ceiling: attachment uploads count against MAX_WRITES_PER_RUN.
  6. refusal-flagged content: a NEEDS_REVIEW segment falls back to SOURCE text in
     the rebuilt file — no assistant hedge ever lands (the choke point never
     commits one), and nothing unreviewed is attached either.
  7. echo/loop guard: an attachment WE authored (author.accountId == ours) OR named
     like our own output ({stem}.{locale}.{ext}) is never re-pulled as source work.
  8. org isolation: cached attachment documents land in the connection's project.
  9. CLI smoke: `connector jira-translate` honors attachment_translate.

Run standalone: python tests/verify_jira_attachments.py  ->  prints "... OK".
"""

import json
import os
import re
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_jira_att_")

import httpx

from app import config
from app.db import engine, init_db
from app.services.connectors import store as cstore

OUR_ACCT = "loc-bot-accountid"
BASE = "https://team.atlassian.net"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _docx_bytes(paragraphs) -> bytes:
    from docx import Document as D

    doc = D()
    for p in paragraphs:
        doc.add_paragraph(p)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _docx_paras(data: bytes) -> list:
    from docx import Document as D

    return [p.text for p in D(BytesIO(data)).paragraphs if p.text.strip()]


def _html_bytes(body_texts) -> bytes:
    inner = "".join(f"<p>{t}</p>" for t in body_texts)
    return (f"<html><head><title>t</title></head><body>{inner}</body></html>").encode()


def _parse_multipart(content: bytes, content_type: str):
    """Extract (filename, file_bytes) from a multipart/form-data body (the `file`
    part httpx builds for an attachment upload)."""
    if "boundary=" not in content_type:
        return "", b""
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode()
    for part in content.split(b"--" + boundary):
        if b"Content-Disposition" in part and b'name="file"' in part:
            header, _, body = part.partition(b"\r\n\r\n")
            m = re.search(rb'filename="([^"]*)"', header)
            fname = m.group(1).decode() if m else ""
            body = body.rsplit(b"\r\n", 1)[0]  # strip the trailing CRLF before the boundary
            return fname, body
    return "", b""


class FakeJira:
    """A minimal offline Jira tenant: JQL search, /myself, attachment metadata +
    content download, and multipart attachment upload capture."""

    def __init__(self, issues, attachments):
        # issues: {key: {"summary": str, "description": str|None}}
        # attachments: {key: [ {id, filename, mime, bytes, author_id, size?} ]}
        self.issues = issues
        self.attachments = attachments
        self.uploads = []  # {issue, filename, bytes, no_check, content_type}
        self.comments = []
        self.myself_available = True
        self.downloads = []  # attachment ids actually fetched

    def transport(self):
        def handler(req):
            path, method = req.url.path, req.method
            if path == "/rest/api/3/myself":
                if not self.myself_available:
                    return httpx.Response(503, text="down")
                return httpx.Response(200, json={"accountId": OUR_ACCT, "displayName": "Loc Bot"})
            if path == "/rest/api/3/search/jql":
                issues = [
                    {"key": k, "fields": {kk: vv for kk, vv in v.items()}}  # noqa: C416
                    for k, v in self.issues.items()
                ]
                return httpx.Response(200, json={"isLast": True, "issues": issues})
            m = re.match(r"^/rest/api/3/issue/([^/]+)$", path)
            if m and method == "GET" and req.url.params.get("fields") == "attachment":
                key = m.group(1)
                arr = []
                for a in self.attachments.get(key, []):
                    arr.append(
                        {
                            "id": a["id"],
                            "filename": a["filename"],
                            "mimeType": a["mime"],
                            "size": a.get("size", len(a["bytes"])),
                            "content": f"{BASE}/rest/api/3/attachment/content/{a['id']}",
                            "author": {"accountId": a.get("author_id", "")},
                        }
                    )
                return httpx.Response(200, json={"fields": {"attachment": arr}})
            mc = re.match(r"^/rest/api/3/attachment/content/(.+)$", path)
            if mc and method == "GET":
                aid = mc.group(1)
                self.downloads.append(aid)
                for arr in self.attachments.values():
                    for a in arr:
                        if a["id"] == aid:
                            return httpx.Response(200, content=a["bytes"])
                return httpx.Response(404)
            mu = re.match(r"^/rest/api/3/issue/([^/]+)/attachments$", path)
            if mu and method == "POST":
                fname, body = _parse_multipart(req.content, req.headers.get("content-type", ""))
                self.uploads.append(
                    {
                        "issue": mu.group(1),
                        "filename": fname,
                        "bytes": body,
                        "no_check": req.headers.get("x-atlassian-token", ""),
                        "content_type": req.headers.get("content-type", ""),
                    }
                )
                return httpx.Response(
                    200, json=[{"id": f"up-{len(self.uploads)}", "filename": fname}]
                )
            mco = re.match(r"^/rest/api/3/issue/([^/]+)/comment$", path)
            if mco and method == "POST":
                self.comments.append(json.loads(req.content))
                return httpx.Response(201, json={"id": str(len(self.comments))})
            return httpx.Response(404, json={"path": path, "method": method})

        return httpx.MockTransport(handler)


def _conn(name, cfg_extra=None):
    cfg = {
        "base_url": BASE,
        "jql": "project = LOC",
        "source_field": "summary",
        "target_locales": "es-MX",
        "source_locale": "en-US",
        "attachment_translate": "1",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("jira", name, cfg, {"email": "b@x.com", "api_token": "tok"}, actor="admin")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_docx_roundtrip():
    src = _docx_bytes(["Hello world.", "Second paragraph here."])
    fake = FakeJira(
        {"LOC-1": {"summary": "A ticket"}},
        {
            "LOC-1": [
                {
                    "id": "10001",
                    "filename": "guide.docx",
                    "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # noqa: E501
                    "bytes": src,
                    "author_id": "human-1",
                }
            ]
        },
    )
    conn = _conn("att docx")
    ok, msg, summary = cstore.jira_translate(conn.id, actor="auto", transport=fake.transport())
    assert ok, msg
    assert summary["attachments_written"] == 1, summary
    assert len(fake.uploads) == 1, fake.uploads
    up = fake.uploads[0]
    assert up["filename"] == "guide.es-MX.docx", up["filename"]
    assert up["no_check"] == "no-check", ("X-Atlassian-Token: no-check required on upload", up)
    assert up["content_type"].startswith("multipart/form-data"), up["content_type"]
    paras = _docx_paras(up["bytes"])
    assert paras and all(p.startswith("[es-MX]") for p in paras), (
        "every paragraph translated",
        paras,
    )
    assert any("Hello world" in p for p in paras), paras
    print(
        "  docx: download -> translate -> re-attach as {stem}.{loc}.docx (no-check header, multipart)"  # noqa: E501
    )


def test_html_roundtrip():
    src = _html_bytes(["Welcome to the portal.", "Please sign in below."])
    fake = FakeJira(
        {"LOC-2": {"summary": "A ticket"}},
        {
            "LOC-2": [
                {
                    "id": "20001",
                    "filename": "page.html",
                    "mime": "text/html",
                    "bytes": src,
                    "author_id": "human-1",
                }
            ]
        },
    )
    conn = _conn("att html")
    ok, msg, summary = cstore.jira_translate(conn.id, actor="auto", transport=fake.transport())
    assert ok, msg
    assert summary["attachments_written"] == 1 and len(fake.uploads) == 1, (summary, fake.uploads)
    up = fake.uploads[0]
    assert up["filename"] == "page.es-MX.html", up["filename"]
    text = up["bytes"].decode("utf-8")
    assert "[es-MX]" in text and "Welcome to the portal" in text and "<html" in text.lower(), text[
        :200
    ]
    print("  html: download -> translate -> re-attach as {stem}.{loc}.html (tags preserved)")


def test_allowlist_and_unsupported_and_size():
    docx = _docx_bytes(["Only this one is translatable."])
    html = _html_bytes(["This html is NOT in the allowlist."])
    fake = FakeJira(
        {"LOC-3": {"summary": "A ticket"}},
        {
            "LOC-3": [
                {
                    "id": "1",
                    "filename": "keep.docx",
                    "mime": "application/octet-stream",
                    "bytes": docx,
                    "author_id": "h",
                },
                {
                    "id": "2",
                    "filename": "skip.html",
                    "mime": "text/html",
                    "bytes": html,
                    "author_id": "h",
                },
                {
                    "id": "3",
                    "filename": "archive.zip",
                    "mime": "application/zip",
                    "bytes": b"PK\x03\x04nope",
                    "author_id": "h",
                },
            ]
        },
    )
    # allowlist restricts to .docx only.
    conn = _conn("att allowlist", {"attachment_types": ".docx"})
    ok, msg, summary = cstore.jira_translate(conn.id, actor="auto", transport=fake.transport())
    assert ok, msg
    assert summary["attachments_written"] == 1, summary
    assert [u["filename"] for u in fake.uploads] == ["keep.es-MX.docx"], fake.uploads
    # both the not-allowlisted html and the unsupported zip are skipped, not failed.
    assert summary["attachments_skipped"] >= 2, summary
    assert "2" not in fake.downloads and "3" not in fake.downloads, (
        "a skipped-by-type attachment is never even downloaded",
        fake.downloads,
    )
    print(
        "  allowlist/unsupported: .docx kept, .html (not allowlisted) + .zip (unsupported) skipped"
    )

    # size guard: a metadata-oversized attachment is skipped BEFORE download.
    big = _docx_bytes(["big"])
    fake2 = FakeJira(
        {"LOC-4": {"summary": "A ticket"}},
        {
            "LOC-4": [
                {
                    "id": "9",
                    "filename": "huge.docx",
                    "mime": "application/octet-stream",
                    "bytes": big,
                    "size": 99_000_000,
                    "author_id": "h",
                }
            ]
        },
    )
    conn2 = _conn("att size")
    orig = config.MAX_ATTACHMENT_BYTES
    config.MAX_ATTACHMENT_BYTES = 1000
    try:
        res = cstore._jira_translate_attachments(
            conn2.id, ["LOC-4"], locales=["es-MX"], transport=fake2.transport()
        )
    finally:
        config.MAX_ATTACHMENT_BYTES = orig
    assert res["attachments_written"] == 0 and res["attachments_skipped"] >= 1, res
    assert fake2.downloads == [], (
        "over-cap attachment skipped by metadata BEFORE download",
        fake2.downloads,
    )
    print("  size guard: over-MAX_ATTACHMENT_BYTES attachment skipped before download")


def test_image_exts_excluded_from_blank_default():
    """Adversarial review (image-localization Phase 1): ImageFilter registers
    png/jpg/jpeg/svg into the filter registry at import, so a blank attachment_types
    ('all supported') must NOT silently start pulling image attachments into a LIVE
    connector run — that is Phase 3, opt-in only. Blank => images excluded, real doc
    types still admitted; an EXPLICIT image ext => admitted (the Phase 3 mechanism)."""
    from app.services.image_localization import IMAGE_EXTS

    blank = cstore._attachment_allowlist({"attachment_types": ""})
    assert not (blank & set(IMAGE_EXTS)), (
        "blank attachment_types must not admit image types",
        blank & set(IMAGE_EXTS),
    )
    # Non-image supported formats are unchanged (pre-branch behavior preserved).
    assert "docx" in blank and "html" in blank, blank
    # Explicit opt-in still works (Phase 3 enablement is an explicit allowlist entry).
    explicit = cstore._attachment_allowlist({"attachment_types": ".png, .docx"})
    assert "png" in explicit and "docx" in explicit, explicit
    assert "jpeg" not in explicit, explicit  # only what was explicitly listed
    print(
        "  image exts: excluded from the blank 'all supported' default; explicit "
        "attachment_types entry still opts an image type in (Phase 3)"
    )


def test_hash_suppression():
    src = _docx_bytes(["Suppress me on the second run."])
    fake = FakeJira(
        {"LOC-5": {"summary": "A ticket"}},
        {
            "LOC-5": [
                {
                    "id": "5000",
                    "filename": "doc.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "h",
                }
            ]
        },
    )
    conn = _conn("att suppress")
    r1 = cstore._jira_translate_attachments(
        conn.id, ["LOC-5"], locales=["es-MX"], transport=fake.transport()
    )
    assert r1["attachments_written"] == 1, r1
    n_after_1 = len(fake.uploads)
    # Second run, unchanged bytes -> content-hash suppresses; no new upload.
    r2 = cstore._jira_translate_attachments(
        conn.id, ["LOC-5"], locales=["es-MX"], transport=fake.transport()
    )
    assert r2["attachments_written"] == 0 and r2["attachments_suppressed"] >= 1, r2
    assert len(fake.uploads) == n_after_1, ("unchanged attachment not re-uploaded", fake.uploads)
    print("  suppression: unchanged attachment content-hash suppresses on re-run (no re-attach)")


def test_write_ceiling():
    fake = FakeJira(
        {"LOC-6": {"summary": "A ticket"}},
        {
            "LOC-6": [
                {
                    "id": f"c{i}",
                    "filename": f"f{i}.docx",
                    "mime": "application/octet-stream",
                    "bytes": _docx_bytes([f"File {i} body."]),
                    "author_id": "h",
                }
                for i in range(4)
            ]
        },
    )
    conn = _conn("att ceiling")
    orig = config.MAX_WRITES_PER_RUN
    config.MAX_WRITES_PER_RUN = 2
    try:
        res = cstore._jira_translate_attachments(
            conn.id, ["LOC-6"], locales=["es-MX"], transport=fake.transport()
        )
    finally:
        config.MAX_WRITES_PER_RUN = orig
    assert res["attachments_written"] == 2, (
        "exactly 2 uploads under a ceiling of 2 with 4 pending",
        res,
    )
    assert len(fake.uploads) == 2, fake.uploads
    assert res.get("attachments_ceiling_hit") is True, res
    print("  ceiling: attachment uploads count against MAX_WRITES_PER_RUN (2 of 4 with ceiling=2)")


def test_refusal_fallback_to_source():
    import app.services.refusal as refusal

    src = _docx_bytes(["A normal line.", "This one has REFUSE_ME in it."])
    fake = FakeJira(
        {"LOC-7": {"summary": "A ticket"}},
        {
            "LOC-7": [
                {
                    "id": "7000",
                    "filename": "mix.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "h",
                }
            ]
        },
    )
    conn = _conn("att refusal")
    real_is_refusal = refusal.is_refusal
    real_guard = config.REFUSAL_GUARD_ENABLED
    set_pieces("app.services.refusal", "is_refusal", lambda text: "REFUSE_ME" in (text or ""))
    config.REFUSAL_GUARD_ENABLED = True
    try:
        res = cstore._jira_translate_attachments(
            conn.id, ["LOC-7"], locales=["es-MX"], transport=fake.transport()
        )
    finally:
        set_pieces("app.services.refusal", "is_refusal", real_is_refusal)
        config.REFUSAL_GUARD_ENABLED = real_guard
    assert res["attachments_written"] == 1 and len(fake.uploads) == 1, (res, fake.uploads)
    paras = _docx_paras(fake.uploads[0]["bytes"])
    # The normal line is translated; the refused line falls back to its exact SOURCE
    # text — NO "[es-MX]" prefix and, critically, NO assistant hedge (the choke point
    # never commits one to target_text).
    normal = [p for p in paras if "normal line" in p.lower()]
    refused = [p for p in paras if "REFUSE_ME" in p]
    assert normal and normal[0].startswith("[es-MX]"), ("non-refused line translated", paras)
    assert refused and refused[0] == "This one has REFUSE_ME in it.", (
        "refused segment reverts to SOURCE, no hedge/no [es-MX]",
        paras,
    )
    assert not any("I'm ready to translate" in p or "please provide" in p.lower() for p in paras), (
        "no hedge text ever written to a customer-facing attachment",
        paras,
    )
    print("  refusal: NEEDS_REVIEW segment reverts to source in the file; no hedge, coherent doc")


def test_loop_guard():
    src = _docx_bytes(["Translate the source, not our own output."])
    ours = _docx_bytes(["already translated by us"])
    fake = FakeJira(
        {"LOC-8": {"summary": "A ticket"}},
        {
            "LOC-8": [
                {
                    "id": "src",
                    "filename": "report.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "human-1",
                },
                # Our OWN previously-uploaded translation (authored by us). Must NOT be
                # re-translated (would echo report.es-MX.es-MX.docx forever).
                {
                    "id": "mine",
                    "filename": "report.es-MX.docx",
                    "mime": "application/octet-stream",
                    "bytes": ours,
                    "author_id": OUR_ACCT,
                },
            ]
        },
    )
    conn = _conn("att loopguard")
    res = cstore._jira_translate_attachments(
        conn.id, ["LOC-8"], locales=["es-MX"], transport=fake.transport()
    )
    names = [u["filename"] for u in fake.uploads]
    assert names == ["report.es-MX.docx"], (
        "only the source is translated (authorship guard)",
        names,
    )
    assert "mine" not in fake.downloads, (
        "our own attachment is never even downloaded",
        fake.downloads,
    )
    assert not any(n == "report.es-MX.es-MX.docx" for n in names), (
        "no echo/self re-translation",
        names,
    )
    assert res["attachments_written"] == 1, res
    print("  loop-guard (authorship): our own uploaded translation is never re-pulled as source")

    # Degraded fallback: /myself unavailable (no accountId) -> the filename-pattern
    # guard still skips a file shaped like our output.
    fake2 = FakeJira(
        {"LOC-9": {"summary": "A ticket"}},
        {
            "LOC-9": [
                {
                    "id": "s2",
                    "filename": "note.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "human-1",
                },
                {
                    "id": "m2",
                    "filename": "note.es-MX.docx",
                    "mime": "application/octet-stream",
                    "bytes": ours,
                    "author_id": "unknown",
                },
            ]
        },
    )
    fake2.myself_available = False
    conn2 = _conn("att loopguard degraded")
    cstore._jira_translate_attachments(
        conn2.id, ["LOC-9"], locales=["es-MX"], transport=fake2.transport()
    )
    names2 = [u["filename"] for u in fake2.uploads]
    assert names2 == ["note.es-MX.docx"], (
        "filename fallback skips our-shaped file when /myself down",
        names2,
    )
    print(
        "  loop-guard (filename fallback): {stem}.{loc}.{ext} skipped when /myself is unavailable"
    )


def test_org_isolation():
    from sqlmodel import Session, select

    from app.models import Document, Project

    src = _docx_bytes(["Isolation check."])
    fake = FakeJira(
        {"LOC-10": {"summary": "A ticket"}},
        {
            "LOC-10": [
                {
                    "id": "iso",
                    "filename": "iso.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "h",
                }
            ]
        },
    )
    conn = _conn("att isolation")
    cstore._jira_translate_attachments(
        conn.id, ["LOC-10"], locales=["es-MX"], transport=fake.transport()
    )
    refreshed = cstore.get(conn.id)
    assert refreshed.project_id, "attachment run must bind the connection's project"
    with Session(engine) as s:
        docs = s.exec(select(Document).where(Document.fmt == "docx")).all()
        att_docs = [d for d in docs if d.filename == "iso.docx"]
        # The cached attachment Document lives in the connection's PROJECT and survives
        # a connection delete; full erasure = delete that project (see test_delete_*).
        assert att_docs, "the cached attachment document must exist in the connection's project"
        for d in att_docs:
            assert d.project_id == refreshed.project_id, (
                "attachment document lands in the connection's own project (org-scoped)",
                d.project_id,
            )
        proj = s.get(Project, refreshed.project_id)
        assert getattr(proj, "org_id", None) == getattr(refreshed, "org_id", None), (
            "the project inherits the connection's org"
        )
    print(
        "  org isolation: cached attachment documents land in the connection's org-scoped project"
    )


def test_cli_smoke():
    from app.cli import cmd_connector

    src = _docx_bytes(["CLI smoke line."])
    fake = FakeJira(
        {"LOC-11": {"summary": "A ticket"}},
        {
            "LOC-11": [
                {
                    "id": "cli",
                    "filename": "cli.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "h",
                }
            ]
        },
    )
    conn = _conn("att cli")

    class _Args:
        connection_id = conn.id
        issue_keys = ""
        jql = ""
        locales = ""
        actor = "cli"
        local = True
        org = None
        as_json = True

    emitted = {}

    class _Emit:
        as_json = True

        def result(self, text, data):
            emitted.update(data)

        def json(self, data):
            emitted.update(data)

    cmd_connector._set_transport(fake.transport())
    try:
        rc = cmd_connector._jira_translate(_Args(), _Emit())
    finally:
        cmd_connector._set_transport(None)
    assert rc == 0 and emitted.get("ok"), (rc, emitted)
    assert emitted["summary"]["attachments_written"] == 1, emitted["summary"]
    assert [u["filename"] for u in fake.uploads] == ["cli.es-MX.docx"], fake.uploads
    print(
        "  CLI: `connector jira-translate` honors attachment_translate (round-trip via _TRANSPORT)"
    )


def test_all_refused_not_uploaded():
    """FINDING 1 regression: a doc whose segments ALL refuse/flag for a locale must NOT
    upload — export would emit pure SOURCE bytes under {stem}.{loc}.{ext} (a fake
    translation). Pre-fix this uploaded 1 file (attachments_written == 1); the fix skips
    the upload, counts attachments_untranslated, records a retryable failure, leaves the
    (attachment, locale) UNHASHED, and surfaces it loudly in the run message."""
    import app.services.refusal as refusal

    # EVERY paragraph carries the refusal trigger, so every produced target flags.
    src = _docx_bytes(["Please REFUSE_ME now.", "Also REFUSE_ME right here."])
    fake = FakeJira(
        {"LOC-AR": {"summary": "A ticket"}},
        {
            "LOC-AR": [
                {
                    "id": "ar1",
                    "filename": "allref.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "human-1",
                }
            ]
        },
    )
    conn = _conn("att all-refused")
    real_is_refusal = refusal.is_refusal
    real_guard = config.REFUSAL_GUARD_ENABLED
    set_pieces("app.services.refusal", "is_refusal", lambda text: "REFUSE_ME" in (text or ""))
    config.REFUSAL_GUARD_ENABLED = True
    try:
        _ok, msg, summary = cstore.jira_translate(conn.id, actor="auto", transport=fake.transport())
    finally:
        set_pieces("app.services.refusal", "is_refusal", real_is_refusal)
        config.REFUSAL_GUARD_ENABLED = real_guard
    # No upload fired for the all-refused attachment.
    assert summary["attachments_written"] == 0, ("all-refused doc must NOT upload", summary)
    assert not any(u["filename"] == "allref.es-MX.docx" for u in fake.uploads), (
        "no source-bytes fake translation uploaded",
        fake.uploads,
    )
    # Counted + recorded as a RETRYABLE failure.
    assert summary["attachments_untranslated"] == 1, summary
    fails = summary["attachment_failures"]
    assert any(
        "refused" in (f.get("error") or "") and f.get("filename") == "allref.docx" for f in fails
    ), ("a retryable failure is recorded", fails)
    # Hash ABSENT for the pair (so a re-run after human review re-attempts it).
    cfg_after = (cstore.get(conn.id).config or {}).get("jira_attachment_hashes") or {}
    assert not any(k.startswith("ar1:") for k in cfg_after), (
        "the (attachment, locale) is NOT hashed when nothing uploaded",
        cfg_after,
    )
    # Surfaced LOUDLY in the human run message.
    assert "refused/flagged" in msg and "NOT uploaded" in msg, ("loud message", msg)
    print("  all-refused: no upload, attachments_untranslated counted, unhashed, message loud")


def test_loopguard_healthy_authorship_translates():
    """FINDING 2 regression: the filename loop-guard must fire ONLY in degraded mode.
    A human-uploaded file named like our output ({stem}.{loc}.{ext}) with /myself HEALTHY
    is real source work and MUST be translated (pre-fix it was silently dropped —
    data loss). In DEGRADED mode (/myself unavailable) the same file IS skipped, but now
    counted (attachments_loopguard_skipped) and logged, never silent."""
    # File name collides with our own-output shape for the target locale fr-FR.
    src = _docx_bytes(["A human wrote this fr-FR-named source file."])
    fake = FakeJira(
        {"LOC-HA": {"summary": "A ticket"}},
        {
            "LOC-HA": [
                {
                    "id": "ha1",
                    "filename": "report.fr-FR.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "human-1",
                }
            ]
        },
    )
    conn = _conn("att healthy authorship", {"target_locales": "fr-FR"})
    res = cstore._jira_translate_attachments(
        conn.id, ["LOC-HA"], locales=["fr-FR"], transport=fake.transport()
    )
    # HEALTHY /myself -> authorship is authoritative (author != us) -> translated.
    assert res["attachments_written"] == 1, ("healthy-mode: human file IS translated", res)
    assert res["attachments_loopguard_skipped"] == 0, res
    assert "ha1" in fake.downloads, (
        "the human file IS downloaded when authorship healthy",
        fake.downloads,
    )
    assert [u["filename"] for u in fake.uploads] == ["report.fr-FR.fr-FR.docx"], (
        "translate flow handles a locale-named source sanely",
        fake.uploads,
    )
    print("  loop-guard (healthy): human file named like our output IS translated, not dropped")

    # DEGRADED mode: /myself down -> filename fallback fires, but counted + (would be) logged.
    fake2 = FakeJira(
        {"LOC-HB": {"summary": "A ticket"}},
        {
            "LOC-HB": [
                {
                    "id": "hb1",
                    "filename": "report.fr-FR.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "human-1",
                }
            ]
        },
    )
    fake2.myself_available = False
    conn2 = _conn("att degraded authorship", {"target_locales": "fr-FR"})
    res2 = cstore._jira_translate_attachments(
        conn2.id, ["LOC-HB"], locales=["fr-FR"], transport=fake2.transport()
    )
    assert res2["attachments_written"] == 0, ("degraded-mode: our-shaped file IS skipped", res2)
    assert res2["attachments_loopguard_skipped"] == 1, ("skip is counted, not silent", res2)
    assert "hb1" not in fake2.downloads, ("degraded skip happens before download", fake2.downloads)
    print("  loop-guard (degraded): our-shaped file skipped but COUNTED (never silent)")


def test_ceiling_gates_billable_work():
    """FINDING 3 regression: the WriteBudget must gate the BILLABLE steps
    (download + ingest + pretranslate), not only the upload. Pre-fix, all 4 attachments
    were downloaded + ingested and only the uploads were capped (paid MT past the
    ceiling). The fix trims before the billable work: with ceiling=2 and 4 pending
    single-locale attachments, exactly 2 are downloaded AND ingested."""
    from sqlmodel import Session, select

    from app.models import Document

    fake = FakeJira(
        {"LOC-BG": {"summary": "A ticket"}},
        {
            "LOC-BG": [
                {
                    "id": f"bg{i}",
                    "filename": f"budgetgate{i}.docx",
                    "mime": "application/octet-stream",
                    "bytes": _docx_bytes([f"Budget gate body {i}."]),
                    "author_id": "h",
                }
                for i in range(4)
            ]
        },
    )
    conn = _conn("att budget gate")
    orig = config.MAX_WRITES_PER_RUN
    config.MAX_WRITES_PER_RUN = 2
    try:
        res = cstore._jira_translate_attachments(
            conn.id, ["LOC-BG"], locales=["es-MX"], transport=fake.transport()
        )
    finally:
        config.MAX_WRITES_PER_RUN = orig
    assert res["attachments_written"] == 2, res
    assert len(fake.uploads) == 2, fake.uploads
    # The point of Finding 3: assert DOWNLOADS and INGESTED DOCUMENTS, not just uploads.
    assert len(fake.downloads) == 2, (
        "only 2 downloaded under ceiling=2 (billable work gated)",
        fake.downloads,
    )
    with Session(engine) as s:
        docs = s.exec(select(Document).where(Document.filename.like("budgetgate%"))).all()
    assert len(docs) == 2, (
        "only 2 ingested as Documents under ceiling=2",
        [d.filename for d in docs],
    )
    assert res.get("attachments_ceiling_hit") is True, res
    print("  ceiling: gates the billable download+ingest, not just uploads (2 of 4)")


def test_streaming_download_size_abort():
    """FINDING 5 regression: download_attachment STREAMS with a cumulative byte cap and
    aborts once the running total exceeds max_bytes — a body that LIES about its declared
    size cannot force a full buffer. A counting stream records how much the transport
    actually produced.

    What this proves: post-fix the underlying body is only PARTIALLY consumed before the
    abort (streaming), whereas the pre-fix code buffered r.content in full and consumed
    ALL of it before the len() check. What it can't prove offline: true peak-RSS memory
    bounding (the mock body is already resident) — that is the streaming property the
    partial-consumption assertion stands in for. Both pre- and post-fix return None; the
    consumed-bytes count is what distinguishes them."""
    consumed = {"n": 0}
    full = b"y" * 8192
    chunk_size = 256

    class _CountingStream(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(0, len(full), chunk_size):
                ch = full[i : i + chunk_size]
                consumed["n"] += len(ch)
                yield ch

        def close(self):
            pass

    def handler(req):
        # Declared size is a small LIE (10); the real body is 8192 bytes, streamed.
        return httpx.Response(200, stream=_CountingStream())

    conn = _conn("att stream cap")
    jr = cstore.build_fresh(conn.id, httpx.MockTransport(handler))
    data = jr.download_attachment(f"{BASE}/rest/api/3/attachment/content/s1", max_bytes=1000)
    assert data is None, "streaming download must abort once total > max_bytes"
    assert consumed["n"] < len(full), (
        "streaming aborts before consuming the whole oversized body",
        consumed["n"],
        len(full),
    )
    assert consumed["n"] <= 2000, ("abort happens shortly after crossing the cap", consumed["n"])
    print(
        f"  streaming: over-cap body aborted after {consumed['n']}/{len(full)} bytes (declared size ignored)"  # noqa: E501
    )


def test_delete_surfaces_retained_project():
    """FINDING 4 regression: deleting a connection RETAINS its synced project + pulled
    content (no destructive cascade). store.delete returns the retained project name and
    both the web flash and the CLI output tell the operator a separate project delete is
    needed for full erasure."""
    src = _docx_bytes(["Give this connection a project."])
    fake = FakeJira(
        {"LOC-DL": {"summary": "A ticket"}},
        {
            "LOC-DL": [
                {
                    "id": "dl1",
                    "filename": "keep.docx",
                    "mime": "application/octet-stream",
                    "bytes": src,
                    "author_id": "h",
                }
            ]
        },
    )
    conn = _conn("att delete surfacing")
    # A run binds the connection's project (where the cached attachment Document lives).
    cstore._jira_translate_attachments(
        conn.id, ["LOC-DL"], locales=["es-MX"], transport=fake.transport()
    )
    refreshed = cstore.get(conn.id)
    assert refreshed.project_id, "the run must bind a project"
    proj_name = f"{conn.name} (jira)"

    # store.delete returns the retained project name.
    retained = cstore.delete(conn.id)
    assert retained == proj_name, ("delete returns the retained project name", retained, proj_name)
    # The project (and its cached document) SURVIVE the connection delete.
    from sqlmodel import Session, select

    from app.models import Document, Project

    with Session(engine) as s:
        assert s.get(Project, refreshed.project_id) is not None, (
            "project retained after connection delete"
        )
        docs = s.exec(select(Document).where(Document.project_id == refreshed.project_id)).all()
        assert any(d.filename == "keep.docx" for d in docs), "cached attachment document retained"

    # CLI parity: the delete verb's message mentions the retained project.
    from app.cli import cmd_connector

    conn2 = _conn("att delete cli")
    cstore._jira_translate_attachments(
        conn2.id, ["LOC-DL"], locales=["es-MX"], transport=fake.transport()
    )

    class _Args:
        connection_id = conn2.id
        yes = True
        actor = "cli"
        local = True
        org = None

    emitted = {}

    class _Emit:
        as_json = False

        def result(self, text, data):
            emitted["text"] = text
            emitted["data"] = data

        def json(self, data):
            emitted["data"] = data

    rc = cmd_connector._delete(_Args(), _Emit())
    assert rc == 0, rc
    assert "retained" in emitted["text"] and "delete the project separately" in emitted["text"], (
        "CLI delete surfaces retention",
        emitted.get("text"),
    )
    print(
        "  delete: project + pulled content retained; web+CLI surface 'delete the project separately'"  # noqa: E501
    )


def main():
    init_db()
    test_docx_roundtrip()
    test_html_roundtrip()
    test_allowlist_and_unsupported_and_size()
    test_image_exts_excluded_from_blank_default()
    test_hash_suppression()
    test_write_ceiling()
    test_refusal_fallback_to_source()
    test_loop_guard()
    test_org_isolation()
    test_cli_smoke()
    test_all_refused_not_uploaded()
    test_loopguard_healthy_authorship_translates()
    test_ceiling_gates_billable_work()
    test_streaming_download_size_abort()
    test_delete_surfaces_retained_project()
    print("JIRA ATTACHMENTS OK")


if __name__ == "__main__":
    main()
