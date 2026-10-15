"""Non-ASCII request-header hardening (chip task_3cc0e09a).

Starlette decodes request headers as **latin-1**, so a header carrying non-ASCII BYTES
(which a raw client / proxy can send even though httpx rejects non-ASCII *str* values,
accepting *bytes*) becomes a Python str with code points > 0x7f. Two auth/CSRF hot-path
compares used a raw ``secrets.compare_digest(str, str)``, which RAISES ``TypeError:
comparing strings with non-ASCII characters is not supported`` on such input -- surfacing
as an UNAUTHENTICATED **500** (plus an is-set enumeration oracle):

  * ``app/web/deps.py`` ``api_identity`` -- the shared connector-token compare, reached
    when a tokenless-check fails and an ``X-Loc-Token`` is present but matches no key.
  * ``app/main.py`` ``_csrf_ok`` -- the synchronizer-token compare for mutating,
    session-authed, non-/api web requests (``X-CSRF-Token`` header OR ``csrf_token`` form
    field).

Both now go through ``security.constant_time_eq`` (SHA-256-digest constant-time compare,
``surrogatepass``), so a non-ASCII value is a clean MISMATCH -> the honest 4xx (401 for the
token path, 403 for the CSRF path), never a 500. This suite REPRODUCES each formerly-500
path with a real ``bytes([0xe9])`` header and pins the 4xx; a control proves the legitimate
paths still authenticate (no happy-path regression).

A THIRD seam in the same family (arm [3], prod lane-B QA): the single-document download's
RESPONSE header. A document filename with a non-latin-1 character (CJK, emoji) -- which
rename ACCEPTS (only ``"``/CRLF rejected) -- could not latin-1-encode into a bare
``Content-Disposition: attachment; filename="..."`` on the ASGI wire, raising
UnicodeEncodeError -> a 500 on ``GET /api/documents/{id}/download`` (and the web
``/documents/{id}/export`` twin). Both now emit an RFC-6266
``content.content_disposition_attachment`` value (ASCII ``filename=`` fallback +
``filename*=UTF-8''<percent-encoded>``), latin-1-safe by construction.

Hermetic (throwaway LOC_TMS_DATA_DIR, cleared provider keys). ASCII-only prints.
Final line: NON-ASCII HEADERS OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_nonascii_hdr_")
os.environ["LOC_TMS_SERVER"] = "1"  # a normal, non-embedded server boot

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine
from app.main import app
from app.models import Document, Project, Role, Segment
from app.services import apikeys, security, settings
from tests import _csrf

#: One non-ASCII wire byte; Starlette latin-1-decodes it to U+00E9 (a str code point > 0x7f
#: that raw compare_digest cannot compare). Sending it as a BYTES header value is exactly the
#: shape a raw client / appending proxy can put on the wire (httpx rejects non-ASCII str).
NB = bytes([0xE9])


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:200]})"


def main():
    with TestClient(app, raise_server_exceptions=False) as c:
        # ---- [1] deps.api_identity connector-token compare (was a 500) ---------- #
        # A present-but-non-ASCII X-Loc-Token never matches a real key, so api_identity
        # falls to the connector-token compare; that compare must NOT raise a TypeError
        # (unauthenticated 500) -- it must yield a clean mismatch -> 401 unauthorized.
        r = c.get("/api/projects", headers={"X-Loc-Token": NB * 40})
        _is(r, 401, "a non-ASCII X-Loc-Token must be 401 (not a compare_digest 500)")
        assert r.json() == {"ok": False, "error": "unauthorized"}, r.text
        # A 64 KB non-ASCII token is still a clean 401 (never a 500 on a huge value).
        _is(
            c.get("/api/projects", headers={"X-Loc-Token": NB * 65536}),
            401,
            "a 64 KB non-ASCII X-Loc-Token must be 401 (not a 500)",
        )
        # Control: an ASCII bogus token reaches the same terminal 401 (byte-identical body),
        # so the fix did not change the legitimate reject path.
        r2 = c.get("/api/projects", headers={"X-Loc-Token": "lsk_bogus_ascii_token"})
        _is(r2, 401, "an ASCII bogus X-Loc-Token is 401 (unchanged control)")
        assert r2.json() == {"ok": False, "error": "unauthorized"}, r2.text
        print(
            "  [1] deps.api_identity: a non-ASCII X-Loc-Token yields 401 unauthorized "
            "(not a compare_digest 500); the ASCII reject path is byte-identical"
        )

        # ---- [2] main._csrf_ok compare (was a 500) ------------------------------ #
        security.create_user("admin@x.com", "Admin", Role.ADMIN, "admin-password-1234")
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
        assert security.auth_required(), "auth wall must be on for the CSRF path"
        _csrf.csrf_value(c)  # a GET mints session["csrf"] so `expected` is non-empty
        # A non-ASCII X-CSRF-Token header hits the header branch of _csrf_ok's compare, which
        # must yield a clean CSRF MISMATCH -> 403, never a compare_digest TypeError -> 500.
        r = c.post("/projects", headers={"x-csrf-token": NB * 20}, data={"name": "x"})
        _is(r, 403, "a non-ASCII X-CSRF-Token header must be 403 (not a compare_digest 500)")
        # Control A: a WRONG ASCII token reaches the same 403 (so the arm above pins the
        # non-ASCII->403 conversion, not merely a blanket 403 -- the gate really compares).
        r = c.post("/projects", headers={"x-csrf-token": "wrong-ascii-token"}, data={"name": "x"})
        _is(r, 403, "a WRONG ASCII X-CSRF-Token is 403 (unchanged control)")
        # Control B: a VALID csrf token still PASSES the CSRF gate (no happy-path regression)
        # -- it is NOT a 403 (it proceeds past CSRF to the normal auth/route handling).
        tok = _csrf.csrf_value(c)
        r = c.post("/projects", headers={"x-csrf-token": tok}, data={"name": "x"})
        assert r.status_code != 403, (
            f"a VALID csrf token must still pass the CSRF gate, got 403: {r.text[:200]}"
        )
        print(
            "  [2] main._csrf_ok: a non-ASCII X-CSRF-Token header yields 403 (not a 500); a "
            "wrong ASCII token is 403 (the gate really compares); a valid token still passes"
        )

        # ---- [3] single-doc download Content-Disposition (was a 500) ------------ #
        # A non-latin-1 document filename could not encode into a bare filename="..." on the
        # wire -> a 500. Now an RFC-6266 header (ASCII fallback + filename*=UTF-8''<pct>).
        from urllib.parse import quote

        settings.set_many({"security_auth_required": "0"})  # web /export -> single-operator
        settings.invalidate_cache()
        _dluser = security.create_user("dl@x.com", "DL", Role.ADMIN, "dl-password-1234")
        _row, key = apikeys.create_key(_dluser.id, "dl", scopes="api docs")
        H = {"X-Loc-Token": key}

        def _dl_doc(client_filename):
            pid = int(
                c.post(
                    "/projects",
                    data={
                        "name": "P-" + client_filename,
                        "source_locale": "en-US",
                        "target_locales": "fr-FR",
                        "default_provider": "mock",
                    },
                    follow_redirects=False,
                )
                .headers["location"]
                .rsplit("/", 1)[-1]
            )
            up = c.post(
                "/projects/%d/documents" % pid,  # noqa: UP031
                data={"mode": "segmented"},
                files={"file": (client_filename, b"Hello world. A test.", "text/plain")},
                follow_redirects=False,
            )
            did = int(up.headers["location"].rsplit("/", 1)[-1].split("?")[0])
            with Session(engine) as s:
                for sg in s.exec(
                    select(Segment).where(
                        Segment.document_id == did, Segment.target_locale == "fr-FR"
                    )
                ).all():
                    sg.target_text = "traduit"
                    sg.status = "mt"
                    s.add(sg)
                s.commit()
                stored = s.get(Document, did).filename
            return did, stored

        # (a) a NON-ASCII (CJK) filename -> 200 with an RFC-6266 header (was a 500)
        cjk = "文档报告.txt"  # CJK stem
        did, stored = _dl_doc(cjk)
        assert not stored.isascii(), f"the stored filename must really be non-ASCII: {stored!r}"
        r = c.get("/api/documents/%d/download?locale=fr-FR" % did, headers=H)  # noqa: UP031
        _is(r, 200, "a non-ASCII document filename must download 200 (not a header-encode 500)")
        cd = r.headers.get("content-disposition", "")
        cd.encode("latin-1")  # MUST be wire-encodable (the crux) -- no raise
        assert "filename*=UTF-8''" in cd, (
            f"the RFC-6266 filename* parameter must be present: {cd!r}"
        )
        assert quote("文档报告") in cd, (
            f"filename* must carry the percent-encoded UTF-8 stem: {cd!r}"
        )
        assert 'filename="' in cd, f"an ASCII filename= fallback must be present: {cd!r}"
        # the web /export twin shares the helper -> also 200, also wire-encodable
        rw = c.get("/documents/%d/export?locale=fr-FR" % did)  # noqa: UP031
        _is(rw, 200, "the web /export twin of a non-ASCII filename must be 200")
        rw.headers.get("content-disposition", "").encode("latin-1")
        # (b) control: a PLAIN-ASCII filename downloads 200 with the name preserved verbatim
        did2, _ = _dl_doc("report.txt")
        r2 = c.get("/api/documents/%d/download?locale=fr-FR" % did2, headers=H)  # noqa: UP031
        _is(r2, 200, "a plain-ASCII filename must download 200 (control)")
        assert 'filename="report.fr-FR.txt"' in r2.headers.get("content-disposition", ""), (
            "an ASCII filename must appear verbatim in the filename= fallback: {!r}".format(
                r2.headers.get("content-disposition")
            )
        )
        print(
            "  [3] single-doc download: a non-ASCII (CJK) filename downloads 200 with a "
            "latin-1-safe RFC-6266 header (filename= fallback + filename*=UTF-8''<pct>) on "
            "/api AND the web /export twin (not a 500); an ASCII name is preserved verbatim"
        )

        # ---- [4] TM export Content-Disposition (HIGH-1, reviewer 2) ------------- #
        # GET /projects/{id}/tm/export?source=&target= interpolates RAW query params into the
        # header -> a `"` breakout (spoofed filename*/injected parameter) + a 500 on a
        # non-latin-1 value. Now built via content.content_disposition_attachment.
        pid = int(
            c.post(
                "/projects",
                data={
                    "name": "TMx",
                    "source_locale": "en-US",
                    "target_locales": "fr-FR",
                    "default_provider": "mock",
                },
                follow_redirects=False,
            )
            .headers["location"]
            .rsplit("/", 1)[-1]
        )
        for atk, why in (
            ('x"; foo=1', "quote-breakout"),
            ("x\"; filename*=UTF-8''evil", "filename*-spoof"),
            ("文档", "CJK"),
        ):
            r = c.get("/projects/%d/tm/export" % pid, params={"source": atk, "target": "fr-FR"})  # noqa: UP031
            _is(r, 200, f"TM export must be 200 for a {why} source (not a 500/breakout)")
            cd = r.headers.get("content-disposition", "")
            cd.encode("latin-1")  # wire-safe (no non-latin-1 500)
            assert cd.count('"') == 2, f"TM export: injected quote must be neutralised: {cd!r}"
            assert "filename*=UTF-8''" in cd, cd
        # a `"` in the source lands as %22 in filename*, never a literal breakout quote.
        r = c.get("/projects/%d/tm/export" % pid, params={"source": 'a"b', "target": "fr-FR"})  # noqa: UP031
        assert "%22" in r.headers.get("content-disposition", ""), (
            "the injected quote must be percent-encoded in filename*: {!r}".format(
                r.headers.get("content-disposition")
            )
        )
        print(
            "  [4] TM export: raw source/target query params -> RFC-6266 header (quote "
            "neutralised in the fallback, filename*-spoof trapped inside it, CJK 200 not 500)"
        )

        # ---- [5] image original download Content-Disposition (HIGH-2, reviewer 2) --- #
        # GET /images/{id}/original serves the raw doc.filename; an SVG is force-attachment, so
        # a `"` breaks out and a non-latin-1 name 500s. Now via the same helper.
        settings.set_many({"image_localization_enabled": "1"})
        settings.invalidate_cache()
        from app.ports import blobstore

        SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

        def _img_doc(fname):
            with Session(engine) as s:
                proj = Project(name="Img-" + fname[:8], source_locale="en-US", created_by="system")
                s.add(proj)
                s.commit()
                s.refresh(proj)
                d = Document(
                    project_id=proj.id, filename=fname, fmt="svg", status="new", created_by="system"
                )
                s.add(d)
                s.commit()
                s.refresh(d)
                did = d.id
            blobstore.get().put("%d.svg" % did, SVG)  # noqa: UP031
            return did

        did = _img_doc("文档报告.svg")  # CJK-named SVG
        r = c.get("/images/%d/original" % did)  # noqa: UP031
        _is(r, 200, "a CJK-named SVG original must download 200 (not a header-encode 500)")
        cd = r.headers.get("content-disposition", "")
        cd.encode("latin-1")
        assert cd.count('"') == 2 and "filename*=UTF-8''" in cd, cd
        did2 = _img_doc('evil".svg')  # quote-breakout filename
        cd2 = c.get("/images/%d/original" % did2).headers.get("content-disposition", "")  # noqa: UP031
        assert cd2.count('"') == 2 and "%22" in cd2, (
            f"image original: the injected quote must be neutralised + %-encoded: {cd2!r}"
        )
        print(
            "  [5] image original: raw doc.filename -> RFC-6266 header (CJK 200 not 500; a "
            '`"`-breakout name neutralised in the fallback + %%-encoded in filename*)'
        )

        # ---- [6] project ZIP export Content-Disposition (chip task_2e26723b) ----- #
        # GET /projects/{id}/export and /api/projects/{id}/export interpolate a raw ZIP /
        # single-file name (delivery._safe preserves Unicode word chars) -> a non-latin-1
        # PROJECT name 500s on the wire (probe-proven live). Now via the RFC-6266 helper.
        def _proj_with_translated_doc(name):
            pid = int(
                c.post(
                    "/projects",
                    data={
                        "name": name,
                        "source_locale": "en-US",
                        "target_locales": "fr-FR",
                        "default_provider": "mock",
                    },
                    follow_redirects=False,
                )
                .headers["location"]
                .rsplit("/", 1)[-1]
            )
            up = c.post(
                "/projects/%d/documents" % pid,  # noqa: UP031
                data={"mode": "segmented"},
                files={"file": ("hello.txt", b"Hello world. A test.", "text/plain")},
                follow_redirects=False,
            )
            did = int(up.headers["location"].rsplit("/", 1)[-1].split("?")[0])
            with Session(engine) as s:
                for sg in s.exec(
                    select(Segment).where(
                        Segment.document_id == did, Segment.target_locale == "fr-FR"
                    )
                ).all():
                    sg.target_text = "traduit"
                    sg.status = "mt"
                    s.add(sg)
                s.commit()
            return pid

        pid = _proj_with_translated_doc("项目报告")  # CJK project name
        for route, hdrs, label in (
            ("/projects/%d/export" % pid, {}, "web"),  # noqa: UP031
            ("/api/projects/%d/export" % pid, H, "/api"),  # noqa: UP031
        ):
            r = c.get(route, headers=hdrs)
            _is(
                r,
                200,
                f"a CJK project name must export 200 on {label} (not a header-encode 500)",
            )
            cd = r.headers.get("content-disposition", "")
            cd.encode("latin-1")  # wire-safe (the crux)
            assert cd.count('"') == 2 and "filename*=UTF-8''" in cd, cd
            assert quote("项目报告") in cd, (
                f"filename* must carry the percent-encoded project name: {cd!r}"
            )
        # ASCII control: the name is preserved verbatim in the filename= fallback.
        pid2 = _proj_with_translated_doc("AsciiProj")
        r2 = c.get("/api/projects/%d/export" % pid2, headers=H)  # noqa: UP031
        _is(r2, 200, "an ASCII project name exports 200 (control)")
        assert 'filename="AsciiProj_translated.zip"' in r2.headers.get("content-disposition", ""), (
            "an ASCII project name must appear verbatim in the filename= fallback: {!r}".format(
                r2.headers.get("content-disposition")
            )
        )
        print(
            "  [6] project ZIP export: a CJK project name -> RFC-6266 header on BOTH "
            "/projects/{id}/export and /api (200 not 500); an ASCII name preserved verbatim"
        )

    print("NON-ASCII HEADERS OK")


if __name__ == "__main__":
    main()
