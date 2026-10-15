"""Request and upload size limits (security review 2026-09, train F7 fix-request-limits).

One suite for the five denial-of-service findings the train fixes. Every test here was
run against the base code and failed there (the base-red record is in the request-limits reply
notes):

* #287 request body caps: an anonymous oversized body (sign-in form, /api JSON with no key,
  a chunked body, a web form with no session) is a clean 413 before any of it is read; a
  signed-in caller keeps the large cap (normal uploads unchanged) and is refused past it.
* #275 upload read caps: no web route reads an UploadFile whole (a source ratchet), the
  segmentation-profile SRX upload reads in bounded chunks, and the key import refuses an
  oversized JSON body with 413 before parsing it.
* #271 regex backtracking: the Markdown skip-line and image patterns and the SVG tag split
  are linear (timing ratchets on the pathological inputs, plus equivalence with the old
  patterns on normal input), and the upload parse runs off the event loop.
* #272 PDF render caps: a huge blank page is rendered with its long edge at most
  PDF_OCR_MAX_PIXEL_DIM, a normal page at the usual 150 dpi, and a PDF over PDF_MAX_PAGES
  is refused (filter and /api 413).
* #224 zip bombs: a DOCX or XLSX whose parts inflate past the ratio, total-size or member
  limits is refused at parse, at export (build), by the gold-rules XLSX loader, and at the
  web, /api and CLI upload doors; a normal DOCX/XLSX parses as before.
"""

import asyncio
import io
import os
import re
import sys
import time
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_reqlim_")

import pypdfium2 as pdfium
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Role
from app.services import apikeys, content, security, settings
from tests._csrf import csrf_for

init_db()

PW = "reqlim-password-123456"  # pregate: allow-secret throwaway test-user password
_N = [0]
MIB = 1024 * 1024
# Read through getattr so this suite also RUNS on the base code (where these settings do
# not exist yet) and fails there on behaviour, which is the base-red proof.
ANON_CAP = getattr(config, "REQUEST_MAX_ANON_BODY_BYTES", MIB)
PDF_PX_CAP = getattr(config, "PDF_OCR_MAX_PIXEL_DIM", 4000)
FORM_HEAD = b"email=x&password=y&pad="  # pregate: allow-secret dummy sign-in form field


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _project(name=None) -> int:
    with Session(engine) as s:
        p = content.project_create(
            s,
            name or f"ReqLim {_uniq()}",
            source_locale="en-US",
            target_locales="de-DE",
        )
        return p.id


def _user(role=Role.ADMIN):
    email = f"reqlim{_uniq()}@example.com"
    return security.create_user(email, email, role, PW), email


def _login(email) -> TestClient:
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code, r.text[:200])
    csrf_for(c)
    return c


def _is_limit_error(exc) -> bool:
    return type(exc).__name__ == "InputLimitError"


class _Override:
    """Temporarily set config attributes (restored on exit)."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = getattr(config, k, None)
            setattr(config, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(config, k, v)


# --- fixtures: files -----------------------------------------------------------------
def _docx(extra: dict | None = None) -> bytes:
    from docx import Document

    d = Document()
    d.add_paragraph("Hello world, this is a normal document.")
    buf = io.BytesIO()
    d.save(buf)
    return _with_members(buf.getvalue(), extra or {})


def _xlsx(extra: dict | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "source"
    ws["B1"] = "rule"
    ws["A2"] = "Hello spreadsheet"
    ws["B2"] = "keep it short"
    ws["C1"] = "target_locale"
    ws["C2"] = "de-DE"
    buf = io.BytesIO()
    wb.save(buf)
    return _with_members(buf.getvalue(), extra or {})


def _with_members(blob: bytes, extra: dict) -> bytes:
    """Re-pack an OOXML package with extra (deflated) members added."""
    if not extra:
        return blob
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        for name, data in extra.items():
            dst.writestr(name, data)
    return out.getvalue()


def _bomb_member() -> dict:
    # 6 MiB of zeros deflates to a few KB: a ratio near 1000.
    return {"word/media/bomb.bin": b"\0" * (6 * MIB)}


def _pdf(pages) -> bytes:
    doc = pdfium.PdfDocument.new()
    for w, h in pages:
        doc.new_page(w, h)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# =====================================================================================
# #287 request body caps
# =====================================================================================
def test_anonymous_oversized_bodies_are_413_before_reading():
    _auth(True)
    _user()  # an account exists, so the sign-in wall is live
    anon = TestClient(app)
    csrf_for(anon)  # a real pre-login session with a CSRF token, like a browser
    big = b"a" * (ANON_CAP + 4096)

    # 1. the sign-in form (the CSRF check used to read this whole body first)
    r = anon.post(
        "/auth/login",
        content=FORM_HEAD + big,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 413, ("sign-in form", r.status_code, r.text[:200])

    # 2. /api JSON with no key (FastAPI used to parse it before the key check)
    r = TestClient(app).post(
        "/api/translate/text",
        content=b'{"text":"' + big + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413, ("api json no key", r.status_code, r.text[:200])
    assert r.json().get("ok") is False and "too large" in r.json().get("error", ""), r.text

    # 3. a chunked body (no Content-Length) is counted as it streams; the sign-in form's
    # CSRF check reads the body, so the read itself is what gets cut off
    def chunks():
        yield FORM_HEAD
        for _ in range(8):
            yield b"b" * (ANON_CAP // 4)

    r = anon.post(
        "/auth/login",
        content=chunks(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}, r.request.headers
    assert r.status_code == 413, ("chunked", r.status_code, r.text[:200])

    # 4. a web multipart form with no signed-in session
    pid = _project()
    r = anon.post(
        f"/projects/{pid}/documents",
        files={"file": ("big.txt", big, "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 413, ("web form, no session", r.status_code, r.text[:200])

    # 5. a signed-in browser still gets the small cap on /auth paths
    _u, email = _user()
    c = _login(email)
    r = c.post(
        "/auth/password-change",
        content=b"pad=" + big,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 413, ("signed-in /auth", r.status_code)

    # 6. an ordinary small sign-in is unchanged (the _login helper above proved it too)
    wrong = {"email": "nobody@example.com", "password": "wrong"}  # pregate: allow-secret dummy
    r = anon.post("/auth/login", data=wrong)
    assert r.status_code != 413, r.status_code
    _auth(False)
    print("  anonymous oversized bodies: 413 before reading ok")


def test_signed_in_callers_keep_the_upload_cap_and_are_refused_past_it():
    _auth(True)
    user, email = _user()
    _row, raw_key = apikeys.create_key(user.id, "reqlim", scopes="api docs")
    pid = _project()
    # a 2 MiB text file: over the anon cap, and under the #571 segment cap (about 28000
    # lines; 160000 short lines would be refused as too many segments)
    line = b"Hello world, this is a longer line that keeps the segment count low.\n"
    body = line * ((2 * MIB) // len(line))

    # a signed-in web upload over the anonymous cap works exactly as before
    c = _login(email)
    r = c.post(
        f"/projects/{pid}/documents",
        files={"file": ("normal.txt", body, "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/documents/" in r.headers["location"], (
        r.status_code,
        r.headers.get("location"),
    )

    # an API key holder too
    api = TestClient(app)
    r = api.post(
        "/api/documents",
        headers={"X-Loc-Token": raw_key},
        data={"project_id": str(pid)},
        files={"file": ("normal2.txt", body, "text/plain")},
    )
    assert r.status_code == 200 and r.json()["ok"], (r.status_code, r.text[:300])

    # ... and both are refused past the signed-in cap
    with _Override(REQUEST_MAX_BODY_BYTES=MIB + MIB // 2):
        r = c.post(
            f"/projects/{pid}/documents",
            files={"file": ("big.txt", body, "text/plain")},
            follow_redirects=False,
        )
        assert r.status_code == 413, ("web past cap", r.status_code)
        r = api.post(
            "/api/documents",
            headers={"X-Loc-Token": raw_key},
            data={"project_id": str(pid)},
            files={"file": ("big.txt", body, "text/plain")},
        )
        assert r.status_code == 413, ("api past cap", r.status_code)
    # a WRONG key gets the anonymous cap (413, not a parse and then 401)
    r = api.post(
        "/api/documents",
        headers={"X-Loc-Token": "_".join(("lsu", "not", "a", "real", "key"))},
        data={"project_id": str(pid)},
        files={"file": ("x.txt", body, "text/plain")},
    )
    assert r.status_code == 413, ("bad key", r.status_code)
    _auth(False)
    print("  signed-in callers keep the upload cap, refused past it ok")


def test_upload_reports_segments_rows_and_locales_separately():
    """#581: POST /api/documents no longer answers an ambiguous `segments` count (it used
    to report rows = segments x target locales). It now reports the distinct source-segment
    count in `segments`, the row count in `translation_rows`, and the locale count in
    `target_locales`."""
    _auth(True)
    try:
        user, _email = _user()
        _row, raw_key = apikeys.create_key(user.id, "reqlim-cnt", scopes="api docs")
        api = TestClient(app)
        # two non-empty lines -> 2 source segments (the blank line is not a segment)
        body = b"First paragraph.\n\nSecond paragraph.\n"

        def _upload(pid: int) -> dict:
            r = api.post(
                "/api/documents",
                headers={"X-Loc-Token": raw_key},
                data={"project_id": str(pid)},
                files={"file": ("two.txt", body, "text/plain")},
            )
            assert r.status_code == 200, (r.status_code, r.text[:300])
            j = r.json()
            assert j.get("ok"), j
            return j

        # three target locales: 2 source segments x 3 = 6 rows
        with Session(engine) as s:
            pid3 = content.project_create(
                s,
                f"ReqLim 3loc {_uniq()}",
                source_locale="en-US",
                target_locales="de-DE,fr-FR,ja-JP",
            ).id
        j3 = _upload(pid3)
        assert (j3["segments"], j3["translation_rows"], j3["target_locales"]) == (2, 6, 3), j3

        # one target locale: 2 segments -> 2 rows, 1 locale (segments == translation_rows)
        with Session(engine) as s:
            pid1 = content.project_create(
                s,
                f"ReqLim 1loc {_uniq()}",
                source_locale="en-US",
                target_locales="de-DE",
            ).id
        j1 = _upload(pid1)
        assert (j1["segments"], j1["translation_rows"], j1["target_locales"]) == (2, 2, 1), j1
    finally:
        _auth(False)
    print("  upload reports segments / translation_rows / target_locales separately ok")


# =====================================================================================
# #275 upload read caps
# =====================================================================================
def test_no_web_route_reads_an_upload_whole():
    """Source ratchet: an UploadFile is read through deps.read_capped_upload, never with
    a bare `.read()` that buffers the whole file first."""
    offenders = []
    web = os.path.join(_ROOT, "app", "web")
    pat = re.compile(r"await\s+[\w.]+\.read\(\s*\)")
    for name in sorted(os.listdir(web)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(web, name), encoding="utf-8") as fh:
            for no, line in enumerate(fh, 1):
                if pat.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"app/web/{name} line {no}: {line.strip()}")
    assert not offenders, "unbounded upload reads:\n" + "\n".join(offenders)
    print("  no unbounded UploadFile.read() in app/web ok")


def test_segprofile_srx_upload_is_read_in_bounded_chunks():
    _auth(False)
    from starlette.datastructures import UploadFile

    sizes = []
    orig = UploadFile.read

    async def spy(self, size=-1):
        sizes.append(size)
        return await orig(self, size)

    UploadFile.read = spy
    try:
        c = TestClient(app)
        r = c.post(
            "/settings/segprofiles",
            data={"name": f"Big SRX {_uniq()}", "description": "", "customer_id": ""},
            files={"srx_upload": ("big.srx", b"<" + b"x" * (2 * MIB), "application/xml")},
            follow_redirects=False,
        )
    finally:
        UploadFile.read = orig
    assert r.status_code == 303, r.status_code
    assert "too large" in unquote(r.headers["location"]), r.headers["location"]
    assert sizes and all(s is not None and s > 0 for s in sizes), sizes
    print("  segmentation-profile SRX upload bounded ok")


def test_key_import_refuses_an_oversized_body_before_parsing():
    from app.models import ProjectType

    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    _auth(True)
    user, _email = _user()
    _row, raw_key = apikeys.create_key(user.id, "reqlim-keys", scopes="api keys")
    with Session(engine) as s:
        p = content.project_create(
            s,
            f"ReqLim keys {_uniq()}",
            source_locale="en-US",
            target_locales="de-DE",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
    api = TestClient(app)
    # normal size: unchanged
    r = api.post(
        f"/api/projects/{pid}/keys/import",
        headers={"X-Loc-Token": raw_key},
        json={"filename": "en.json", "data": '{"hello": "Hello"}', "locale": "en-US"},
    )
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text[:300])
    # over the cap: 413 from the bounded read, not a parse and then a 400
    with _Override(MAX_UPLOAD_BYTES=4096):
        blob = b'{"filename":"x.json","data":"' + b"z" * (200 * 1024) + b'"}'
        r = api.post(
            f"/api/projects/{pid}/keys/import",
            headers={"X-Loc-Token": raw_key, "Content-Type": "application/json"},
            content=blob,
        )
    assert r.status_code == 413, (r.status_code, r.text[:300])
    assert "upload cap" in r.json()["error"], r.text
    _auth(False)
    settings.set_many({"key_projects_enabled": "0"})
    settings.invalidate_cache()
    print("  key import bounded before parsing ok")


# =====================================================================================
# #271 regex backtracking, parse off the event loop
# =====================================================================================
def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def test_markdown_patterns_are_linear():
    from app.filters.markdown_filter import MarkdownFilter

    md = MarkdownFilter()
    # the old skip pattern was cubic on a run of spaces then a letter (1200 -> ~5 s)
    spaces = " " * 1500 + "a\n"
    took = _timed(lambda: md.parse(spaces.encode()))
    assert took < 1.0, f"skip-line pattern took {took:.2f}s"
    # the old image pattern was quadratic on repeated "![" (60 KB -> ~8 s)
    images = "text " + "![" * 30000 + "\n"
    took = _timed(lambda: md.parse(images.encode()))
    assert took < 1.0, f"image pattern took {took:.2f}s"
    # normal Markdown: rules, tables, images unchanged
    doc = (
        "# Title\n\n---\n\n| a | b |\n|:--|--:|\n| one | two |\n\n"
        "Text with ![alt text](img.png) inline.\n\n***\n"
    )
    units = [u.text for u in md.parse(doc.encode()).units]
    assert "Title" in units and "| one | two |" in units, units
    assert not any(set(u) <= set("-*|: ") for u in units), units
    assert any(u.startswith("Text with") for u in units), units
    print("  markdown patterns linear ok")


def test_svg_tag_split_is_linear_and_unchanged():
    from app.filters import image_filter

    filt = image_filter.ImageFilter()
    # many "<" and no ">" after them: the old split rescanned to the end from each one
    hostile = ("<svg><text>" + "<a" * 60000).encode()
    took = _timed(lambda: filt._parse_svg(hostile))
    assert took < 1.0, f"svg split took {took:.2f}s"
    sample = '<svg x="1"><text>Hi <tspan>there</tspan> a < b</text><!-- c<d --><></svg>'
    split = getattr(image_filter, "_split_tags", image_filter._TOKEN_RE.split)
    assert split(sample) == image_filter._TOKEN_RE.split(sample)
    units = [u.text for u in filt._parse_svg(b"<svg><text>Hello SVG</text></svg>").units]
    assert units == ["Hello SVG"], units
    print("  svg tag split linear and unchanged ok")


def test_html_tokenizer_stays_linear():
    from app.filters.html_filter import HtmlFilter

    hostile = ("<p>" + "<a" * 100000 + "</p>").encode()
    took = _timed(lambda: HtmlFilter().parse(hostile))
    assert took < 2.0, f"html parse took {took:.2f}s"
    print("  html tokenizer linear ok")


def test_upload_parse_runs_off_the_event_loop():
    _auth(False)
    from app.filters.text_filter import TextFilter

    seen = []
    orig = TextFilter.parse

    def spy(self, data, provider=None):
        try:
            asyncio.get_running_loop()
            seen.append("event loop")
        except RuntimeError:
            seen.append("worker thread")
        return orig(self, data, provider)

    TextFilter.parse = spy
    try:
        pid = _project()
        c = TestClient(app)
        r = c.post(
            f"/projects/{pid}/documents",
            files={"file": ("t.txt", b"Hello thread.", "text/plain")},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        user, _ = _user()
        _row, raw_key = apikeys.create_key(user.id, "reqlim-thread", scopes="api docs")
        r = c.post(
            "/api/documents",
            headers={"X-Loc-Token": raw_key},
            data={"project_id": str(pid)},
            files={"file": ("t2.txt", b"Hello API thread.", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.text[:200])
    finally:
        TextFilter.parse = orig
    assert seen == ["worker thread", "worker thread"], seen
    print("  upload parse runs in a worker thread ok")


# =====================================================================================
# #272 PDF render caps
# =====================================================================================
def test_pdf_render_is_bounded_by_page_size():
    from app.filters.pdf_filter import PdfFilter

    rendered = []
    orig = pdfium.PdfPage.render

    def spy(self, *a, **kw):
        w, h = self.get_size()
        scale = kw.get("scale", a[0] if a else 1)
        rendered.append((round(w * scale), round(h * scale)))
        kw["scale"] = 0.05  # keep the test itself light
        return orig(self, **kw)

    pdfium.PdfPage.render = spy
    try:
        PdfFilter().parse(_pdf([(3000, 3000)]))  # the probe's 551-byte blank page
        PdfFilter().parse(_pdf([(595, 842)]))  # a blank A4 page
    finally:
        pdfium.PdfPage.render = orig
    (big_w, big_h), (a4_w, a4_h) = rendered
    assert max(big_w, big_h) <= PDF_PX_CAP, rendered
    # a normal page still renders at 150 dpi
    assert (a4_w, a4_h) == (round(595 * 150 / 72), round(842 * 150 / 72)), rendered
    print("  pdf render bounded by page size ok")


def test_pdf_page_count_is_capped():
    _auth(False)
    from app.filters.pdf_filter import PdfFilter

    blob = _pdf([(595, 842)] * 4)
    with _Override(PDF_MAX_PAGES=3):
        try:
            PdfFilter().parse(blob)
            raise AssertionError("a 4-page PDF passed a 3-page cap")
        except Exception as exc:
            assert _is_limit_error(exc) and "4 pages" in str(exc), repr(exc)
        user, _ = _user()
        _row, raw_key = apikeys.create_key(user.id, "reqlim-pdf", scopes="api docs")
        r = TestClient(app).post(
            "/api/documents",
            headers={"X-Loc-Token": raw_key},
            data={"project_id": str(_project())},
            files={"file": ("many.pdf", blob, "application/pdf")},
        )
    assert r.status_code == 413 and "limit is 3" in r.json()["error"], (r.status_code, r.text)
    print("  pdf page cap ok")


# =====================================================================================
# #224 zip bombs (DOCX, XLSX)
# =====================================================================================
def test_normal_ooxml_parses_unchanged():
    from app.filters.docx_filter import DocxFilter
    from app.filters.xlsx_filter import XlsxFilter

    units = [u.text for u in DocxFilter().parse(_docx()).units]
    assert units == ["Hello world, this is a normal document."], units
    units = [u.text for u in XlsxFilter().parse(_xlsx()).units]
    assert "Hello spreadsheet" in units, units
    print("  normal docx/xlsx unchanged ok")


def test_docx_and_xlsx_bombs_are_refused_at_parse_and_build():
    from app.filters.docx_filter import DocxFilter
    from app.filters.xlsx_filter import XlsxFilter

    cases = [
        ("docx parse", lambda: DocxFilter().parse(_docx(_bomb_member()))),
        ("docx build", lambda: DocxFilter().build(_docx(_bomb_member()), {})),
        ("xlsx parse", lambda: XlsxFilter().parse(_xlsx(_bomb_member()))),
        ("xlsx build", lambda: XlsxFilter().build(_xlsx(_bomb_member()), {})),
    ]
    for label, fn in cases:
        try:
            fn()
            raise AssertionError(f"{label}: a zip bomb was accepted")
        except Exception as exc:
            assert _is_limit_error(exc) and "compression bomb" in str(exc), (label, repr(exc))
    # total uncompressed size and member count, each over its (lowered) limit
    with _Override(ZIP_MAX_UNCOMPRESSED_BYTES=2 * MIB):
        try:
            DocxFilter().parse(_docx({"word/media/big.bin": os.urandom(3 * MIB)}))
            raise AssertionError("total-size limit not enforced")
        except Exception as exc:
            assert _is_limit_error(exc) and "expands to" in str(exc), repr(exc)
    with _Override(ZIP_MAX_MEMBERS=5):
        try:
            XlsxFilter().parse(_xlsx())
            raise AssertionError("member-count limit not enforced")
        except Exception as exc:
            assert _is_limit_error(exc) and "internal parts" in str(exc), repr(exc)
    print("  docx/xlsx bombs refused at parse and build ok")


def test_every_upload_door_refuses_a_docx_bomb_cleanly():
    _auth(False)
    bomb = _docx(_bomb_member())
    pid = _project()
    c = TestClient(app)
    # web
    r = c.post(
        f"/projects/{pid}/documents",
        files={"file": ("bomb.docx", bomb, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert "compression bomb" in unquote(r.headers["location"]), r.headers["location"]
    # /api (the MCP upload tool and the CLI --remote arm use this route)
    user, _ = _user()
    _row, raw_key = apikeys.create_key(user.id, "reqlim-bomb", scopes="api docs")
    r = c.post(
        "/api/documents",
        headers={"X-Loc-Token": raw_key},
        data={"project_id": str(pid)},
        files={"file": ("bomb.docx", bomb, "application/octet-stream")},
    )
    assert r.status_code == 413 and "compression bomb" in r.json()["error"], (
        r.status_code,
        r.text[:300],
    )
    # CLI (local)
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="reqlim_cli_"), "bomb.docx")
    with open(path, "wb") as fh:
        fh.write(bomb)
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = cli_main(["doc", "upload", str(pid), path])
    assert rc != 0 and "compression bomb" in buf.getvalue(), (rc, buf.getvalue())
    print("  web, /api and CLI refuse a docx bomb cleanly ok")


def test_zip_forged_sizes():
    import struct
    import zlib

    from app.filters import zip_guard
    from app.filters.base import InputLimitError

    for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", method) as zf:
            zf.writestr("word/media/hidden.bin", b"x" * (6 * MIB))
        blob = bytearray(buf.getvalue())
        central = blob.index(b"PK\x01\x02")
        # CRC matches the declared prefix: ZipExtFile can silently accept the lie.
        crc = zlib.crc32(b"x")
        struct.pack_into("<I", blob, 14, crc)
        struct.pack_into("<I", blob, 22, 1)
        struct.pack_into("<I", blob, central + 16, crc)
        struct.pack_into("<I", blob, central + 24, 1)
        try:
            zip_guard.check(bytes(blob))
        except InputLimitError:
            continue
        raise AssertionError(f"forged ZIP sizes accepted, compression {method}")


def test_xlsx_structure_before_library_load():
    from unittest.mock import patch

    from app.filters.base import InputLimitError
    from app.filters.xlsx_filter import XlsxFilter

    namespace = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheets = [
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Hi</t></is></c>'
        '</row><row r="1048576"><c r="XFD1048576" t="inlineStr">'
        "<is><t>Far</t></is></c></row></sheetData>",
        '<sheetData/><mergeCells><mergeCell ref="A1:XFD1048576"/></mergeCells>',
        '<dimension ref="A1:XFD1048576"/><sheetData/>',
        '<sheetData><row r="1"><notCell r="XFD1048576" t="inlineStr">'
        "<is><t>Far</t></is></notCell></row></sheetData>",
        '<sheetData/><mergeCells><mergeCell ref="A1:IV900"/>'
        '<mergeCell ref="A1:IV900"/><mergeCell ref="A1:IV900"/></mergeCells>',
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'
        + "a" * 32768
        + "</t></is></c></row></sheetData>",
    ]
    cases = [("worksheet", sheet) for sheet in sheets] + [("unexpectedRoot", sheets[0])]
    for root_tag, sheet in cases:
        src = _xlsx()
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(src)) as zf, zipfile.ZipFile(out, "w") as dst:
            for info in zf.infolist():
                data = (
                    f"<{root_tag} {namespace}>{sheet}</{root_tag}>".encode()
                    if info.filename == "xl/worksheets/sheet1.xml"
                    else zf.read(info)
                )
                dst.writestr(info.filename, data)
        blob = out.getvalue()
        for action in (
            lambda blob=blob: XlsxFilter().parse(blob),
            lambda blob=blob: XlsxFilter().build(blob, {}),
        ):
            with (
                patch(
                    "app.filters.xlsx_filter.load_workbook",
                    side_effect=AssertionError("unsafe workbook reached openpyxl"),
                ),
                patch(
                    "openpyxl.load_workbook",
                    side_effect=AssertionError("unsafe workbook reached openpyxl"),
                ),
            ):
                try:
                    action()
                except InputLimitError as exc:
                    assert "limit" in str(exc).lower(), str(exc)
                    continue
                raise AssertionError("unsafe workbook accepted")


def test_svg_whitespace_tag_growth():
    from app.filters.image_filter import ImageFilter

    filt = ImageFilter()
    timings = []
    for n in (2000, 4000, 8000):
        blob = ("<svg><" + " " * n + "><text>Hello</text></svg>").encode()
        timings.append(_timed(lambda blob=blob: (filt.parse(blob), filt.build(blob, {}))))
    print("  SVG whitespace parse/build timings:", timings)
    assert timings[-1] < 0.4, timings


def test_xlsx_ignores_nonworksheet_xml():
    from app.filters.xlsx_filter import XlsxFilter

    # A normal embedded SVG may have a doctype; only spreadsheet XML needs this preflight.
    blob = _xlsx({"xl/media/image.svg": b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'})
    assert "Hello spreadsheet" in [u.text for u in XlsxFilter().parse(blob).units]


def test_xlsx_shared_string_expansion_budget():
    from unittest.mock import patch

    from app.filters.base import InputLimitError
    from app.filters.xlsx_filter import XlsxFilter

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    cells = "".join(f'<c r="A{n}" t="s"><v>0</v></c>' for n in range(1, 5))
    sheet = f'<worksheet xmlns="{ns}"><sheetData><row r="1">{cells}</row></sheetData></worksheet>'
    text = "a" * 32
    shared = f'<sst xmlns="{ns}"><si><t>{text}</t></si></sst>'
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_xlsx())) as zf, zipfile.ZipFile(out, "w") as dst:
        for info in zf.infolist():
            data = zf.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = sheet.encode()
            elif info.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
                    b'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
                )
            dst.writestr(info.filename, data)
        dst.writestr("xl/sharedStrings.xml", shared)
    blob = out.getvalue()
    negative = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf, zipfile.ZipFile(negative, "w") as dst:
        for info in zf.infolist():
            dst.writestr(info.filename, zf.read(info).replace(b"<v>0</v>", b"<v>-1</v>"))
    for candidate in (blob, negative.getvalue()):
        with (
            _Override(XLSX_MAX_TOTAL_TEXT_CHARS=100),
            patch(
                "app.filters.xlsx_filter.load_workbook",
                side_effect=AssertionError("repeated shared text reached openpyxl"),
            ),
        ):
            try:
                XlsxFilter().parse(candidate)
            except InputLimitError:
                pass
            else:
                raise AssertionError("shared text expansion accepted")
    with _Override(XLSX_MAX_TOTAL_TEXT_CHARS=1000):
        assert [u.text for u in XlsxFilter().parse(blob).units] == [text] * 4


def test_asgi_counting_and_streaming():
    from app.web.body_limit import BodyLimitMiddleware, BodyTooLarge

    async def exercise(length, streaming=False, payload=b"x" * 33):
        sent = []
        delivered = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            sent.append(message)

        async def downstream(scope, receive, send):
            if streaming:
                await send({"type": "http.response.start", "status": 200, "headers": []})
            delivered.append(await receive())
            if not streaming:
                await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        scope = {
            "type": "http",
            "path": "/auth/login",
            "root_path": "",
            "headers": [] if length is None else [(b"content-length", length)],
        }
        await BodyLimitMiddleware(downstream)(scope, receive, send)
        return sent, delivered

    with _Override(REQUEST_MAX_ANON_BODY_BYTES=32):
        for length in (b"1", b"32", b"-1", None):
            sent, delivered = asyncio.run(exercise(length))
            assert sent[0]["status"] == 413 and not delivered, (length, sent)
        sent, delivered = asyncio.run(exercise(b"2", streaming=True, payload=b"ok"))
        assert sent[0]["status"] == 200 and sent[-1]["body"] == b"ok" and delivered
        try:
            asyncio.run(exercise(None, streaming=True))
        except BodyTooLarge:
            pass
        else:
            raise AssertionError("over-cap streaming read did not abort the connection")


def test_xlsx_shared_formula_expansion_budget():
    from unittest.mock import patch

    from app.filters.base import InputLimitError
    from app.filters.xlsx_filter import XlsxFilter

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    formula = "1+" * 15 + "1"
    cells = f'<c r="A1"><f t="shared" si="0">{formula}</f><v>0</v></c>'
    cells += "".join(f'<c r="A{n}"><f t="shared" si="0"/><v>0</v></c>' for n in range(2, 6))
    sheet = f'<worksheet xmlns="{ns}"><sheetData><row r="1">{cells}</row></sheetData></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_xlsx())) as zf, zipfile.ZipFile(out, "w") as dst:
        for info in zf.infolist():
            dst.writestr(
                info.filename,
                sheet.encode() if info.filename == "xl/worksheets/sheet1.xml" else zf.read(info),
            )
    blob = out.getvalue()
    with (
        _Override(XLSX_MAX_TOTAL_TEXT_CHARS=100),
        patch(
            "app.filters.xlsx_filter.load_workbook",
            side_effect=AssertionError("shared formula expansion reached openpyxl"),
        ),
    ):
        try:
            XlsxFilter().parse(blob)
        except InputLimitError as exc:
            assert "text" in str(exc) and "limit" in str(exc), str(exc)
        else:
            raise AssertionError("shared formula expansion accepted")
    with _Override(XLSX_MAX_TOTAL_TEXT_CHARS=1000):
        assert not XlsxFilter().parse(blob).units


def test_machine_limit_envelopes():
    import json

    from app.web.body_limit import _too_large_response

    webhook = _too_large_response("/webhooks/vendor-returns", 32)
    assert webhook.media_type == "application/json", webhook.media_type
    scim = _too_large_response("/scim/v2/Users", 32)
    assert scim.media_type == "application/scim+json", scim.media_type
    assert json.loads(scim.body)["status"] == "413"


def test_pdf_rotated_crop_bitmap_bounds():
    from app.filters.pdf_filter import PdfFilter

    rendered = []
    original = pdfium.PdfPage.render

    def spy(self, *args, **kwargs):
        bitmap = original(self, *args, **kwargs)
        rendered.append((bitmap.width, bitmap.height))
        return bitmap

    from unittest.mock import patch

    for rotation, cropped in ((0, False), (90, False), (270, True)):
        doc = pdfium.PdfDocument.new()
        page = doc.new_page(14400, 10000)
        page.set_rotation(rotation)
        if cropped:
            page.set_cropbox(0, 0, 600, 800)
        page.close()
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        with _Override(PDF_OCR_MAX_PIXEL_DIM=128), patch.object(pdfium.PdfPage, "render", spy):
            PdfFilter().parse(buf.getvalue())
    assert len(rendered) == 3 and all(max(size) <= 128 for size in rendered), rendered


def test_offload_busy_and_context():
    from contextvars import ContextVar

    from app.web import deps
    from app.web.streaming import stream_slot

    marker = ContextVar("review_parse_context", default="missing")

    async def exercise():
        called = []
        marker.set("caller")

        def work():
            called.append(marker.get())
            return "parsed"

        async with stream_slot() as admitted:
            assert admitted
            try:
                await deps.offload_parse(work)
            except deps.WorkerBusy:
                pass
            else:
                raise AssertionError("busy pool admitted a parse")
            assert not called
        assert await deps.offload_parse(work) == "parsed"
        assert called == ["caller"]
        response = deps.busy_response()
        assert response.status_code == 503 and int(response.headers["Retry-After"]) >= 1

    with _Override(STREAM_WORKERS=1, STREAM_WAIT_S=0.01):
        asyncio.run(exercise())


def test_export_safety_refusals():
    import tempfile

    from sqlmodel import select

    from app.models import Segment

    _auth(False)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post(
        f"/projects/{_project()}/documents",
        files={"file": ("safe.docx", _docx(), "application/octet-stream")},
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])
    with Session(engine) as session:
        for segment in session.exec(select(Segment).where(Segment.document_id == doc_id)):
            segment.target_text = "Hallo Welt."
            segment.status = "mt"
            session.add(segment)
        session.commit()
    user, _ = _user()
    _, raw = apikeys.create_key(user.id, "export-limits", scopes="api docs")
    failures = []
    with _Override(ZIP_MAX_MEMBERS=1):
        for url in (
            f"/documents/{doc_id}/export?locale=de-DE",
            f"/api/documents/{doc_id}/download?locale=de-DE",
        ):
            r = c.get(url, headers={"X-Loc-Token": raw})
            if r.status_code != 413 or "internal parts" not in r.text:
                failures.append(f"{url}: HTTP {r.status_code}")
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(buf), redirect_stderr(buf):
            try:
                rc = cli_main(
                    [
                        "doc",
                        "export",
                        str(doc_id),
                        "--locale",
                        "de-DE",
                        "--out",
                        os.path.join(folder, "export.docx"),
                    ]
                )
                assert rc != 0 and "internal parts" in buf.getvalue(), buf.getvalue()
            except Exception as exc:
                failures.append(f"CLI raised {type(exc).__name__}")
    assert not failures, failures


def test_empty_upload_is_refused_on_api_web_and_ingest():
    """#573: an empty or whitespace-only .txt is refused (422 on /api, an err= redirect on
    the web, a raised EmptyDocumentError at ingest), never ingested as a 0-segment doc."""
    from sqlmodel import select

    from app.filters.base import EmptyDocumentError
    from app.models import Document, Project
    from app.services.ingest import ingest_document

    _auth(False)
    marker = "no text to translate"
    empty = b""  # 0 bytes
    blank = b"   \t\n \n\t  \n"  # spaces, tabs and newlines only

    def _docs() -> int:
        with Session(engine) as s:
            return len(s.exec(select(Document)).all())

    # /api/documents: 422, ok false, the message, and no Document row for either file.
    user, _ = _user()
    _row, raw_key = apikeys.create_key(user.id, "reqlim-empty", scopes="api docs")
    api = TestClient(app)
    for name, blob in (("a.txt", empty), ("b.txt", blank)):
        before = _docs()
        r = api.post(
            "/api/documents",
            headers={"X-Loc-Token": raw_key},
            data={"project_id": str(_project())},
            files={"file": (name, blob, "text/plain")},
        )
        assert r.status_code == 422, (name, r.status_code, r.text[:200])
        assert r.json().get("ok") is False, (name, r.text[:200])
        assert marker in r.json().get("error", ""), (name, r.text[:200])
        assert _docs() == before, (name, "an empty /api upload created a Document row")

    # a normal one-line text file still ingests (200, 1 segment).
    r = api.post(
        "/api/documents",
        headers={"X-Loc-Token": raw_key},
        data={"project_id": str(_project())},
        files={"file": ("c.txt", b"Hello world.\n", "text/plain")},
    )
    assert r.status_code == 200 and r.json()["ok"] and r.json()["segments"] == 1, (
        r.status_code,
        r.text[:200],
    )

    # web upload route (documents_tm_only_delivery_empty_g.py): the empty file redirects
    # with err= carrying the same message and creates no Document.
    for name, blob in (("a.txt", empty), ("b.txt", blank)):
        pid = _project()
        before = _docs()
        r = TestClient(app).post(
            f"/projects/{pid}/documents",
            files={"file": (name, blob, "text/plain")},
            follow_redirects=False,
        )
        assert r.status_code == 303, (name, r.status_code)
        assert marker in unquote(r.headers["location"]), r.headers.get("location")
        assert _docs() == before, (name, "an empty web upload created a Document row")

    # ingest_document raises EmptyDocumentError directly (the CLI path: its generic
    # `except Exception` already shows this message).
    with Session(engine) as s:
        project = s.get(Project, _project())
        try:
            ingest_document(s, project, "d.txt", blank, "segmented")
            raise AssertionError("an all-blank upload was ingested instead of refused")
        except EmptyDocumentError as exc:
            assert marker in str(exc), str(exc)
    print("  empty/whitespace upload refused on /api, web and ingest ok")


def main():
    failures = []
    for test in (
        test_zip_forged_sizes,
        test_xlsx_structure_before_library_load,
        test_xlsx_ignores_nonworksheet_xml,
        test_xlsx_shared_string_expansion_budget,
        test_xlsx_shared_formula_expansion_budget,
        test_svg_whitespace_tag_growth,
        test_asgi_counting_and_streaming,
        test_machine_limit_envelopes,
        test_pdf_rotated_crop_bitmap_bounds,
        test_offload_busy_and_context,
        test_export_safety_refusals,
    ):
        try:
            test()
        except Exception as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)
    test_anonymous_oversized_bodies_are_413_before_reading()
    test_signed_in_callers_keep_the_upload_cap_and_are_refused_past_it()
    test_upload_reports_segments_rows_and_locales_separately()
    test_no_web_route_reads_an_upload_whole()
    test_segprofile_srx_upload_is_read_in_bounded_chunks()
    test_key_import_refuses_an_oversized_body_before_parsing()
    test_markdown_patterns_are_linear()
    test_svg_tag_split_is_linear_and_unchanged()
    test_html_tokenizer_stays_linear()
    test_upload_parse_runs_off_the_event_loop()
    test_pdf_render_is_bounded_by_page_size()
    test_pdf_page_count_is_capped()
    test_normal_ooxml_parses_unchanged()
    test_docx_and_xlsx_bombs_are_refused_at_parse_and_build()
    test_every_upload_door_refuses_a_docx_bomb_cleanly()
    test_empty_upload_is_refused_on_api_web_and_ingest()
    print("REQUEST LIMITS OK")


if __name__ == "__main__":
    main()
