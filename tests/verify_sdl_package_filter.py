"""Trados Studio packages (Batch 13): project packages (.sdlppx) in, return packages
(.sdlrpx) out (app/filters/sdl_package.py, app/services/sdl_package.py).

  inspect      the .sdlproj language directions, one document per target SDLXLIFF, every
               other member listed as skipped with a reason (source files, TMs, reports).
  member       a document's stored original holds only the .sdlproj and its SDLXLIFF, byte
               for byte; parse gives exactly the SDLXLIFF filter's units.
  identity     a build with nothing to write returns a return package whose SDLXLIFF is the
               original's bytes and whose .sdlproj differs ONLY by PackageType.
  hostile      not a zip, no/two .sdlproj, DOCTYPE in the .sdlproj, no language direction,
               no target files, too many target files, a zip bomb, a forged marker.
  import       ingest of a .sdlppx makes one document per target language the project has,
               lists the rest; a project with none of the languages is refused, nothing kept.
  export       one return package for the whole package: translated de-DE file, held fr-FR
               file left out and reported (allow_held puts it back).
  cli          `doc upload` of a package and `project export --sdl-return-package`.

Hermetic + offline. Final line: "SDL PACKAGE FILTER OK".
"""

import io
import json
import os
import subprocess
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sdlpkg_")

from app.filters import get_filter, supported_exts
from app.filters import sdl_package as pkg
from app.filters.base import InputLimitError

FX = os.path.join(_ROOT, "tests", "fixtures", "formats")


def _fx(*parts) -> bytes:
    with open(os.path.join(FX, *parts), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts: test LF bytes


PROJ = _fx("sdlpackage", "demo.sdlproj")
DE = _fx("catxliff", "sample.sdlxliff")
FR = DE.replace(b'target-language="de-DE"', b'target-language="fr-FR"')
STAMP = (2026, 10, 10, 18, 0, 0)
PASSWORD_FIXTURE = "sdlpkg-fixture-pw-12345"  # pregate: allow-secret test fixture user


def _package(members=None, proj=PROJ, comment=b"") -> bytes:
    if members is None:
        members = [
            ("demo.sdlproj", proj),
            ("en-US/guide.docx", b"PK not really a docx"),
            ("de-DE/guide.docx.sdlxliff", DE),
            ("fr-FR/guide.docx.sdlxliff", FR),
            ("Tms/demo.sdltm", b"SQLite format 3\x00"),
            ("Reports/Analyze Files en-US_de-DE.xml", b"<report/>"),
        ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in members:
            z.writestr(zipfile.ZipInfo(name, date_time=STAMP), body, zipfile.ZIP_DEFLATED)
        z.comment = comment
    return buf.getvalue()


PKG = _package()


def _members(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: (z.read(i), i.date_time) for i in z.infolist()}


def _refused(data: bytes, needle: str) -> None:
    try:
        pkg.inspect(data, "x.sdlppx")
    except InputLimitError as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_registration():
    exts = set(supported_exts())
    assert {"sdlrpx", "sdlppx"} <= exts
    assert get_filter("sdlppx").fmt == "sdlrpx"
    print("  [registry] sdlrpx / sdlppx registered OK")


def test_inspect():
    info = pkg.inspect(PKG, "demo.sdlppx").as_dict()
    assert info["source_locale"] == "en-US", info
    assert info["target_locales"] == ["de-DE", "fr-FR"], info
    assert info["package_guid"] == "c0ffee00-1234-4abc-9def-001122334455", info
    assert info["package_type"] == "ProjectPackage", info
    assert [d["path"] for d in info["documents"]] == [
        "de-DE/guide.docx.sdlxliff",
        "fr-FR/guide.docx.sdlxliff",
    ], info
    reasons = {s["path"]: s["reason"] for s in info["skipped"]}
    assert "source file" in reasons["en-US/guide.docx"], reasons
    assert "translation memory" in reasons["Tms/demo.sdltm"], reasons
    assert "support file" in reasons["Reports/Analyze Files en-US_de-DE.xml"], reasons
    assert info["skipped_total"] == 3, info
    print("  [inspect] language pair, one document per target file, skipped files listed OK")


def test_member_blob_and_identity():
    info = pkg.inspect(PKG, "demo.sdlppx")
    blob = pkg.member_blob(PKG, info, info.targets[0])
    m = _members(blob)
    assert set(m) == {"demo.sdlproj", "de-DE/guide.docx.sdlxliff"}, set(m)
    assert m["demo.sdlproj"][0] == PROJ and m["de-DE/guide.docx.sdlxliff"][0] == DE
    filt = get_filter("sdlrpx")
    units = {u.p_index: u.text for u in filt.parse(blob).units}
    assert units == {u.p_index: u.text for u in get_filter("sdlxliff").parse(DE).units}, units
    skel = filt.parse(blob).skeleton
    assert skel["sdl_package"]["member"] == "de-DE/guide.docx.sdlxliff", skel
    out = filt.build(blob, {})
    om = _members(out)
    assert list(om) == ["demo.sdlproj", "de-DE/guide.docx.sdlxliff"], list(om)
    assert om["de-DE/guide.docx.sdlxliff"] == (DE, STAMP), "SDLXLIFF bytes or date changed"
    assert om["demo.sdlproj"][0] == PROJ.replace(
        b'PackageType="ProjectPackage"', b'PackageType="ReturnPackage"'
    ), "the .sdlproj changed beyond PackageType"
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert not z.comment.startswith(pkg.MARKER), "the stored-blob marker leaked"
    # a translation lands in the SDLXLIFF only
    key = min(units)
    out2 = filt.build_localized(blob, {str(key): "Offnen Sie das Menu."}, "de-DE")
    sdl2 = _members(out2)["de-DE/guide.docx.sdlxliff"][0]
    assert b"Offnen Sie das Menu." in sdl2 and b'conf="Translated"' in sdl2, sdl2[:200]
    # the flip touches the root element only, never a nested or absent attribute
    assert pkg.flip_package_type(b'<Project Name="x"/>') == b'<Project Name="x"/>'
    nested = (
        b'<PackageProject PackageType="ReturnPackage">'
        b'<X PackageType="ProjectPackage"/></PackageProject>'
    )
    assert pkg.flip_package_type(nested) == nested
    # a raw package with exactly ONE target file parses directly (connectors, old paths)
    one = _package([("demo.sdlproj", PROJ), ("de-DE/guide.docx.sdlxliff", DE)])
    assert {u.p_index for u in filt.parse(one).units} == set(units)
    try:
        filt.parse(PKG)
    except InputLimitError as exc:
        assert "2 target files" in str(exc), str(exc)
    else:
        raise AssertionError("a two-file raw package must not parse as one document")
    print("  [member] stored original, units, identity build, PackageType-only change OK")


def test_hostile():
    from app import config

    _refused(b"not a zip at all", "not a valid zip")
    _refused(_package([("de-DE/a.sdlxliff", DE)]), "exactly one .sdlproj")
    _refused(
        _package([("a.sdlproj", PROJ), ("b.sdlproj", PROJ), ("de-DE/a.sdlxliff", DE)]), "found 2"
    )
    doctype = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
        b"<PackageProject>&a;</PackageProject>"
    )
    _refused(_package([("a.sdlproj", doctype), ("de-DE/a.sdlxliff", DE)]), "could not be read")
    nolang = b'<PackageProject PackageType="ProjectPackage"><LanguageDirections/></PackageProject>'
    _refused(_package([("a.sdlproj", nolang), ("de-DE/a.sdlxliff", DE)]), "no language direction")
    _refused(_package([("demo.sdlproj", PROJ), ("en-US/guide.docx", b"x")]), "no target-language")
    _refused(_package([("demo.sdlproj", b"<Other/>"), ("de-DE/a.sdlxliff", DE)]), "not a Trados")
    old = pkg.MAX_TARGET_FILES
    pkg.MAX_TARGET_FILES = 1
    try:
        _refused(PKG, "the limit is 1")
    finally:
        pkg.MAX_TARGET_FILES = old
    # a compression bomb is refused by zip_guard before any member is read
    old_ratio = config.ZIP_MAX_RATIO
    config.ZIP_MAX_RATIO = 5
    try:
        bomb = _package([("demo.sdlproj", PROJ), ("de-DE/a.sdlxliff", DE + b" " * 2_000_000)])
        _refused(bomb, "expands")
    finally:
        config.ZIP_MAX_RATIO = old_ratio
    # a forged marker naming a member that is missing / not an SDLXLIFF
    for meta in (
        {"member": "de-DE/missing.sdlxliff", "project": "demo.sdlproj"},
        {"member": "en-US/guide.docx", "project": "demo.sdlproj"},
    ):
        forged = _package(
            [("demo.sdlproj", PROJ), ("en-US/guide.docx", b"x"), ("de-DE/guide.docx.sdlxliff", DE)],
            comment=pkg.MARKER + json.dumps(meta).encode(),
        )
        try:
            get_filter("sdlrpx").parse(forged)
        except InputLimitError as exc:
            assert "incomplete" in str(exc), str(exc)
        else:
            raise AssertionError(f"forged marker {meta} must be refused")
    print(
        "  [hostile] not-zip, sdlproj count, DOCTYPE, no languages, no targets, caps, "
        "bomb, forged marker OK"
    )


def _project(s, targets):
    from app.models import Project, TargetLocale

    p = Project(
        name="sdlpkg-" + "-".join(targets), org_id=1, source_locale="en-US", default_provider="mock"
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    for t in targets:
        s.add(TargetLocale(project_id=p.id, locale=t))
    s.commit()
    return p


def test_import_and_return_package():
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Segment
    from app.services import delivery, sdl_package
    from app.services.ingest import ingest_document

    init_db()
    with Session(engine) as s:
        # a project with none of the package's languages: refused, nothing stored
        bad = _project(s, ["es-ES"])
        try:
            ingest_document(s, bad, "demo.sdlppx", PKG, "segmented")
        except InputLimitError as exc:
            assert "de-DE, fr-FR" in str(exc), str(exc)
        else:
            raise AssertionError("a package with no matching target language must be refused")
        assert not s.exec(select(Document).where(Document.project_id == bad.id)).all()

        # de-DE only: the fr-FR file is listed as skipped
        de_only = _project(s, ["de-DE"])
        doc = ingest_document(s, de_only, "demo.sdlppx", PKG, "segmented")
        summary = sdl_package.import_summary(s, doc)
        assert [d["locale"] for d in summary["documents"]] == ["de-DE"], summary
        assert any("fr-FR is not a target" in x["reason"] for x in summary["skipped"]), summary

        p = _project(s, ["de-DE", "fr-FR"])
        first = ingest_document(s, p, "demo.sdlppx", PKG, "segmented")
        summary = sdl_package.import_summary(s, first)
        docs = summary["documents"]
        assert [(d["locale"], d["path"]) for d in docs] == [
            ("de-DE", "de-DE/guide.docx.sdlxliff"),
            ("fr-FR", "fr-FR/guide.docx.sdlxliff"),
        ], docs
        assert docs[0]["filename"] == "de-DE_guide.docx.sdlrpx", docs
        assert summary["skipped_total"] == 3, summary
        de_doc = s.get(Document, docs[0]["id"])
        assert de_doc.fmt == "sdlrpx"
        for g in s.exec(
            select(Segment).where(
                Segment.document_id == de_doc.id, Segment.target_locale == "de-DE"
            )
        ).all():
            g.target_text = "DE " + g.source_text
            g.status = "translated"
            s.add(g)
        s.commit()

        data, name, skipped = delivery.export_sdl_return_package(s, p.id)
        assert name == "demo.sdlrpx", name
        m = _members(data)
        assert list(m) == ["demo.sdlproj", "de-DE/guide.docx.sdlxliff"], list(m)
        assert b'PackageType="ReturnPackage"' in m["demo.sdlproj"][0]
        assert b"DE Then click Save." in m["de-DE/guide.docx.sdlxliff"][0]
        assert [x["locale"] for x in skipped] == ["fr-FR"], skipped
        # allow_held puts the untranslated fr-FR file back, unchanged in its targets
        data, _name, skipped = delivery.export_sdl_return_package(s, p.id, allow_held=True)
        m = _members(data)
        assert set(m) == {"demo.sdlproj", "de-DE/guide.docx.sdlxliff", "fr-FR/guide.docx.sdlxliff"}
        assert not skipped
        assert m["fr-FR/guide.docx.sdlxliff"][0] == FR, (
            "an untranslated file must come back unchanged"
        )
        # a single document's own export is a one-file return package
        from app.services.export import build_translated_file

        one = _members(build_translated_file(s, de_doc, "de-DE"))
        assert list(one) == ["demo.sdlproj", "de-DE/guide.docx.sdlxliff"], list(one)
        # a project with no package documents names the problem
        try:
            delivery.export_sdl_return_package(s, bad.id)
        except ValueError as exc:
            assert "no documents imported from a Trados package" in str(exc)
        else:
            raise AssertionError("a project without package documents must refuse")
        pid, de_id = p.id, de_doc.id
    globals()["_E2E"] = (pid, de_id)
    print("  [import/export] one document per target, mismatch refused, return package built OK")


def test_cli():
    tmp = os.environ["LOC_TMS_DATA_DIR"]
    pkg_path = os.path.join(tmp, "cli-demo.sdlppx")
    with open(pkg_path, "wb") as fh:
        fh.write(PKG)
    pid, _de = globals()["_E2E"]

    def cli(*argv):
        r = subprocess.run(
            [sys.executable, "-m", "app.cli", "--json", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_ROOT,
            env=os.environ.copy(),
            timeout=300,
        )
        return r.returncode, r.stdout, r.stderr

    rc, out, err = cli("doc", "upload", str(pid), pkg_path)
    assert rc == 0, (out, err)
    res = json.loads(out)
    assert res["fmt"] == "sdlrpx" and len(res["sdl_package"]["documents"]) == 2, res
    out_path = os.path.join(tmp, "cli-return.sdlrpx")
    guid = "c0ffee00-1234-4abc-9def-001122334455"
    rc, out, err = cli(
        "project", "export", str(pid), "--sdl-return-package", guid, "--allow-held", "-o", out_path
    )
    assert rc == 0, (out, err)
    assert os.path.isfile(out_path)
    with zipfile.ZipFile(out_path) as z:
        assert "demo.sdlproj" in z.namelist(), z.namelist()
    # project create --from-sdl-package takes the languages from the .sdlproj
    from sqlmodel import Session

    from app.db import engine
    from app.models import Customer

    with Session(engine) as s:
        c = Customer(name="sdlpkg customer", org_id=1)
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id
    rc, out, err = cli(
        "project",
        "create",
        "From package",
        "--customer-id",
        str(cid),
        "--from-sdl-package",
        pkg_path,
    )
    assert rc == 0, (out, err)
    res = json.loads(out)
    assert res["source_locale"] == "en-US" and res["target_locales"] == ["de-DE", "fr-FR"], res
    assert [d["locale"] for d in res["sdl_package"]["documents"]] == ["de-DE", "fr-FR"], res
    print("  [cli] doc upload, project create --from-sdl-package, export --sdl-return-package OK")


def test_web_and_api():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import apikeys, security

    pid, _de = globals()["_E2E"]
    u = security.create_user("sdlpkg@example.test", "sdlpkg", "admin", PASSWORD_FIXTURE)
    _row, raw = apikeys.create_key(u.id, "sdlpkg-test", scopes="api docs")
    tok = {"X-Loc-Token": raw}
    with TestClient(app) as c:
        page = c.get(f"/projects/{pid}")
        assert page.status_code == 200, page.status_code
        assert 'id="sdl-return-download"' in page.text and "Tms/demo.sdltm" in page.text
        r = c.get(f"/projects/{pid}/export?sdl_return_package=1", follow_redirects=False)
        assert r.status_code == 200, (r.status_code, r.headers.get("location"))
        assert "demo.sdlproj" in _members(r.content), "web return package"
        assert r.headers.get("x-export-skipped") == "1", dict(r.headers)
        r = c.get(f"/api/projects/{pid}/export?sdl_return_package=1&allow_held=1", headers=tok)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        assert len(_members(r.content)) == 3, list(_members(r.content))
        up = c.post(
            "/api/documents",
            data={"project_id": str(pid)},
            files={"file": ("demo.sdlppx", PKG, "application/octet-stream")},
            headers=tok,
        )
        assert up.status_code == 200, up.text[:300]
        assert len(up.json()["sdl_package"]["documents"]) == 2, up.json()
    print("  [web/api] project page block, web + API return package, API upload listing OK")


def test_create_project_web_and_api():
    """Gate (web/CLI parity): "create a project from a package" on the web new-project
    form and on POST /api/documents sdl_new_project=1, not only the CLI. Languages come
    from the .sdlproj; a refused package or a failed import leaves no project behind."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine
    from app.main import app
    from app.models import Customer, Document, Project, TargetLocale
    from app.services import apikeys, security
    from tests import _csrf

    with Session(engine) as s:
        c = Customer(name="sdlpkg web customer", org_id=1)
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id
    u = security.create_user("sdlpkg-new@example.test", "sdlpkg-new", "admin", PASSWORD_FIXTURE)
    _row, raw = apikeys.create_key(u.id, "sdlpkg-new", scopes="api docs")
    tok = {"X-Loc-Token": raw}
    broken = _package([("demo.sdlproj", PROJ), ("de-DE/guide.docx.sdlxliff", b"<xliff broken")])

    def project_named(name):
        with Session(engine) as s:
            return s.exec(select(Project).where(Project.name == name)).first()

    def shape(p):
        with Session(engine) as s:
            tls = sorted(
                t.locale
                for t in s.exec(select(TargetLocale).where(TargetLocale.project_id == p.id))
            )
            docs = s.exec(select(Document).where(Document.project_id == p.id)).all()
            return p.source_locale, tls, sorted(d.fmt for d in docs)

    with TestClient(app) as cl:
        page = cl.get("/projects")
        assert page.status_code == 200 and 'name="sdl_package"' in page.text, "form field"
        assert 'enctype="multipart/form-data"' in page.text, "the form must post files"
        hdr = {"x-csrf-token": _csrf.csrf_value(cl)}

        def web_create(name, data):
            return cl.post(
                "/projects",
                data={
                    "name": name,
                    "customer_id": str(cid),
                    "source_locale": "ja-JP",
                    "target_locales": "es-ES",
                },
                files={"sdl_package": ("demo.sdlppx", data, "application/octet-stream")},
                headers=hdr,
                follow_redirects=False,
            )

        r = web_create("Web from package", PKG)
        assert r.status_code == 303, (r.status_code, r.text[:300])
        p = project_named("Web from package")
        assert p is not None and r.headers["location"] == f"/projects/{p.id}#sdl-package", (
            r.headers.get("location")
        )
        assert shape(p) == ("en-US", ["de-DE", "fr-FR"], ["sdlrpx", "sdlrpx"]), shape(p)
        for name, data in (("Web not a zip", b"not a zip"), ("Web broken import", broken)):
            r = web_create(name, data)
            assert r.status_code == 303 and "err=" in r.headers["location"], r.headers
            assert project_named(name) is None, f"{name}: a refused package left a project"
        # a plain create (no file) is unchanged
        r = cl.post(
            "/projects",
            data={"name": "Web plain", "customer_id": str(cid), "target_locales": "es-ES"},
            headers=hdr,
            follow_redirects=False,
        )
        assert r.status_code == 303 and project_named("Web plain") is not None, r.headers

        def api_create(data, **form):
            return cl.post(
                "/api/documents",
                data={"sdl_new_project": "1", **form},
                files={"file": ("demo.sdlppx", data, "application/octet-stream")},
                headers=tok,
            )

        r = api_create(PKG, customer_id=str(cid), name="API from package")
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["source_locale"] == "en-US" and body["target_locales"] == ["de-DE", "fr-FR"]
        assert [d["locale"] for d in body["sdl_package"]["documents"]] == ["de-DE", "fr-FR"]
        p = project_named("API from package")
        assert p is not None and p.id == body["project_id"] and p.customer_id == cid
        r = api_create(PKG, customer_id=str(cid), name="API from package")
        assert r.status_code == 409, (r.status_code, r.text[:200])
        r = api_create(PKG, name="API no customer")
        assert r.status_code == 400 and project_named("API no customer") is None, r.text[:200]
        r = api_create(PKG, customer_id="999999", name="API unknown customer")
        assert r.status_code == 404, r.text[:200]
        r = api_create(PKG, customer_id=str(cid), project_id="1", name="API both")
        assert r.status_code == 400 and "not both" in r.json()["error"], r.text[:200]
        r = api_create(broken, customer_id=str(cid), name="API broken")
        assert r.status_code in (413, 422), (r.status_code, r.text[:200])
        assert project_named("API broken") is None, "a failed import left a project"
        r = cl.post(
            "/api/documents",
            data={"sdl_new_project": "1", "customer_id": str(cid)},
            files={"file": ("notes.txt", b"hello", "text/plain")},
            headers=tok,
        )
        assert r.status_code == 400, r.text[:200]
    print("  [create] web form + API sdl_new_project create a project from a package OK")


def main():
    test_registration()
    test_inspect()
    test_member_blob_and_identity()
    test_hostile()
    test_import_and_return_package()
    test_web_and_api()
    test_create_project_web_and_api()
    test_cli()
    print("SDL PACKAGE FILTER OK")


if __name__ == "__main__":
    main()
