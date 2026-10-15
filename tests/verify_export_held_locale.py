"""Held-locale export is refused (chip task_9c665111 / AUDIT-phase2 FINDING 8) -- hermetic.

THE TRAP the Phase-2 skill demo hit: a fr-FR run PAUSED at human_review, every segment
left `new`/uncommitted, and `download?locale=fr-FR` returned English SOURCE with a 200 --
a pure-source file that reads as a finished deliverable. A locale with NO committed
translation is HELD; exporting it must REFUSE (409), never silently ship source.

The guard is a DELIVERY guard (repo law K1: the export guard is a delivery guard, never a
storage guard) -- one shared seam (export.held_locale_reason / assert_deliverable) every
surface calls before build_translated_file, so web / /api / CLI / MCP / the project bundle
refuse identically. It fires ONLY when NOT ONE segment for the locale carries a target; a
PARTIAL delivery (some translated, some source-fallback -- maintainer-approved) is NOT held.

Pinned:
  1. SERVICE: held_locale_reason -> a reason for an all-untranslated locale, None once any
     segment has a target, None for a partial locale. build_translated_file STILL emits
     source (the guard is a pre-check, not a build change) -- so the guard is what
     prevents the silent-source download (fail-before/pass-after).
  2. WEB `/documents/{id}/export` and 3. `/api/documents/{id}/download` -> 409 held; 200
     once translated; a partial locale exports (200).
  4. CLI `doc export` -> nonzero, names "held"; succeeds once translated.
  5. BUNDLE (delivery.export_project): a held locale is SKIP-AND-REPORTED, never
     source-echoed into the ZIP.

Hermetic: throwaway LOC_TMS_DATA_DIR + MockProvider. ASCII-only.
Final line: "EXPORT HELD LOCALE OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_heldexp_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Document, Role, Segment, User
from app.services import apikeys, delivery, export

init_db()
_LOCALE = "fr-FR"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _admin_key():
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "held-admin@x.io")).first()
        if not u:
            u = User(email="held-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "held-api", scopes="api docs")
    return raw


def _project(client, name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": _LOCALE,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(client, pid):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("hello.txt", b"Hello world. This is a test.", "text/plain")},
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _segments(doc_id):
    with Session(engine) as s:
        return list(
            s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == _LOCALE)
                .order_by(Segment.order_index)
            ).all()
        )


def _translate_all(doc_id):
    with Session(engine) as s:
        for sg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == _LOCALE)
        ).all():
            sg.target_text = "traduit"
            sg.status = "mt"
            s.add(sg)
        s.commit()


def _translate_one(doc_id):
    with Session(engine) as s:
        segs = list(
            s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == _LOCALE)
                .order_by(Segment.order_index)
            ).all()
        )
        segs[0].target_text = "traduit"
        segs[0].status = "mt"
        s.add(segs[0])
        s.commit()
        return len(segs)


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue() + err.getvalue()


client = TestClient(app)
H = {"X-Loc-Token": _admin_key()}


# ============================================================================ #
# 1. service seam + fail-before/pass-after
# ============================================================================ #
def test_service_seam():
    pid = _project(client, "held-svc")
    doc_id = _upload(client, pid)
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        reason = export.held_locale_reason(s, doc, _LOCALE)
        if not reason or "held" not in reason.lower():
            _fail(f"an all-untranslated locale must be held, got {reason!r}")
        # fail-before/pass-after: the BUILD still emits source (the guard is a pre-check,
        # not a build change) -- so held_locale_reason is precisely what stops the silent
        # source download the audit saw.
        data = export.build_translated_file(s, doc, _LOCALE)
        if b"Hello world" not in data:
            _fail("build_translated_file was expected to still source-echo a held locale")
        # assert_deliverable raises the typed exception
        try:
            export.assert_deliverable(s, doc, _LOCALE)
        except export.ExportHeldLocale as exc:
            if exc.locale != _LOCALE:
                _fail("ExportHeldLocale must carry the locale")
        else:
            _fail("assert_deliverable must raise on a held locale")

    # once translated -> not held
    _translate_all(doc_id)
    with Session(engine) as s:
        if export.held_locale_reason(s, s.get(Document, doc_id), _LOCALE) is not None:
            _fail("a translated locale must NOT be held")

    # partial -> not held (one committed target is enough)
    pid2 = _project(client, "held-partial")
    doc2 = _upload(client, pid2)
    n = _translate_one(doc2)
    if n < 2:
        _fail("need a 2-segment doc to prove the partial case")
    with Session(engine) as s:
        if export.held_locale_reason(s, s.get(Document, doc2), _LOCALE) is not None:
            _fail("a PARTIAL locale (one committed target) must NOT be held")
    print(
        "  1. service: held all-untranslated, not-held once translated/partial; "
        "build still source-echoes (fail-before) OK"
    )


# ============================================================================ #
# 2 + 3. web + /api document surfaces
# ============================================================================ #
def test_web_and_api_document():
    pid = _project(client, "held-doc")
    doc_id = _upload(client, pid)

    # WEB export -> 409 held, and the body NAMES the way out (review M1 / #42 lesson)
    r = client.get(f"/documents/{doc_id}/export", params={"locale": _LOCALE})
    if r.status_code != 409 or not r.json().get("held"):
        _fail(f"web export of a held locale must 409 held, got {r.status_code} {r.text[:120]}")
    if "allow_held=1" not in (r.json().get("export_anyway_url") or ""):
        _fail(f"the web 409 must name the allow_held override URL: {r.json()}")

    # M1: the explicit override ships the source-filled file (200)
    r = client.get(f"/documents/{doc_id}/export", params={"locale": _LOCALE, "allow_held": 1})
    if r.status_code != 200 or b"Hello world" not in r.content:
        _fail(f"web export with allow_held=1 must ship source (200): {r.status_code}")

    # L2: an UNKNOWN locale for this doc is a clean 404 (not a held-409, not a 200)
    r = client.get(f"/documents/{doc_id}/export", params={"locale": "zz-ZZ"})
    if r.status_code != 404:
        _fail(f"web export of an unknown locale must 404, got {r.status_code}")

    # /api download -> 409 held (the audit's exact case) + override URL
    r = client.get(f"/api/documents/{doc_id}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 409 or not r.json().get("held"):
        _fail(f"/api download of a held locale must 409 held, got {r.status_code} {r.text[:120]}")
    if b"Hello world" in r.content:
        _fail("the 409 body must NOT contain the source text")
    if "allow_held=1" not in (r.json().get("export_anyway_url") or ""):
        _fail(f"the /api 409 must name the allow_held override URL: {r.json()}")
    # M1: /api override ships source (200)
    r = client.get(
        f"/api/documents/{doc_id}/download", headers=H, params={"locale": _LOCALE, "allow_held": 1}
    )
    if r.status_code != 200 or b"Hello world" not in r.content:
        _fail(f"/api download with allow_held=1 must ship source (200): {r.status_code}")

    # translate -> both 200
    _translate_all(doc_id)
    r = client.get(f"/documents/{doc_id}/export", params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"web export of a translated locale must 200, got {r.status_code}")
    r = client.get(f"/api/documents/{doc_id}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"/api download of a translated locale must 200, got {r.status_code}")

    # a PARTIAL doc exports (200) -- source-fallback for the untranslated half
    pid2 = _project(client, "held-doc-partial")
    doc2 = _upload(client, pid2)
    _translate_one(doc2)
    r = client.get(f"/api/documents/{doc2}/download", headers=H, params={"locale": _LOCALE})
    if r.status_code != 200:
        _fail(f"a partial locale must export (200), got {r.status_code}")
    print("  2+3. web + /api: held locale 409, translated 200, partial 200 OK")


# ============================================================================ #
# 4. CLI doc export
# ============================================================================ #
def test_cli():
    pid = _project(client, "held-cli")
    doc_id = _upload(client, pid)
    out_path = os.path.join(_TMP, "held_out.txt")
    rc, out = _run_cli(
        ["doc", "export", str(doc_id), "--locale", _LOCALE, "--out", out_path, "--force"]
    )
    if rc == 0:
        _fail("CLI doc export of a held locale must exit nonzero")
    if "held" not in out.lower():
        _fail(f"CLI refusal must name 'held': {out}")
    if "--allow-held" not in out:
        _fail(f"CLI refusal must name the --allow-held way-out (M1): {out}")
    if os.path.exists(out_path):
        _fail("CLI must NOT write a file for a held locale")

    # M1: --allow-held ships the source-filled file
    rc, out = _run_cli(
        [
            "doc",
            "export",
            str(doc_id),
            "--locale",
            _LOCALE,
            "--out",
            out_path,
            "--force",
            "--allow-held",
        ]
    )
    if rc != 0 or not os.path.exists(out_path):
        _fail(f"CLI doc export --allow-held must write the source file: rc={rc} {out}")
    os.remove(out_path)

    _translate_all(doc_id)
    rc, out = _run_cli(
        ["doc", "export", str(doc_id), "--locale", _LOCALE, "--out", out_path, "--force"]
    )
    if rc != 0:
        _fail(f"CLI doc export of a translated locale must succeed: {out}")
    print("  4. CLI doc export: held refused (no file written), translated succeeds OK")


# ============================================================================ #
# 5. project bundle skip-and-report
# ============================================================================ #
def test_bundle_skip_and_report():
    pid = _project(client, "held-bundle")
    _upload(client, pid)  # held (untranslated)
    with Session(engine) as s:
        try:
            data, _name, _ctype, skipped = delivery.export_project(
                s, pid, locales=[_LOCALE], zip=True
            )
        except ValueError as exc:
            _fail(f"a held bundle must SKIP-and-report, not raise: {exc}")
    held_skips = [x for x in skipped if x["locale"] == _LOCALE and "held" in x["reason"].lower()]
    if not held_skips:
        _fail(f"the held (doc, locale) must be in the skipped manifest: {skipped}")
    if b"Hello world" in data:
        _fail("the bundle must NOT source-echo the held locale into the ZIP")

    # the ZIP skip-report reason NAMES the way out (M1)
    if "allow-held" not in held_skips[0]["reason"] and "allow_held" not in held_skips[0]["reason"]:
        _fail(f"the held skip-report must name the allow-held way-out: {held_skips[0]}")

    # single-file fast path (one doc, one locale, no zip) -> raises ExportHeldLocale
    with Session(engine) as s:
        try:
            delivery.export_project(s, pid, locales=[_LOCALE], zip=False)
        except export.ExportHeldLocale:
            pass
        else:
            _fail("the single-file bundle path must raise ExportHeldLocale for a held locale")
        # M1 (residual a): allow_held ships the source-filled single file
        data2, _name2, _c, _sk = delivery.export_project(
            s, pid, locales=[_LOCALE], zip=False, allow_held=True
        )
        if b"Hello world" not in data2:
            _fail("export_project(allow_held=True) single-file must ship source bytes")

    # /api project export: single-file held -> 409 naming export_anyway_url; override -> 200
    r = client.get(f"/api/projects/{pid}/export", headers=H, params={"locales": _LOCALE})
    if r.status_code != 409 or not r.json().get("held"):
        _fail(f"/api single-file project export of a held locale must 409, got {r.status_code}")
    if "allow_held=1" not in (r.json().get("export_anyway_url") or ""):
        _fail(f"the /api project-export 409 must name allow_held: {r.json()}")
    r = client.get(
        f"/api/projects/{pid}/export", headers=H, params={"locales": _LOCALE, "allow_held": 1}
    )
    if r.status_code != 200 or b"Hello world" not in r.content:
        _fail(f"/api project export with allow_held=1 must ship source: {r.status_code}")
    print(
        "  5. bundle: held skip-and-reported+way-out (ZIP) / raised / allow_held "
        "ships source (single-file + /api) OK"
    )


# ============================================================================ #
# 6. files-04: same-name documents never become duplicate ZIP members
# ============================================================================ #
def test_bundle_unique_member_names():
    import zipfile

    pid = _project(client, "dup-names")
    names = ["guide.txt", "guide.txt", "Guide.txt", "guide?.txt", "guide*.txt"]
    doc_ids = []
    for _n in names:
        d = _upload(client, pid)
        _translate_all(d)
        doc_ids.append(d)
    with Session(engine) as s:
        for d, n in zip(doc_ids, names, strict=True):
            doc = s.get(Document, d)
            doc.filename = n  # force the colliding names (upload may normalise them)
            s.add(doc)
        s.commit()
        data, _name, _ctype, skipped = delivery.export_project(s, pid, locales=[_LOCALE], zip=True)
    if skipped:
        _fail(f"precondition: every document is translated, nothing skipped: {skipped}")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = z.namelist()
        files = [m for m in members if m.startswith(f"{_LOCALE}/")]
        if len(files) != len(names):
            _fail(f"[files-04] every document must be a member: {members}")
        folded = [m.casefold() for m in files]
        if len(set(folded)) != len(folded):
            _fail(f"[files-04] duplicate (case-insensitive) member names: {files}")
        if "_RENAMED.txt" not in members:
            _fail(f"[files-04] a renamed member must be listed in _RENAMED.txt: {members}")
        manifest = z.read("_RENAMED.txt").decode("utf-8")
        if manifest.count(" -> ") != len(names) - 2:
            _fail(f"[files-04] _RENAMED.txt must list each renamed member: {manifest}")
    print("  6. bundle: same-name and same-after-sanitise documents get unique members OK")


def main():
    test_service_seam()
    test_web_and_api_document()
    test_cli()
    test_bundle_skip_and_report()
    test_bundle_unique_member_names()
    print("EXPORT HELD LOCALE OK")


if __name__ == "__main__":
    main()
