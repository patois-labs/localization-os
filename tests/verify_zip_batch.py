"""Batch 13: ZIP batch upload + translated download as a ZIP with the same folders.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). Proves:
  [A] expand(): one member per supported file with its folder path; unsupported, nested
      .zip, symlink, OS junk and case-insensitive duplicates are LISTED as skipped.
  [B] hostile archives refuse the WHOLE upload: '..', absolute, drive-letter and control
      character paths, a compression bomb, a forged size, too many members, too many
      supported files; non-zip bytes are NotAZipArchive.
  [C] web: upload creates one document per supported file named by its folder path,
      lists skipped/failed members on the project page, and offers a per-batch download;
      the download is <locale>/<original path> with translated content; a stored path
      that was tampered with cannot climb out of its locale folder.
  [D] /api: upload returns documents/skipped/failed; formats lists .zip under
      batch_formats only; documents list carries zip_batches; export?batch= works;
      junk .zip bytes stay 415 (web: err=format, as verify_chunk1/9 pin).
  [E] CLI: doc upload <pid> x.zip, doc list, project export --batch.
  [F] .zip is not a registered filter (supported_exts unchanged: Jira still skips .zip).
Final line: "ZIP BATCH OK".
"""

import io
import json
import os
import sys
import tempfile
import zipfile
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_zipbatch_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.filters import supported_exts, zip_batch
from app.filters.base import InputLimitError
from app.main import app
from app.models import Document, Segment, SegStatus


def _zip(entries, method=zipfile.ZIP_DEFLATED) -> bytes:
    """entries: list of (name, bytes) or (ZipInfo, bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _symlink_info(name):
    zi = zipfile.ZipInfo(name)
    zi.external_attr = 0o120777 << 16
    return zi


MD = b"# Getting started\n\nInstall the app. Then open it.\n"
TXT = b"Hello world.\nSecond line here.\n"
JSON_ = b'{"title": "Save {count} files", "body": "Click %s to continue."}\n'
NESTED = _zip([("inner.md", b"# inner\n")])


def _good_zip() -> bytes:
    return _zip(
        [
            ("docs/", b""),
            ("docs/guide/intro.md", MD),
            ("readme.txt", TXT),
            ("data/strings.json", JSON_),
            ("tools/setup.exe", b"MZ\x00\x00"),
            ("archive/nested.zip", NESTED),
            ("__MACOSX/docs/._intro.md", b"\x00\x05"),
            (".DS_Store", b"\x00\x00"),
            (_symlink_info("docs/link.md"), b"../../etc/passwd"),
            ("DOCS/GUIDE/INTRO.md", MD),
            ("blank.txt", b"   \n\n"),
            ("noext", b"plain"),
        ]
    )


def section_a():
    b = zip_batch.expand(_good_zip(), archive="site.zip")
    paths = [m.path for m in b.members]
    # blank.txt is supported (it FAILS later at ingest), so it is a member here
    assert paths == ["docs/guide/intro.md", "readme.txt", "data/strings.json", "blank.txt"], paths
    names = [m.name for m in b.members]
    assert names[0] == "docs__guide__intro.md", names
    skipped = {s["path"]: s["reason"] for s in b.skipped}
    assert skipped["tools/setup.exe"].startswith("unsupported format (.exe)"), skipped
    assert "nested ZIP" in skipped["archive/nested.zip"], skipped
    assert "system file" in skipped["__MACOSX/docs/._intro.md"], skipped
    assert "system file" in skipped[".DS_Store"], skipped
    assert "symbolic link" in skipped["docs/link.md"], skipped
    assert "duplicate" in skipped["DOCS/GUIDE/INTRO.md"], skipped
    assert "no extension" in skipped["noext"], skipped
    assert len(b.batch_id) == 12 and b.archive == "site.zip"
    assert zip_batch.is_batch_upload("x.ZIP") and not zip_batch.is_batch_upload("x.flprjzip")
    assert not zip_batch.is_batch_upload("zip")
    print("  [A] expand: members keep folders; 7 kinds of skipped member listed with reasons")


def _refused(data, needle):
    try:
        zip_batch.expand(data)
    except InputLimitError as exc:
        assert needle in str(exc), (needle, str(exc))
        return
    raise AssertionError(f"expected refusal containing {needle!r}")


def section_b():
    for bad in (
        "../evil.md",
        "docs/../../evil.md",
        "/etc/evil.md",
        "C:/evil.md",
        "C:\\evil.md",
        "docs\\..\\..\\evil.md",
        "a\x01b.md",
        "trail./x.md",
    ):
        _refused(_zip([("ok.md", MD), (bad, MD)]), "unsafe path")
    for raw in ("../x", "/x", "c:x", "", "a/./../b"):
        assert zip_batch.safe_member_path(raw) is None, raw
    assert zip_batch.safe_member_path("a/./b//c.md") == "a/b/c.md"
    # compression bomb: 50 MB of zeros deflates ~1000x
    _refused(_zip([("bomb.txt", b"\x00" * (50 * 1024 * 1024))]), "compression bomb")
    # forged declared size: patch the central directory's uncompressed size downwards
    data = bytearray(_zip([("a.txt", b"A" * 5000)]))
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 24 : cd + 28] = (10).to_bytes(4, "little")
    try:
        zip_batch.expand(bytes(data))
        raise AssertionError("forged size accepted")
    except (InputLimitError, zip_batch.NotAZipArchive):
        pass
    old_members, old_docs = config.ZIP_MAX_MEMBERS, config.ZIP_BATCH_MAX_DOCUMENTS
    try:
        config.ZIP_MAX_MEMBERS = 3
        _refused(_zip([(f"f{i}.txt", TXT) for i in range(4)]), "internal parts")
        config.ZIP_MAX_MEMBERS = old_members
        config.ZIP_BATCH_MAX_DOCUMENTS = 2
        _refused(_zip([(f"f{i}.txt", TXT) for i in range(3)]), "ZIP_BATCH_MAX_DOCUMENTS")
        # skipped members do not count toward the document cap
        b = zip_batch.expand(_zip([("a.txt", TXT), ("b.exe", b"x"), ("c.exe", b"x")]))
        assert len(b.members) == 1 and len(b.skipped) == 2
    finally:
        config.ZIP_MAX_MEMBERS, config.ZIP_BATCH_MAX_DOCUMENTS = old_members, old_docs
    for junk in (b"", b"PK\x03\x04hello", b"not a zip"):
        try:
            zip_batch.expand(junk)
            raise AssertionError("junk accepted")
        except zip_batch.NotAZipArchive:
            pass
    print("  [B] hostile: traversal/absolute/drive/control paths, bomb, forged size, caps refused")


def _project(client, name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "fr-FR, de-DE",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _translate(pid, locale):
    with Session(engine) as s:
        doc_ids = [d.id for d in s.exec(select(Document).where(Document.project_id == pid))]
        for seg in s.exec(
            select(Segment).where(Segment.document_id.in_(doc_ids), Segment.target_locale == locale)
        ):
            seg.target_text = "[FR] " + seg.source_text
            seg.status = SegStatus.APPROVED
            s.add(seg)
        s.commit()


def section_c(client):
    pid = _project(client, "ZipBatch Web")
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("site.zip", _good_zip(), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    loc = r.headers["location"]
    assert "zip_msg=" in loc and "err=" not in loc, loc
    page = client.get(loc.split("#")[0])
    assert page.status_code == 200
    html = page.text
    assert "3 document(s) created from site.zip" in html, html[:2000]
    assert "tools/setup.exe: unsupported format (.exe)" in html
    assert "archive/nested.zip: nested ZIP archive" in html
    assert "blank.txt: The file has no text to translate." in html
    assert 'id="zip-batches"' in html and "?batch=" in html
    assert ".zip" in html.split('id="doc-upload-file"', 1)[1].split("required", 1)[0]
    with Session(engine) as s:
        docs = s.exec(select(Document).where(Document.project_id == pid)).all()
        by_name = {d.filename: d for d in docs}
    assert set(by_name) == {"docs__guide__intro.md", "readme.txt", "data__strings.json"}, by_name
    info = by_name["docs__guide__intro.md"].skeleton["zip_batch"]
    assert info["path"] == "docs/guide/intro.md" and info["archive"] == "site.zip"
    batch_id = info["id"]
    assert len({d.skeleton["zip_batch"]["id"] for d in docs}) == 1

    # gate (train-9): many long non-Latin skipped paths must not blow the redirect URL
    # (20 listed CJK paths percent-encode to ~40 KB uncapped)
    long_dir = "文書" * 60
    many = [("ok.md", MD)] + [(f"{long_dir}/f{i}.exe", b"x") for i in range(25)]
    r = client.post(
        f"/projects/{_project(client, 'ZipBatch CJK')}/documents",
        data={"mode": "segmented"},
        files={"file": ("cjk.zip", _zip(many), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert len(r.headers["location"]) < 8000, len(r.headers["location"])
    assert "full list is on the API and CLI" in unquote(r.headers["location"])

    # a crafted free-text zip_msg link is NOT shown (page_guard)
    spoof = client.get(f"/projects/{pid}?zip_msg=Call+555+for+support")
    assert "Call 555 for support" not in spoof.text

    _translate(pid, "fr-FR")
    r = client.get(f"/projects/{pid}/export?batch={batch_id}&locales=fr-FR")
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.headers["content-type"].startswith("application/zip")
    assert "site_translated.zip" in r.headers.get("content-disposition", "")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(n for n in zf.namelist() if not n.startswith("_"))
    assert names == ["fr-FR/data/strings.json", "fr-FR/docs/guide/intro.md", "fr-FR/readme.txt"], (
        names
    )
    intro = zf.read("fr-FR/docs/guide/intro.md").decode()
    assert "[FR] Install the app." in intro and intro.startswith("# [FR] Getting started"), intro
    strings = json.loads(zf.read("fr-FR/data/strings.json"))
    assert strings["title"] == "[FR] Save {count} files", strings
    assert "%s" in strings["body"]

    # all languages: de-DE is untranslated (held) -> skip-and-reported, never source-echoed
    r = client.get(f"/projects/{pid}/export?batch={batch_id}")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "fr-FR/docs/guide/intro.md" in zf.namelist()
    assert not any(n.startswith("de-DE/") for n in zf.namelist()), zf.namelist()
    assert "_QUARANTINED.txt" in zf.namelist()

    # an unknown batch is a refusal, not an empty zip or the whole project
    r = client.get(f"/projects/{pid}/export?batch=nope", follow_redirects=False)
    assert r.status_code == 303 and "err=export" in r.headers["location"], r.headers

    # a non-batch document in the same project is never part of the batch bundle
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("loose.txt", b"Loose file.\n", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    _translate(pid, "fr-FR")
    r = client.get(f"/projects/{pid}/export?batch={batch_id}&locales=fr-FR")
    assert "fr-FR/loose.txt" not in zipfile.ZipFile(io.BytesIO(r.content)).namelist()

    # defence in depth: a stored path tampered to climb out stays inside its locale folder
    with Session(engine) as s:
        d = s.exec(select(Document).where(Document.filename == "readme.txt")).first()
        sk = dict(d.skeleton)
        sk["zip_batch"] = {**sk["zip_batch"], "path": "../../evil.txt"}
        d.skeleton = sk
        s.add(d)
        s.commit()
    r = client.get(f"/projects/{pid}/export?batch={batch_id}&locales=fr-FR")
    nl = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "fr-FR/readme.txt" in nl and not any(".." in n for n in nl), nl

    # nothing supported -> nothing created, plain error naming why
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("bin.zip", _zip([("a.exe", b"x")]), "application/zip")},
        follow_redirects=False,
    )
    assert (
        "err=" in r.headers["location"] and "No%20document%20was%20created" in r.headers["location"]
    )
    # hostile archive -> whole refusal, nothing stored
    before = _count_docs(pid)
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("evil.zip", _zip([("ok.md", MD), ("../x.md", MD)]), "application/zip")},
        follow_redirects=False,
    )
    assert "unsafe%20path" in r.headers["location"], r.headers["location"]
    assert _count_docs(pid) == before
    # junk bytes named .zip keep the pinned err=format (verify_chunk1 / verify_chunk9)
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("archive.zip", b"PK\x03\x04hello", "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=format" in r.headers["location"]
    print("  [C] web: upload -> 3 docs + listed skips/fails; batch ZIP keeps folders per language")
    return pid, batch_id


def _count_docs(pid):
    with Session(engine) as s:
        return len(s.exec(select(Document.id).where(Document.project_id == pid)).all())


PW = "zipbatch-pw-123456"


def section_d(web):
    from app.services import apikeys, security

    pid = _project(web, "ZipBatch API")
    u = security.create_user("zip@example.com", "Zip Admin", "admin", PW)
    _row, raw = apikeys.create_key(u.id, "zipbatch", scopes="api docs")
    client = TestClient(app, headers={"X-Loc-Token": raw})
    r = client.get("/api/formats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "zip" not in {f["ext"] for f in body["formats"]}
    assert [f["ext"] for f in body["batch_formats"]] == ["zip"]
    r = client.post(
        "/api/documents",
        data={"project_id": str(pid), "mode": "segmented"},
        files={"file": ("site.zip", _good_zip(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["project_id"] == pid and body["archive"] == "site.zip"
    assert [d["path"] for d in body["documents"]] == [
        "docs/guide/intro.md",
        "readme.txt",
        "data/strings.json",
    ], body["documents"]
    assert all(d["segments"] > 0 for d in body["documents"])
    assert len(body["skipped"]) == 7 and [f["path"] for f in body["failed"]] == ["blank.txt"]
    batch_id = body["batch_id"]
    r = client.get(f"/api/projects/{pid}/documents")
    zb = r.json()["zip_batches"]
    assert zb and zb[0]["id"] == batch_id and zb[0]["documents"] == 3, zb
    _translate(pid, "fr-FR")
    r = client.get(f"/api/projects/{pid}/export?batch={batch_id}&locales=fr-FR")
    assert r.status_code == 200, r.text[:300]
    nl = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "fr-FR/docs/guide/intro.md" in nl, nl
    r = client.get(f"/api/projects/{pid}/export?batch=nope")
    assert r.status_code == 400 and "nope" in r.json()["error"]
    r = client.post(
        "/api/documents",
        data={"project_id": str(pid)},
        files={"file": ("a.zip", _zip([("a.exe", b"x")]), "application/zip")},
    )
    assert r.status_code == 422 and r.json()["skipped"][0]["path"] == "a.exe", r.text
    assert "No document was created" in r.json()["error"]
    r = client.post(
        "/api/documents",
        data={"project_id": str(pid)},
        files={"file": ("a.zip", _zip([("/abs.md", MD)]), "application/zip")},
    )
    assert r.status_code == 413 and "unsafe path" in r.json()["error"], r.text
    r = client.post(
        "/api/documents",
        data={"project_id": str(pid)},
        files={"file": ("a.zip", b"PK\x03\x04junk", "application/zip")},
    )
    assert r.status_code == 415, r.text
    print("  [D] api: upload lists documents/skipped/failed; batch_formats; export?batch=")


def section_e():
    from tests._cli_runner import run_cli

    with Session(engine) as s:
        from app.models import Project

        pid = s.exec(select(Project.id).where(Project.name == "ZipBatch API")).first()
    tmp = tempfile.mkdtemp(prefix="zipbatch_cli_")
    src = os.path.join(tmp, "bundle.zip")
    with open(src, "wb") as fh:
        fh.write(_zip([("pages/home.md", MD), ("x.bin", b"\x00")]))
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    assert "pages/home.md" in out and "skipped  x.bin: unsupported format (.bin)" in out, out
    assert "--batch" in out
    code, out, err = run_cli(["--json", "doc", "upload", str(pid), src])
    assert code == 0, (out, err)
    body = json.loads(out)
    batch_id = body["batch_id"]
    assert body["documents"][0]["path"] == "pages/home.md" and body["skipped"][0]["path"] == "x.bin"
    code, out, err = run_cli(["doc", "list", str(pid)])
    assert code == 0 and f"ZIP batch {batch_id}: bundle.zip (1 document(s))" in out, out
    code, out, err = run_cli(["--json", "doc", "list", str(pid)])
    assert any(b["id"] == batch_id for b in json.loads(out)["zip_batches"])
    _translate(pid, "fr-FR")
    dest = os.path.join(tmp, "out.zip")
    code, out, err = run_cli(
        ["project", "export", str(pid), "--batch", batch_id, "--locales", "fr-FR", "-o", dest]
    )
    assert code == 0, (code, out, err)
    nl = zipfile.ZipFile(dest).namelist()
    assert "fr-FR/pages/home.md" in nl, nl
    junk = os.path.join(tmp, "junk.zip")
    with open(junk, "wb") as fh:
        fh.write(b"PK\x03\x04junk")
    code, out, err = run_cli(["doc", "upload", str(pid), junk])
    assert code == 2 and "unsupported file format" in (out + err), (code, out, err)
    print("  [E] cli: doc upload x.zip, doc list batches, project export --batch")


def section_f():
    exts = supported_exts()
    assert "zip" not in exts, "a .zip must stay out of supported_exts (Jira allowlist)"
    print("  [F] .zip is not a registered document format")


def main():
    init_db()
    client = TestClient(app)
    section_a()
    section_b()
    section_c(client)
    section_d(client)
    section_e()
    section_f()
    print("ZIP BATCH OK")


if __name__ == "__main__":
    main()
