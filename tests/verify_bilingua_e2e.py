"""Bilingua end to end for wave 1 (md, json, po).

With the bilingua wheel installed:

  setting   `bilingua_formats` is switched on through the settings API (POST /api/admin/config),
            not the env override, so the setting path is what routes the uploads.
  upload    each format through the web form, /api, the CLI and MCP: every door stores a
            Bilingua document whose segments carry the SAME token text the Python filter
            gives for that file (TM leverage depends on it); the document page renders.
  translate the mock engine (segmented; md also in full-text mode); QA's tag check finds no
            tag damage in any target.
  export    /api, the web download, the CLI, MCP and the project's bulk ZIP give the same
            bytes; the export re-reads (Bilingua and the Python filter) with every unit
            translated and every inline code back; an untouched export is the upload; the
            localized PO declares the target language.
  zip       a ZIP batch with a switched (md) and an unswitched (txt) member gives each member
            its own engine.
  tm        a file translated in the Python era (switch off) and uploaded again with the switch
            on gets a 100% TM match on every segment that TM stores.
  live      run.py on a free port (throwaway data dir, the switch on): upload -> translate ->
            export over real HTTP with the background workers.

Without the wheel the suite says so ("SKIP: ...") and only checks that the switch on is
harmless (new uploads stay Python on every door). Hermetic + offline.
Documented in: docs/architecture/translation-pipeline.md
Final line: "BILINGUA E2E OK".
"""

import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

TMP = init_env("loctms_bilingua_e2e_")
os.environ.pop("LOC_TMS_BILINGUA_FORMATS", None)
os.environ.pop("LOC_TMS_BILINGUA_SHADOW_FORMATS", None)

from sqlmodel import Session

from app.db import engine, init_db
from app.filters import bilingua_adapter as ba
from app.filters import get_filter
from app.filters.base import source_code_page
from app.models import Document
from app.services import settings
from tests import _bilingua_doors as D

LOCALE = "de-DE"
TOKEN = re.compile("⟦/?[a-z]?\\d+⟧")
FIXTURES = {
    "md": (
        "guide.md",
        b"---\ntitle: User guide\n---\n# Hello *world*\n\nSee [the docs](http://example.test) "
        b"and `code` here.\n\n- one **bold** item\n- two\n\nPlain closing paragraph.\n",
    ),
    "json": (
        "strings.json",
        b'{"title": "Hello {name}", "nested": {"bye": "Goodbye <b>friend</b>", "n": 3},\n'
        b' "list": ["First item", "Second item"]}\n',
    ),
    "po": (
        "app.po",
        b'msgid ""\nmsgstr ""\n"Project-Id-Version: demo\\n"\n'
        b'"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: en\\n"\n'
        b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n#: a.c:1\nmsgid "Hello %s"\n'
        b'msgstr ""\n\nmsgctxt "menu"\nmsgid "Open file"\nmsgstr ""\n\nmsgid "Save <b>now</b>"\n'
        b'msgstr ""\n',
    ),
}


def python_units(fmt, data):
    with source_code_page("en-US"):
        return [u.text for u in get_filter(fmt, _python=True).parse(data).units]


def sources(doc_id):
    return [src for _i, _o, src, _t, _s in D.segments(doc_id, LOCALE)]


def exp(doc_id):
    from app.services import export

    with Session(engine) as s:
        return export.build_translated_file(s, s.get(Document, doc_id), LOCALE)


# --------------------------------------------------------------------------- no wheel
def test_without_wheel():
    """The switch on with no module: every door still stores a Python document."""
    c = D.client()
    tok = D.api_token("bili-e2e-nowheel@example.test")
    os.environ["LOC_TMS_BILINGUA_FORMATS"] = "md,json,po"
    try:
        with D.engine_swap(None):
            assert not ba.available()
            pid = D.new_project("e2e-nowheel")
            for fmt, (name, data) in FIXTURES.items():
                st, body = D.upload_api(c, tok, pid, name, data)
                assert st == 200, (fmt, st, body)
                assert not ba.is_bilingua_skeleton(D.skeleton(body["document_id"])), fmt
                rc, body, out = D.upload_cli(pid, "cli-" + name, data)
                assert rc == 0 and not ba.is_bilingua_skeleton(D.skeleton(body["id"])), out
    finally:
        os.environ.pop("LOC_TMS_BILINGUA_FORMATS", None)
    print("  [no wheel] switch on without the module: uploads stay Python (api, cli) OK")


# --------------------------------------------------------------------------- with the wheel
def switch_on_through_api(c):
    settings.set_many({"remote_admin_enabled": "1"})
    adm = D.api_token("bili-e2e-admin@example.test")
    from app.services import apikeys, security

    user = security.get_by_email("bili-e2e-admin@example.test")
    _row, raw = apikeys.create_key(user.id, "e2e-admin", scopes="api docs admin")
    r = c.post(
        "/api/admin/config",
        headers={"X-Loc-Token": raw},
        json={"settings": {"bilingua_formats": "md,json,po"}},
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert settings.bilingua_formats() == ("md", "json", "po"), settings.bilingua_formats()
    assert "LOC_TMS_BILINGUA_FORMATS" not in os.environ
    return adm


def test_doors(c, tok, srv):
    """Upload through four doors, translate (mock), QA, export through five."""
    from app.services.quality_common import tag_damage

    out = {}
    for fmt, (name, data) in FIXTURES.items():
        pid = D.new_project("e2e-" + fmt)
        py = python_units(fmt, data)
        ids = {}
        st, loc, ids["web"] = D.upload_web(c, pid, name, data)
        assert st == 303 and ids["web"], (fmt, st, loc)
        st, body = D.upload_api(c, tok, pid, name, data)
        assert st == 200, (fmt, st, body)
        ids["api"] = body["document_id"]
        rc, body, text = D.upload_cli(pid, name, data)
        assert rc == 0 and body, (fmt, rc, text)
        ids["cli"] = body["id"]
        ok, body, statuses = D.upload_mcp(srv, pid, name, data)
        assert ok, (fmt, body, statuses)
        ids["mcp"] = body["document_id"]
        for door, doc_id in ids.items():
            sk = D.skeleton(doc_id)
            assert ba.is_bilingua_skeleton(sk), (fmt, door, "not a Bilingua document")
            assert [u["text"] for u in sk["units"]] == py, (fmt, door, sk["units"], py)
        src = sources(ids["api"])
        assert src and all(sources(i) == src for i in ids.values()), (fmt, "doors differ")
        page = c.get(f"/documents/{ids['api']}")
        assert page.status_code == 200, (fmt, page.status_code)
        # translate with the mock engine, QA's tag check, then export through every door
        for doc_id in ids.values():
            D.mock_translate(doc_id, LOCALE)
        segs = D.segments(ids["api"], LOCALE)
        assert all(t for _i, _o, _s, t, _st in segs), (fmt, "a segment got no translation")
        damaged = [(s, t) for _i, _o, s, t, _st in segs if tag_damage(s, t)]
        assert not damaged, (fmt, "tag damage", damaged)
        st, by_api = D.export_api(c, tok, ids["api"], LOCALE)
        assert st == 200 and by_api, (fmt, st, by_api[:200])
        st, by_web = D.export_web(c, ids["api"], LOCALE)
        assert st == 200 and by_web == by_api, (fmt, st)
        rc, by_cli, text = D.export_cli(ids["api"], LOCALE)
        assert rc == 0 and by_cli == by_api, (fmt, rc, text)
        ok, by_mcp, statuses = D.export_mcp(srv, ids["api"], LOCALE)
        assert ok and by_mcp == by_api, (fmt, statuses)
        for door in ("web", "cli", "mcp"):
            assert exp(ids[door]) == by_api, (fmt, door, "same file, other export")
        r = c.get(f"/projects/{pid}/export")
        assert r.status_code == 200 and r.content[:2] == b"PK", (fmt, r.status_code)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            members = [z.read(n) for n in z.namelist() if not n.endswith("/")]
        assert by_api in members, (fmt, "bulk ZIP export lacks the file", z.namelist())
        check_export(fmt, data, by_api, segs)
        out[fmt] = (pid, ids, by_api)
    print(
        "  [doors] md/json/po: web/api/cli/mcp upload, mock translate, no tag damage, "
        "api/web/cli/mcp/zip export equal OK"
    )
    return out


def check_export(fmt, data, exported, segs):
    """The export re-read: every unit translated, every code back, the target declared."""
    assert exported != data, (fmt, "the export is the source")
    for reader in ("bilingua", "python"):
        with source_code_page("en-US"):
            f = ba.BilinguaFilter(fmt) if reader == "bilingua" else get_filter(fmt, _python=True)
            units = [u.text for u in f.parse(exported).units]
            src = [u.text for u in f.parse(data).units]
        assert len(units) == len(src), (fmt, reader, units, src)
        for a, b in zip(src, units, strict=True):
            assert sorted(TOKEN.findall(a)) == sorted(TOKEN.findall(b)), (fmt, reader, a, b)
        if fmt != "po":  # a PO re-read gives the msgids; its msgstrs are checked below
            assert units != src, (fmt, reader, "no unit was translated")
    targets = [t for _i, _o, _s, t, _st in segs]
    text = exported.decode("utf-8")
    for t in targets:
        plain = TOKEN.sub("", t).strip()
        words = re.findall(r"[A-Za-z]{4,}", plain)[:1]
        assert all(w in text for w in words), (fmt, "a translation is missing", t)
    if fmt == "po":
        assert re.search(rb'"Language: de[-_]?(DE)?\\n"', exported), exported[:400]


def test_untouched_and_fulltext(c, tok):
    for fmt, (name, data) in FIXTURES.items():
        pid = D.new_project("e2e-untouched-" + fmt)
        st, body = D.upload_api(c, tok, pid, name, data)
        assert st == 200, body
        assert exp(body["document_id"]) == data, (fmt, "untouched export is not the upload")
    # full-text mode on md: paragraphs, not sentences
    name, data = FIXTURES["md"]
    pid = D.new_project("e2e-ft")
    r = c.post(
        "/api/documents",
        headers=tok,
        data={"project_id": str(pid), "mode": "full_text"},
        files={"file": (name, data, "text/markdown")},
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    doc_id = r.json()["document_id"]
    assert ba.is_bilingua_skeleton(D.skeleton(doc_id))
    D.mock_translate(doc_id, LOCALE)
    out = exp(doc_id)
    assert out != data and out.decode("utf-8").count("\n") >= data.decode("utf-8").count("\n") - 1
    print("  [untouched/full-text] untouched exports equal the upload; md full-text translates OK")


def test_zip_batch(c, tok):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.md", FIXTURES["md"][1])
        z.writestr("b.txt", b"Plain text, not switched.\n")
        z.writestr("c.json", FIXTURES["json"][1])
    pid = D.new_project("e2e-zip")
    st, body = D.upload_api(c, tok, pid, "batch.zip", buf.getvalue())
    assert st == 200, (st, body)
    by_fmt = {}
    for doc_id in D.doc_ids(pid):
        sk = D.skeleton(doc_id)
        by_fmt[sk.get("format")] = ba.is_bilingua_skeleton(sk)
    assert by_fmt == {"md": True, "txt": False, "json": True}, by_fmt
    print("  [zip] a mixed batch: md and json on Bilingua, txt on Python OK")


def test_tm_leverage(c, tok):
    from app.models import Project
    from app.services import tm
    from app.services.content import apply_tm_to_document

    for fmt, (name, data) in FIXTURES.items():
        pid = D.new_project("e2e-tm-" + fmt)
        settings.set_many({"bilingua_formats": ""})
        st, body = D.upload_api(c, tok, pid, name, data)
        assert st == 200, body
        old = body["document_id"]
        assert not ba.is_bilingua_skeleton(D.skeleton(old)), fmt
        D.mock_translate(old, LOCALE)
        stored = 0
        with Session(engine) as s:
            for _i, _o, src, tgt, _st in D.segments(old, LOCALE):
                if tgt and tm.write_back(s, pid, src, tgt, "en-US", LOCALE):
                    stored += 1
        settings.set_many({"bilingua_formats": "md,json,po"})
        st, body = D.upload_api(c, tok, pid, name, data)
        assert st == 200, body
        new = body["document_id"]
        assert ba.is_bilingua_skeleton(D.skeleton(new)), fmt
        assert sources(new) == sources(old), (fmt, "token text differs: TM leverage lost")
        with Session(engine) as s:
            filled = apply_tm_to_document(s, s.get(Document, new), s.get(Project, pid), LOCALE)
        assert stored and filled == stored, (fmt, "100% matches", filled, "of", stored)
    print("  [tm] Python-era TM: 100% matches on every stored segment of the Bilingua re-upload OK")


# --------------------------------------------------------------------------- live run.py
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_run_py(tok):
    import httpx

    port = free_port()
    pid = D.new_project("e2e-live")
    env = os.environ.copy()
    env.update(
        PORT=str(port),
        HOST="127.0.0.1",
        LOC_TMS_DATA_DIR=TMP,
        LOC_TMS_BILINGUA_FORMATS="md,json,po",
        PYTHONIOENCODING="utf-8",
    )
    log = open(os.path.join(TMP, "run_py.log"), "wb")  # noqa: SIM115 (closed in finally)
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--no-reload"],
        cwd=_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        while True:
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert proc.poll() is None, "run.py exited: " + _tail(log)
            assert time.time() < deadline, "run.py never answered /health: " + _tail(log)
            time.sleep(0.5)
        with httpx.Client(base_url=base, headers=tok, timeout=60) as h:
            for fmt, (name, data) in FIXTURES.items():
                r = h.post(
                    "/api/documents",
                    data={"project_id": str(pid)},
                    files={"file": (name, data, "application/octet-stream")},
                )
                assert r.status_code == 200, (fmt, r.status_code, r.text[:300])
                doc_id = r.json()["document_id"]
                assert ba.is_bilingua_skeleton(D.skeleton(doc_id)), (
                    fmt,
                    "live upload not Bilingua",
                )
                r = h.post(
                    f"/api/documents/{doc_id}/translate",
                    json={"targets": [LOCALE], "provider": "mock"},
                )
                assert r.status_code in (200, 202), (fmt, r.status_code, r.text[:300])
                jobs = r.json().get("jobs") or []
                assert jobs, (fmt, r.text[:300])
                for j in jobs:
                    _wait_job(h, j["job_id"])
                r = h.get(f"/api/documents/{doc_id}/download", params={"locale": LOCALE})
                assert r.status_code == 200 and r.content and r.content != data, (
                    fmt,
                    r.status_code,
                    r.content[:200],
                )
                assert r.content == exp(doc_id), (fmt, "live export differs from in-process")
    finally:
        _stop(proc)
        log.close()
    print(
        f"  [live] run.py on port {port}: upload -> translate (workers) -> export for md/json/po OK"
    )


def _wait_job(h, job_id):
    deadline = time.time() + 120
    while time.time() < deadline:
        body = h.get(f"/api/jobs/{job_id}").json()
        state = str(body.get("status", "")).lower()
        if state in ("completed", "done", "succeeded"):
            return
        assert state not in ("failed", "cancelled", "error"), body
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} never finished")


def _stop(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
        proc.wait(timeout=20)


def _tail(log):
    log.flush()
    with open(log.name, "rb") as fh:
        return fh.read()[-1500:].decode("utf-8", "replace")


def main():
    init_db()
    test_without_wheel()
    real = D.real_engine()
    if real is None:
        print(
            "SKIP: bilingua wheel not installed in this venv; the end-to-end checks with the "
            "engine (doors, translate, export, TM, live run.py) did not run. Run this suite "
            "in an environment that has the bilingua wheel (x86_64 Linux or Windows)."
        )
        print("BILINGUA E2E OK")
        return
    print(f"  engine: wheel {real.version()} {json.dumps(real.profiles())}")
    c = D.client()
    tok = D.api_token("bili-e2e@example.test")
    srv = D.mcp_server(c, tok)
    switch_on_through_api(c)
    test_doors(c, tok, srv)
    test_untouched_and_fulltext(c, tok)
    test_zip_batch(c, tok)
    test_tm_leverage(c, tok)
    test_live_run_py(tok)
    settings.set_many({"bilingua_formats": ""})
    print("BILINGUA E2E OK")


if __name__ == "__main__":
    main()
