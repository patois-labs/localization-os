"""S6 (ADR 0094): streamed background TM import.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Covers:
  parity      -- tm.import_tmx_file (the file path, spooled plan) gives EXACTLY the reply and
                 the stored rows import_tmx gives for the same bytes: in-file duplicates (first
                 source and meta kept, last target wins), rows already in the TM (updated /
                 skipped), refusals, integrity withholding, the placeholder acknowledgement,
                 across tiny resolve batches and tiny write chunks.
  re-import   -- importing the same file again is a no-op (all skipped, no duplicate hash).
  scan        -- an entity declaration is refused even when it straddles two scan blocks and
                 in a UTF-16 file; the <tu> count straddling a block boundary is not lost.
  caps        -- TMX_IMPORT_MAX_BYTES and TMX_IMPORT_MAX_UNITS refuse before anything is
                 stored; a non-TMX file over MAX_UPLOAD_BYTES is refused on the file path.
  memory      -- parsing a 60k-unit file through the file path keeps the Python heap far below
                 what the in-memory plan needed (tracemalloc peak, not proportional to units).
  web         -- a form upload over TMX_IMPORT_INLINE_MAX_BYTES redirects at once with
                 tm_import_job=<id>; the job completes with the import counts; the project page
                 lists the job (and the progress script); the upload file is removed. A small
                 file still imports inline. A wrong locale pair is refused before staging.
  api         -- a multipart POST answers 202 {job_id, status_url}; GET /api/jobs/{id} carries the
                 counts; a JSON base64 import still answers 200 inline; an ack value other than
                 true/false is a 400; a foreign project 404s; a body over the TMX cap is a 413
                 and a JSON body is still held to REQUEST_MAX_BODY_BYTES.
  token       -- the job handler only ever opens upload-<32 hex>.tmx in the import folder.
  failures    -- a parse failure stores nothing and removes the plan file; a write failure in
                 chunk 3 raises TmxImportIncomplete (20 of 50 kept) and the same file re-imports
                 to completion with no duplicate hash; sweep_stale removes only old leftovers.
  cli         -- `tm import` (in-process) imports from the path and reports the counts.

Final line: "TM IMPORT BACKGROUND OK".
"""

import base64
import os
import sys
import time
import tracemalloc

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_tmimportbg_")

from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Job, JobStatus, Project, Role, SegmentTM, TargetLocale, User
from app.services import apikeys, jobs, settings, tm, tm_import_job, tm_import_stream
from tests._cli_runner import run_cli

init_db()
SRC, TGT = "en-US", "de-DE"
client = TestClient(app, raise_server_exceptions=False)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _project(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale=SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=TGT))
        s.commit()
        return p.id


def _tu(src, tgt, props=""):
    return (
        f'<tu>{props}<tuv xml:lang="{SRC}"><seg>{src}</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>{tgt}</seg></tuv></tu>'
    )


def _doc(units):
    return (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US"/>'
        "<body>" + "".join(units) + "</body></tmx>"
    ).encode()


def _plain(n, target="Einheit", start=0):
    return _doc(_tu(f"Unit {i} text", f"{target} {i} Text") for i in range(start, start + n))


def _mixed():
    """Every kind of unit the import treats differently."""
    u = []
    for i in range(40):
        u.append(_tu(f"Row {i} source", f"Zeile {i} alt"))
    # in-file duplicates of new rows: same target (skipped), changed target (updated); the
    # first occurrence's meta stays
    u.append(_tu("Row 3 source", "Zeile 3 alt"))
    u.append(_tu("Row 4 source", "Zeile 4 neu"))
    u.append(_tu("Row 4 source", "Zeile 4 neuer"))
    u.append(_tu("Meta row", "Meta Zeile", '<prop type="x-origin">import-a</prop>'))
    u.append(_tu("Meta row", "Meta Zeile 2", '<prop type="x-origin">import-b</prop>'))
    # placeholder loss (acknowledgeable), refusal hedge
    u.append(_tu("Pay {0} now", "Jetzt zahlen"))
    u.append(_tu("Hello %s", "Hallo"))
    u.append(_tu("A refusal", "I'm sorry, but I can't help with that request."))
    # existing rows (seeded below): unchanged, changed, changed twice in the file
    u.append(_tu("Old 1", "Alt 1"))
    u.append(_tu("Old 2", "Alt 2 neu"))
    u.append(_tu("Old 3", "Alt 3 a"))
    u.append(_tu("Old 3", "Alt 3 b"))
    u.append(_tu("Old 3", "Alt 3 b"))
    return _doc(u)


def _seed(pid):
    with Session(engine) as s:
        tm.import_tmx(
            s,
            pid,
            _doc([_tu("Old 1", "Alt 1"), _tu("Old 2", "Alt 2"), _tu("Old 3", "Alt 3")]),
            SRC,
            TGT,
        )


def _rows(pid):
    with Session(engine) as s:
        return sorted(
            (r.source_hash, r.source_text, r.target_text, r.meta or None)
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid))
        )


def _count(pid):
    with Session(engine) as s:
        return s.exec(
            select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == pid)
        ).one()


def _write(name, data):
    path = os.path.join(_TMP, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _strip(res):
    return {k: v for k, v in res.items() if k not in ("seconds", "filename", "job_id")}


def test_parity():
    old = (tm.TMX_IMPORT_FLUSH_BATCH, tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S)
    try:
        for flush, chunk in ((3, 4), (500, 2500)):
            tm.TMX_IMPORT_FLUSH_BATCH, tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = (
                flush,
                chunk,
                0,
            )
            for ack in (False, True):
                a, b = _project(f"bg-par-a-{flush}-{ack}"), _project(f"bg-par-b-{flush}-{ack}")
                _seed(a)
                _seed(b)
                data = _mixed()
                with Session(engine) as s:
                    ra = tm.import_tmx(s, a, data, SRC, TGT, acknowledge_placeholder_loss=ack)
                path = _write("mixed.tmx", data)
                with Session(engine) as s:
                    rb = tm.import_tmx_file(s, b, path, SRC, TGT, acknowledge_placeholder_loss=ack)
                if ra != rb:
                    _fail(f"file path reply differs from bytes reply ({flush}/{ack}): {ra} != {rb}")
                if _rows(a) != _rows(b):
                    _fail(f"file path stored rows differ from bytes import ({flush}/{ack})")
                want = {"imported": 41, "updated": 6, "skipped": 3}
                if ack:
                    want = {"imported": 43, "updated": 6, "skipped": 3}
                got = {k: rb[k] for k in want}
                if got != want or rb["skipped_refusal"] != 1:
                    _fail(
                        f"counts ({flush}/{ack}): {got} != {want}, refusal {rb['skipped_refusal']}"
                    )
                meta = [r for r in _rows(b) if r[1] == "Meta row"]
                if len(meta) != 1 or meta[0][2] != "Meta Zeile 2":
                    _fail(f"in-file duplicate: last target wins, one row: {meta}")
                if meta[0][3] != {"origin": "import-a"} and meta[0][3] is not None:
                    _fail(f"in-file duplicate keeps the FIRST occurrence's meta: {meta[0][3]}")
                tgt3 = [r[2] for r in _rows(b) if r[1] == "Old 3"]
                if tgt3 != ["Alt 3 b"]:
                    _fail(f"existing row changed twice ends with the last target: {tgt3}")
                # re-import of the same file: no new row, the stored rows unchanged
                before = _rows(b)
                with Session(engine) as s:
                    rr = tm.import_tmx_file(s, b, path, SRC, TGT, acknowledge_placeholder_loss=ack)
                if rr["imported"] != 0 or _rows(b) != before:
                    _fail(f"re-import must store nothing new: {rr}")
    finally:
        tm.TMX_IMPORT_FLUSH_BATCH, tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = old
    leftovers = [p for p in os.listdir(tm_import_stream.spool_dir()) if p.startswith("spool-")]
    if leftovers:
        _fail(f"plan files must be removed after every import: {leftovers}")
    print("  parity: file path == bytes import (replies, rows, meta, re-import) OK")


def test_scan():
    block = tm_import_stream._SCAN_BLOCK
    head = b'<?xml version="1.0"?>' + b" " * (block - 25)
    path = _write("ent.tmx", head + b"<!ENTITY x 'y'><tmx/>")
    if not tm_import_stream.scan_tmx_file(path)[0]:
        _fail("an entity straddling two scan blocks must be found")
    u16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE t [<!ENTITY a "b">]><tmx/>'
    path = _write("ent16.tmx", u16.encode("utf-16"))
    if not tm_import_stream.scan_tmx_file(path)[0]:
        _fail("a UTF-16 entity declaration must be found")
    pid = _project("bg-scan")
    with Session(engine) as s:
        try:
            tm.import_tmx_file(s, pid, path, SRC, TGT)
            _fail("an entity file must be refused")
        except ValueError as exc:
            if "entities" not in str(exc):
                _fail(f"wrong refusal: {exc}")
    pad = block - 2
    data = b" " * pad + b"<tu>" + b"<tu " + b"x" * 10 + b"<tu>"
    path = _write("count.tmx", data)
    if tm_import_stream.scan_tmx_file(path)[1] != 3:
        _fail(f"tu count across a block boundary: {tm_import_stream.scan_tmx_file(path)[1]}")
    print("  scan: entity across blocks + UTF-16, tu count across blocks OK")


def test_caps():
    pid = _project("bg-caps")
    path = _write("caps.tmx", _plain(30))
    old = (config.TMX_IMPORT_MAX_BYTES, config.TMX_IMPORT_MAX_UNITS)
    try:
        config.TMX_IMPORT_MAX_BYTES = 100
        with Session(engine) as s:
            try:
                tm.import_tmx_file(s, pid, path, SRC, TGT)
                _fail("over TMX_IMPORT_MAX_BYTES must be refused")
            except ValueError as exc:
                if "TMX_IMPORT_MAX_BYTES" not in str(exc):
                    _fail(f"size refusal must name the setting: {exc}")
        config.TMX_IMPORT_MAX_BYTES = old[0]
        config.TMX_IMPORT_MAX_UNITS = 10
        with Session(engine) as s:
            try:
                tm.import_tmx_file(s, pid, path, SRC, TGT)
                _fail("over TMX_IMPORT_MAX_UNITS must be refused")
            except ValueError as exc:
                if "TMX_IMPORT_MAX_UNITS" not in str(exc):
                    _fail(f"unit refusal must name the setting: {exc}")
    finally:
        config.TMX_IMPORT_MAX_BYTES, config.TMX_IMPORT_MAX_UNITS = old
    if _count(pid):
        _fail("a refused file must store nothing")
    old_up = config.MAX_UPLOAD_BYTES
    try:
        config.MAX_UPLOAD_BYTES = 20
        csv = _write("tm.csv", b"source,target\nHello there,Hallo da\n")
        with Session(engine) as s:
            try:
                tm.import_tmx_file(s, pid, csv, SRC, TGT)
                _fail("a non-TMX file over MAX_UPLOAD_BYTES must be refused")
            except ValueError as exc:
                if "only a TMX" not in str(exc):
                    _fail(f"wrong refusal: {exc}")
    finally:
        config.MAX_UPLOAD_BYTES = old_up
    print("  caps: bytes, units, non-TMX size refused before any write OK")


def test_memory():
    pid = _project("bg-mem")
    n = 60_000
    path = _write("mem.tmx", _plain(n))
    size = os.path.getsize(path)
    tracemalloc.start()
    try:
        with Session(engine) as s:
            res = tm.import_tmx_file(s, pid, path, SRC, TGT)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if res["imported"] != n:
        _fail(f"memory import stored {res['imported']} of {n}")
    # The in-memory plan cost ~500 bytes per unit plus the file itself (~30 MB here). The
    # spooled plan is bounded by one resolve batch and one write chunk.
    if peak > 12 * 1024 * 1024:
        _fail(f"file path import peak {peak} bytes for a {size}-byte / {n}-unit file")
    print(f"  memory: {n} units, {size // 1024} KB file, peak heap {peak // 1024} KB OK")


def _wait_job(jid, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        job = jobs.get(jid)
        if job and job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            return job
        time.sleep(0.1)
    _fail(f"job {jid} did not finish")


def _uploads():
    return [p for p in os.listdir(tm_import_stream.spool_dir()) if p.startswith("upload-")]


def test_web():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    old = config.TMX_IMPORT_INLINE_MAX_BYTES
    try:
        pid = _project("bg-web")
        small = _plain(5)
        config.TMX_IMPORT_INLINE_MAX_BYTES = len(small) + 10
        form = {"source_locale": SRC, "target_locale": TGT}
        r = client.post(
            f"/projects/{pid}/tm/import",
            data=form,
            files={"file": ("s.tmx", small, "application/xml")},
            follow_redirects=False,
        )
        if r.status_code != 303 or "tm_imported=5" not in r.headers.get("location", ""):
            _fail(f"a small file must still import inline: {r.status_code} {r.headers}")
        big = _plain(400, start=100)
        r = client.post(
            f"/projects/{pid}/tm/import",
            data=form,
            files={"file": ("big.tmx", big, "application/xml")},
            follow_redirects=False,
        )
        loc = r.headers.get("location", "")
        if r.status_code != 303 or "tm_import_job=" not in loc:
            _fail(f"a big file must redirect with the job id: {r.status_code} {loc}")
        jid = int(loc.split("tm_import_job=")[1].split("&")[0])
        job = _wait_job(jid)
        if job.status != JobStatus.COMPLETED or (job.result or {}).get("imported") != 400:
            _fail(f"background job: {job.status} {job.error} {job.result}")
        if _uploads():
            _fail(f"the upload must be removed when the job ends: {_uploads()}")
        page = client.get(loc).text
        if f'id="tm-import-job-{jid}"' not in page or "done: imported 400 units" not in page:
            _fail("the project page must list the finished import with its counts")
        if "tm_import_progress.js" not in page:
            _fail("the project page must load the progress script")
        r = client.post(
            f"/projects/{pid}/tm/import",
            data={"source_locale": SRC, "target_locale": "ja-JP"},
            files={"file": ("big.tmx", big, "application/xml")},
            follow_redirects=False,
        )
        if "err=tmx_locales" not in r.headers.get("location", "") or _uploads():
            _fail(f"a wrong pair must be refused before staging: {r.headers}")
    finally:
        config.TMX_IMPORT_INLINE_MAX_BYTES = old
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    print("  web: inline small, background big (redirect, job, page, cleanup), pair refused OK")


_KEY = []


def _key():
    if not _KEY:
        with Session(engine) as s:
            u = User(email="bg-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
        _row, raw = apikeys.create_key(uid, "bgimp", scopes="api docs")
        _KEY.append(raw)
    return _KEY[0]


def test_api():
    h = {"X-Loc-Token": _key()}
    pid = _project("bg-api")
    data = _plain(300)
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        data={"source": SRC, "target": TGT},
        files={"file": ("a.tmx", data, "application/xml")},
        headers=h,
    )
    j = r.json()
    if (
        r.status_code != 202
        or not j.get("job_id")
        or j.get("status_url") != f"/api/jobs/{j.get('job_id')}"
    ):
        _fail(f"multipart import must answer 202 with the job: {r.status_code} {j}")
    _wait_job(j["job_id"])
    st = client.get(j["status_url"], headers=h).json()
    if st.get("status") != "completed" or st.get("result", {}).get("imported") != 300:
        _fail(f"job status must carry the counts: {st}")
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        json={
            "tmx_b64": base64.b64encode(_plain(3, start=900)).decode(),
            "source": SRC,
            "target": TGT,
        },
        headers=h,
    )
    if r.status_code != 200 or r.json().get("imported") != 3:
        _fail(f"the JSON import must still answer inline: {r.status_code} {r.text[:200]}")
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        data={"source": SRC, "target": TGT, "acknowledge_placeholder_loss": "yes"},
        files={"file": ("a.tmx", data, "application/xml")},
        headers=h,
    )
    if r.status_code != 400:
        _fail(f"an ack other than true/false must be a 400: {r.status_code}")
    r = client.post(
        "/api/projects/987654/tm/import",
        data={"source": SRC, "target": TGT},
        files={"file": ("a.tmx", data, "application/xml")},
        headers=h,
    )
    if r.status_code != 404:
        _fail(f"a missing project must 404: {r.status_code}")
    old = (
        config.TMX_IMPORT_MAX_BYTES,
        config.REQUEST_MAX_BODY_BYTES,
        config.REQUEST_MAX_ANON_BODY_BYTES,
    )
    try:
        config.TMX_IMPORT_MAX_BYTES = 1000
        r = client.post(
            f"/api/projects/{pid}/tm/import",
            data={"source": SRC, "target": TGT},
            files={"file": ("a.tmx", data, "application/xml")},
            headers=h,
        )
        if r.status_code != 413 or _uploads():
            _fail(
                f"over the TMX cap must be a 413 with nothing staged: {r.status_code} {_uploads()}"
            )
        # a multipart TMX may exceed REQUEST_MAX_BODY_BYTES (up to the TMX cap); JSON may not
        config.TMX_IMPORT_MAX_BYTES = 10 * 1024 * 1024
        config.REQUEST_MAX_BODY_BYTES = 4096
        config.REQUEST_MAX_ANON_BODY_BYTES = 1024
        r = client.post(
            f"/api/projects/{pid}/tm/import",
            data={"source": SRC, "target": TGT},
            files={"file": ("a.tmx", data, "application/xml")},
            headers=h,
        )
        if r.status_code != 202:
            _fail(f"a multipart TMX over REQUEST_MAX_BODY_BYTES must be accepted: {r.status_code}")
        _wait_job(r.json()["job_id"])
        r = client.post(
            f"/api/projects/{pid}/tm/import",
            json={"tmx_b64": base64.b64encode(data).decode(), "source": SRC, "target": TGT},
            headers=h,
        )
        if r.status_code != 413:
            _fail(f"a JSON body keeps REQUEST_MAX_BODY_BYTES: {r.status_code}")
    finally:
        (
            config.TMX_IMPORT_MAX_BYTES,
            config.REQUEST_MAX_BODY_BYTES,
            config.REQUEST_MAX_ANON_BODY_BYTES,
        ) = old
    print("  api: 202 + job, JSON inline, ack 400, 404, 413, body caps by content type OK")


def test_token():
    for bad in ("../../loc_tms.db", "abc", "A" * 32, "0" * 31 + "/", None):
        try:
            tm_import_job.upload_path(bad)
            _fail(f"token {bad!r} must be refused")
        except ValueError:
            pass
    pid = _project("bg-token")
    jid = jobs.submit(
        "tm_import", {"project_id": pid, "token": "../x", "source": SRC, "target": TGT}
    )
    job = _wait_job(jid)
    if job.status != JobStatus.FAILED:
        _fail(f"a job with a bad token must fail: {job.status}")
    with Session(engine) as s:
        n = s.exec(select(func.count()).select_from(Job).where(Job.kind == "tm_import")).one()
    if n < 1:
        _fail("tm_import jobs must be recorded")
    print("  token: only upload-<32 hex>.tmx in the import folder is ever opened OK")


def _spools():
    return [p for p in os.listdir(tm_import_stream.spool_dir()) if p.startswith("spool-")]


def test_failure_paths():
    """Parse failure after resolve batches: nothing stored, plan file removed. Write failure in
    chunk 3: TmxImportIncomplete, plan file removed, the same file re-imports to completion with
    no duplicate hash. sweep_stale removes only old leftovers."""
    pid = _project("bg-fail")
    good = _plain(50).decode()
    broken = good.replace("</body></tmx>", "<tu><tuv>")  # truncated document
    path = _write("broken.tmx", broken.encode())
    old = (tm.TMX_IMPORT_FLUSH_BATCH, tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S)
    try:
        tm.TMX_IMPORT_FLUSH_BATCH, tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = 7, 10, 0
        with Session(engine) as s:
            try:
                tm.import_tmx_file(s, pid, path, SRC, TGT)
                _fail("a truncated TMX must be refused")
            except Exception as exc:
                if isinstance(exc, SystemExit):
                    raise
        if _count(pid) or _spools():
            _fail(f"a parse failure must store nothing and leave no plan file: {_spools()}")
        path = _write("fifty.tmx", _plain(50))
        real = tm.import_lock.begin_write_phase
        calls = []

        def flaky(session, model):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("disk full (simulated)")
            return real(session, model)

        tm.import_lock.begin_write_phase = flaky
        try:
            with Session(engine) as s:
                try:
                    tm.import_tmx_file(s, pid, path, SRC, TGT)
                    _fail("a failure in chunk 3 must raise TmxImportIncomplete")
                except tm.TmxImportIncomplete as exc:
                    if exc.rows_written != 20 or exc.rows_total != 50:
                        _fail(f"rows written / total: {exc.rows_written} / {exc.rows_total}")
        finally:
            tm.import_lock.begin_write_phase = real
        if _count(pid) != 20 or _spools():
            _fail(f"two chunks must be kept and the plan file removed: {_count(pid)} {_spools()}")
        with Session(engine) as s:
            res = tm.import_tmx_file(s, pid, path, SRC, TGT)
        with Session(engine) as s:
            distinct = s.exec(
                select(func.count(func.distinct(SegmentTM.source_hash))).where(
                    SegmentTM.project_id == pid
                )
            ).one()
        if res["imported"] != 30 or res["skipped"] != 20 or _count(pid) != 50 or distinct != 50:
            _fail(f"re-import must finish without duplicates: {res} {_count(pid)} {distinct}")
    finally:
        tm.TMX_IMPORT_FLUSH_BATCH, tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = old
    d = tm_import_stream.spool_dir()
    stale, fresh = d / ("upload-" + "a" * 32 + ".tmx"), d / ("upload-" + "b" * 32 + ".tmx")
    for f in (stale, fresh):
        f.write_bytes(b"x")
    two_days = time.time() - 2 * 86400
    os.utime(stale, (two_days, two_days))
    n = tm_import_job.sweep_stale()
    if stale.exists() or not fresh.exists() or n != 1:
        _fail(f"sweep_stale must remove only old leftovers: removed {n}")
    fresh.unlink()
    print("  failure paths: parse failure clean, chunk-3 failure kept 20/50 and resumed, sweep OK")


def test_cli():
    pid = _project("bg-cli")
    path = _write("cli.tmx", _plain(25))
    code, out, err = run_cli(
        ["--json", "tm", "import", str(pid), path, "--source", SRC, "--target", TGT]
    )
    if code != 0 or '"imported": 25' not in out:
        _fail(f"CLI import from the path: {code} {out[:300]} {err[:300]}")
    print("  cli: tm import from the file path OK")


def main():
    test_parity()
    test_scan()
    test_caps()
    test_memory()
    test_web()
    test_api()
    test_token()
    test_failure_paths()
    test_cli()
    print("TM IMPORT BACKGROUND OK")


if __name__ == "__main__":
    main()
