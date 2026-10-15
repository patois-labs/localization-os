"""Issue #615 (second half): big TMX imports commit in bounded chunks, the JSON TM export streams,
and a glossary CSV with an unknown header is refused on every surface.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Covers:
  chunk-bound   -- every write transaction of an import holds at most TMX_WRITE_CHUNK_ROWS rows
                   (INSERT executemany sizes and the commit count are asserted), whatever the
                   mix of new rows, target updates and unchanged touches; an import that fits one
                   chunk is exactly one transaction.
  lock-free     -- at every pause between two chunks another connection CAN take the SQLite write
                   lock at once (deterministic: no timing), so a waiting writer gets in.
  halfway       -- a failure in chunk 3 of 5 raises TmxImportIncomplete naming the rows stored and
                   the retry, keeps the earlier chunks, leaves the session usable; re-importing
                   the SAME file stores the rest with no duplicate source hash. A failure in the
                   FIRST chunk raises the original error and stores nothing (still atomic).
  race          -- a row another writer stores between two chunks is updated to the file's target,
                   not duplicated (#584 semantics), and the counts add up to the file's units.
  edit/delete   -- an existing row edited or deleted by another writer after the import read the
                   TM is re-read under the chunk's lock: an edit gets the file's target exactly as
                   #584's re-run gave it, a delete is not restored, and the counts say exactly what
                   the import wrote (`concurrent_deleted` for the delete).
  json-export   -- GET .../tm/export (the JSON default the remote CLI and MCP read) is streamed:
                   same fields and base64 payload, `bytes` correct, no Content-Length, and the
                   generator's peak memory stays far below the body size.
  csv-header    -- a glossary CSV with an unknown header (`source,target`) is refused with a message
                   naming the expected columns and the ones found, writing nothing, on the web
                   (project and module glossary pages), the /api routes, the in-process CLI and
                   the MCP tool.

Final line: "IMPORT CHUNKS 615 OK".
"""

import asyncio
import base64
import os
import pathlib
import sqlite3
import sys
import tracemalloc
import types
import urllib.parse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_importchunks615_")

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, func, select

from app.db import engine, init_db
from app.main import app
from app.models import GlossaryEntry, Project, Role, SegmentTM, TargetLocale, User
from app.services import apikeys, settings, tm
from tests._cli_runner import run_cli

init_db()
SRC, TGT = "en-US", "de-DE"


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


def _tmx_of(indices, target="Einheit", revised=()):
    units = "".join(
        f'<tu><tuv xml:lang="{SRC}"><seg>Unit {i} text</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>{"Neu" if i in revised else target} {i} Text</seg></tuv></tu>'
        for i in indices
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="{SRC}"/>'
        f"<body>{units}</body></tmx>"
    ).encode()


def _tmx(n, target="Einheit"):
    return _tmx_of(range(n), target)


def _count(pid):
    with Session(engine) as s:
        return s.exec(
            select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == pid)
        ).one()


def _distinct_hashes(pid):
    with Session(engine) as s:
        return s.exec(
            select(func.count(func.distinct(SegmentTM.source_hash))).where(
                SegmentTM.project_id == pid
            )
        ).one()


def _write_now() -> bool:
    """Can ANOTHER connection take the write lock right now? (0.2 s busy timeout.)"""
    con = sqlite3.connect(engine.url.database, timeout=0.2, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=200")
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS lock_probe_615 (t REAL)")
        con.execute("INSERT INTO lock_probe_615 VALUES (0)")
        con.execute("COMMIT")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


class _Knobs:
    """Set the chunk size and pause for one test, restore after."""

    def __init__(self, rows, pause=0.0, sleeper=None):
        self.rows, self.pause, self.sleeper = rows, pause, sleeper

    def __enter__(self):
        self.old = (tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S, tm.time)
        tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = self.rows, self.pause
        if self.sleeper is not None:
            tm.time = types.SimpleNamespace(sleep=self.sleeper)
        return self

    def __exit__(self, *exc):
        tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S, tm.time = self.old
        return False


def test_chunk_bound():
    pid = _project("ic615-bound")
    sizes, commits = [], []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SEGMENTTM") and executemany:
            sizes.append(len(parameters))

    def on_commit(conn):
        commits.append(1)

    event.listen(engine, "before_cursor_execute", spy)
    event.listen(engine, "commit", on_commit)
    try:
        with _Knobs(100), Session(engine) as s:
            res = tm.import_tmx(s, pid, _tmx(1050), SRC, TGT)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
        event.remove(engine, "commit", on_commit)
    if res["imported"] != 1050 or res["write_chunks"] != 11:
        _fail(f"1050 new rows at 100 per chunk must be 11 chunks: {res}")
    if not sizes or max(sizes) > 100:
        _fail(f"an INSERT carried more rows than the chunk bound: {sorted(set(sizes))}")
    if len(commits) < 11:
        _fail(f"each chunk must be its own commit, saw {len(commits)} commits")
    if _count(pid) != 1050 or _distinct_hashes(pid) != 1050:
        _fail("every unit must be stored exactly once")
    # a re-import mixing 250 changed targets with 800 unchanged rows packs into 11 chunks too
    with _Knobs(100):
        # first 250 units revised, the other 800 identical
        doc = _tmx_of(range(1050), revised=set(range(250)))
        with Session(engine) as s:
            res2 = tm.import_tmx(s, pid, doc, SRC, TGT)
    if res2["updated"] != 250 or res2["skipped"] != 800 or res2["write_chunks"] != 11:
        _fail(f"mixed re-import must update 250, skip 800 in 11 chunks: {res2}")
    # an import that fits one chunk is one transaction
    pid2 = _project("ic615-one")
    with Session(engine) as s:
        one = tm.import_tmx(s, pid2, _tmx(100), SRC, TGT)
    if one["write_chunks"] != 1:
        _fail(f"an import under the chunk bound must be a single transaction: {one}")
    print("  chunk-bound: 1050 rows -> 11 chunks of <= 100; mixed re-import packs; small = 1 OK")


def test_lock_free_between_chunks():
    pid = _project("ic615-free")
    results = []

    def sleeper(_s):
        results.append(_write_now())

    with _Knobs(100, pause=0.001, sleeper=sleeper), Session(engine) as s:
        res = tm.import_tmx(s, pid, _tmx(450), SRC, TGT)
    if res["write_chunks"] != 5 or len(results) != 4:
        _fail(f"5 chunks must pause 4 times: {res['write_chunks']} chunks, {len(results)} pauses")
    if not all(results):
        _fail("the write lock was still held at a pause between chunks")
    print("  lock-free: another connection took the write lock at all 4 pauses between chunks OK")


class _FailNthInsert:
    def __init__(self, nth):
        self.nth, self.seen = nth, 0

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SEGMENTTM"):
            self.seen += 1
            if self.seen == self.nth:
                raise RuntimeError("injected chunk failure")

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


def test_failure_halfway_then_retry():
    pid = _project("ic615-half")
    doc = _tmx(450)
    with _Knobs(100):
        with Session(engine) as s:
            with _FailNthInsert(3):
                try:
                    tm.import_tmx(s, pid, doc, SRC, TGT)
                except tm.TmxImportIncomplete as exc:
                    msg = str(exc)
                    if (exc.rows_written, exc.rows_total) != (200, 450):
                        _fail(f"the exception must carry the counts: {exc.rows_written}")
                    if "200 of 450" not in msg or "again" not in msg:
                        _fail(f"the message must name the state and the retry: {msg}")
                else:
                    _fail("a failure in chunk 3 must raise TmxImportIncomplete")
            if _count(pid) != 200:
                _fail(f"the two committed chunks must stay, found {_count(pid)}")
            # the same session is usable and the SAME file finishes the job
            res = tm.import_tmx(s, pid, doc, SRC, TGT)
        if res["imported"] != 250 or res["skipped"] != 200:
            _fail(f"the retry must store the 250 missing rows and skip the 200 present: {res}")
        if _count(pid) != 450 or _distinct_hashes(pid) != 450:
            _fail("the retry must leave 450 rows with no duplicate source hash")
        # a failure in the FIRST chunk is still all-or-nothing, with the original error
        pid2 = _project("ic615-first")
        with Session(engine) as s, _FailNthInsert(1):
            try:
                tm.import_tmx(s, pid2, doc, SRC, TGT)
            except tm.TmxImportIncomplete:
                _fail("nothing was committed, so this must be the original error")
            except RuntimeError:
                pass
            else:
                _fail("the injected failure must propagate")
        if _count(pid2) != 0:
            _fail("a first-chunk failure must store nothing")
    print(
        "  halfway: chunk-3 failure keeps 200 rows and names the retry; first-chunk failure = 0 OK"
    )


def test_writer_between_chunks_is_not_duplicated():
    pid = _project("ic615-race")
    fired = []

    def sleeper(_s):
        if fired:
            return
        fired.append(1)
        # another writer stores 50 of the file's units (with its own target) mid-import
        with Session(engine) as other:
            tm.import_tmx(other, pid, _tmx_of(range(0, 450, 9), "Fremd"), SRC, TGT)

    with _Knobs(100, pause=0.001, sleeper=sleeper), Session(engine) as s:
        res = tm.import_tmx(s, pid, _tmx(450), SRC, TGT)
    if not fired:
        _fail("the race hook never ran")
    if _count(pid) != 450 or _distinct_hashes(pid) != 450:
        _fail("a row stored between chunks must not be duplicated")
    if res["imported"] + res["updated"] + res["skipped"] != 450 or res["imported"] >= 450:
        _fail(f"the counts must still add up to the file's units, with the late rows seen: {res}")
    if res["updated"] < 1 or res["skipped"] or res["concurrent_deleted"]:
        _fail(f"the late rows must be counted as updated to the file's target: {res}")
    # Review gate: exactly the late rows got the file's target (#584 semantics); the other
    # writer's own later change to rows this import had already stored stays.
    with Session(engine) as s:
        fremd = s.exec(
            select(func.count())
            .select_from(SegmentTM)
            .where(SegmentTM.project_id == pid, SegmentTM.target_text.startswith("Fremd"))
        ).one()
    if fremd != 50 - res["updated"]:
        _fail(f"{res['updated']} late rows updated must leave {50 - res['updated']} Fremd: {fremd}")
    print(
        f"  race: late rows updated, not duplicated ({res['imported']} new, "
        f"{res['updated']} updated) OK"
    )


def _target_of(pid, i):
    h = tm.hash_text(f"Unit {i} text", SRC, TGT)
    with Session(engine) as s:
        return s.exec(
            select(SegmentTM.target_text).where(
                SegmentTM.project_id == pid, SegmentTM.source_hash == h
            )
        ).first()


def test_edit_or_delete_during_import_is_reread_and_counted():
    """Review gate: an EXISTING row edited or deleted between phase 1 and that row's chunk is
    re-read under the chunk's lock, never written from stale state. #584's single transaction
    re-ran the import on fresh state (the file's target won over the edit, the counts were the
    re-run's); a chunk must do the same, and must not bring back or count a deleted row."""
    pid = _project("ic615-edit")
    with Session(engine) as s:
        tm.import_tmx(s, pid, _tmx(450), SRC, TGT)
    h = {i: tm.hash_text(f"Unit {i} text", SRC, TGT) for i in (300, 301, 302, 420)}
    fired = []

    def sleeper(_s):
        if fired:
            return
        fired.append(1)
        # chunk 1 (units 0..99) is committed; units 300.. are still to come
        with Session(engine) as other:

            def row(i):
                return other.exec(
                    select(SegmentTM).where(
                        SegmentTM.project_id == pid, SegmentTM.source_hash == h[i]
                    )
                ).one()

            row(300).target_text = "Linguist 300"  # edited: the file changes it anyway
            other.delete(row(301))  # deleted: the file would change it
            row(302).target_text = "Neu 302 Text"  # already what the file says
            row(420).target_text = "Linguist 420"  # edited: phase 1 planned only a touch
            other.commit()

    doc = _tmx_of(range(450), revised=set(range(400)))
    with _Knobs(100, pause=0.001, sleeper=sleeper), Session(engine) as s:
        res = tm.import_tmx(s, pid, doc, SRC, TGT)
    if not fired:
        _fail("the edit hook never ran")
    if _target_of(pid, 300) != "Neu 300 Text" or _target_of(pid, 420) != "Einheit 420 Text":
        _fail("an edited row must end with the file's target, as #584's re-run left it")
    if _target_of(pid, 301) is not None or _count(pid) != 449:
        _fail("a row deleted while the import ran was brought back")
    if _target_of(pid, 299) != "Neu 299 Text" or _target_of(pid, 0) != "Neu 0 Text":
        _fail("rows nobody touched must still get the file's target")
    # 400 planned updates - 301 (deleted) - 302 (already right) + 420 (touch became update)
    want = {"imported": 0, "updated": 399, "skipped": 50, "concurrent_deleted": 1}
    got = {k: res[k] for k in want}
    if got != want:
        _fail(f"the counts must say what this import wrote: {got} != {want}")
    # surfaces: the API reply and the CLI JSON carry the count; the web summary names it
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post(
        f"/api/projects/{pid}/tm/import",
        json={"tmx_b64": base64.b64encode(_tmx(5)).decode(), "source": SRC, "target": TGT},
        headers={"X-Loc-Token": _key()},
    )
    if r.status_code != 200 or r.json().get("concurrent_deleted") != 0:
        _fail(f"the API reply must carry concurrent_deleted: {r.status_code} {r.text[:200]}")
    path = os.path.join(_TMP, "five.tmx")
    with open(path, "wb") as fh:
        fh.write(_tmx(5))
    code, out, err = run_cli(
        ["--json", "tm", "import", str(pid), path, "--source", SRC, "--target", TGT]
    )
    if code != 0 or '"concurrent_deleted": 0' not in out:
        _fail(f"the CLI JSON must carry concurrent_deleted: {code} {out[:300]} {err[:300]}")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    try:
        page = c.get(f"/projects/{pid}?tm_imported=0&tm_deleted=2")
    finally:
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    if "did not restore 2 entries someone deleted while the import ran" not in page.text:
        _fail("the web import summary must name the deleted entries")
    print("  edit/delete during import: re-read per chunk, delete not restored (399/50/1) OK")


_KEY = []


def _key():
    if not _KEY:
        with Session(engine) as s:
            u = User(email="ic615-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
        _row, raw = apikeys.create_key(uid, "ic615", scopes="api docs")
        _KEY.append(raw)
    return _KEY[0]


def test_json_export_is_streamed():
    pid = _project("ic615-json")
    n = 6000
    with Session(engine) as s:
        tm.import_tmx(s, pid, _tmx(n), SRC, TGT)
    h = {"X-Loc-Token": _key()}
    c = TestClient(app)
    old_page = tm.TMX_EXPORT_PAGE
    tm.TMX_EXPORT_PAGE = 100
    try:
        j = c.get(
            f"/api/projects/{pid}/tm/export", params={"source": SRC, "target": TGT}, headers=h
        )
        raw = c.get(
            f"/api/projects/{pid}/tm/export",
            params={"source": SRC, "target": TGT, "raw": "true"},
            headers=h,
        )
    finally:
        tm.TMX_EXPORT_PAGE = old_page
    if j.status_code != 200 or j.headers.get("content-length") is not None:
        _fail(f"the JSON export must be a stream (no Content-Length): {dict(j.headers)}")
    body = j.json()
    xml = base64.b64decode(body["tmx_b64"])
    want = {"ok", "source", "target", "requested_source", "requested_target",
            "locales_normalized", "count", "tmx_b64", "bytes"}  # fmt: skip
    if set(body) != want or body["count"] != n or body["bytes"] != len(xml):
        _fail(f"the JSON export fields changed: {sorted(body)} count={body['count']}")
    if xml != raw.content:
        _fail("the JSON export's TMX must equal the raw stream byte for byte")
    # memory: consume the exact generator the route uses and keep nothing
    from app.web import api_cli_projects_2 as routes

    with Session(engine) as s:
        _src, _tgt, count, pieces = tm.export_tmx_stream(s, pid, SRC, TGT)
        tm.TMX_EXPORT_PAGE = 100
        tracemalloc.start()
        total = 0
        try:
            for chunk in routes._json_b64_stream({"ok": True, "count": count}, pieces):
                total += len(chunk)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            tm.TMX_EXPORT_PAGE = old_page
    if peak > total // 3:
        _fail(f"the streamed JSON export held {peak} bytes for a {total}-byte body")
    print(
        f"  json-export: streamed, equal to raw, peak {peak // 1024} KiB for {total // 1024} KiB OK"
    )


def test_partial_failure_is_honest_indexed_and_silent_about_internals():
    """Review: a partial import must (a) not leak the raw database error and (b) answer the API
    with 500 + partial + counts and no internal text."""
    pid = _project("ic615-honest")
    doc = _tmx(450)
    with _Knobs(100), Session(engine) as s, _FailNthInsert(3):
        try:
            tm.import_tmx(s, pid, doc, SRC, TGT)
        except tm.TmxImportIncomplete as exc:
            msg = str(exc)
        else:
            _fail("chunk-3 failure must raise TmxImportIncomplete")
    if "injected chunk failure" in msg or "RuntimeError" not in msg:
        _fail(f"the message must name the error class only, never its text: {msg}")
    pid2 = _project("ic615-honest-api")
    h = {"X-Loc-Token": _key()}
    c = TestClient(app, raise_server_exceptions=False)
    with _Knobs(100), _FailNthInsert(3):
        r = c.post(
            f"/api/projects/{pid2}/tm/import",
            json={"tmx_b64": base64.b64encode(doc).decode(), "source": SRC, "target": TGT},
            headers=h,
        )
    body = r.json()
    if r.status_code != 500 or body.get("partial") is not True:
        _fail(f"a partial API import must answer 500 with partial=true: {r.status_code} {body}")
    if (body.get("rows_written"), body.get("rows_total")) != (200, 450):
        _fail(f"the API must report the counts: {body}")
    if "injected chunk failure" in r.text:
        _fail("the API reply must not carry the raw database error")
    # Review gate: a DATABASE error before anything committed (first chunk, or a small import)
    # must not put its SQL or bound row text into the API reply either.
    from sqlalchemy.exc import OperationalError

    class _DbFail(_FailNthInsert):
        def _hook(self, conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO SEGMENTTM"):
                raise OperationalError(statement, [("Unit 7 secret row text",)], Exception("disk"))

    pid3 = _project("ic615-honest-db")
    with _DbFail(1):
        r = c.post(
            f"/api/projects/{pid3}/tm/import",
            json={"tmx_b64": base64.b64encode(_tmx(50)).decode(), "source": SRC, "target": TGT},
            headers=h,
        )
    if r.status_code != 500 or r.json().get("ok") is not False:
        _fail(f"a database error must answer 500: {r.status_code} {r.text[:300]}")
    if "INSERT" in r.text or "secret row text" in r.text or "Unit 7" in r.text:
        _fail(f"the API reply leaked the SQL or row text: {r.text[:300]}")
    if _count(pid3) != 0:
        _fail("a first-chunk database error must store nothing")
    print("  honest: error class only, API 500 partial without internals OK")


def test_cut_off_json_export_is_detectable():
    import json as _json

    from app.web import api_cli_projects_2 as routes

    pid = _project("ic615-cut")
    with Session(engine) as s:
        tm.import_tmx(s, pid, _tmx(300), SRC, TGT)
        _a, _b, count, pieces = tm.export_tmx_stream(s, pid, SRC, TGT)
        parts = list(routes._json_b64_stream({"ok": True, "count": count}, pieces))
    if _json.loads(b"".join(parts))["count"] != 300:
        _fail("the complete stream must be valid JSON")
    for cut in (1, 2):
        try:
            _json.loads(b"".join(parts[:-cut]))
        except ValueError:
            continue
        _fail("a stream cut before its end must not parse as JSON")
    print("  cut-off: a truncated JSON export never parses OK")


BAD_CSV = b"source,target\nhello,hallo\nworld,welt\n"


def _check_msg(where, text):
    text = urllib.parse.unquote(str(text))
    for need in ("source_term", "target_term", "source, target"):
        if need not in text:
            _fail(f"{where}: the refusal must name '{need}': {text[:300]}")


def test_csv_header_every_surface():
    from app.services import glossary_module as gm

    pid = _project("ic615-csv")
    with Session(engine) as s:
        gid = gm.create(s, "ic615 module").id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
    h = {"X-Loc-Token": _key()}
    c = TestClient(app, raise_server_exceptions=False)
    payload = {"csv_b64": base64.b64encode(BAD_CSV).decode(), "source": SRC, "target": TGT}
    r = c.post(f"/api/projects/{pid}/glossary/import", json=payload, headers=h)
    if r.status_code != 400 or r.json().get("ok") is not False:
        _fail(f"api project glossary: {r.status_code} {r.text[:200]}")
    _check_msg("api project glossary", r.json()["error"])
    r = c.post(f"/api/glossaries/{gid}/import", json={**payload, "filename": "g.csv"}, headers=h)
    if r.status_code != 400:
        _fail(f"api module glossary: {r.status_code} {r.text[:200]}")
    _check_msg("api module glossary", r.json()["error"])
    # web (session-gated routes: run with the login wall off)
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    try:
        files = {"file": ("g.csv", BAD_CSV, "text/csv")}
        form = {"source_locale": SRC, "target_locale": TGT}
        w = c.post(
            f"/projects/{pid}/glossary/import", data=form, files=files, follow_redirects=False
        )
        if w.status_code != 303 or "err=" not in w.headers.get("location", ""):
            _fail(f"web project glossary: {w.status_code} {w.headers.get('location')}")
        _check_msg("web project glossary", w.headers["location"])
        w = c.post(f"/glossaries/{gid}/import", data=form, files=files, follow_redirects=False)
        if w.status_code != 303 or "err=" not in w.headers.get("location", ""):
            _fail(f"web module glossary: {w.status_code} {w.headers.get('location')}")
        _check_msg("web module glossary", w.headers["location"])
        page = c.get(w.headers["location"])
        if "source_term" not in page.text:
            _fail("the module glossary page must show the refusal text")
    finally:
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    # CLI (in-process)
    path = os.path.join(_TMP, "bad.csv")
    with open(path, "wb") as fh:
        fh.write(BAD_CSV)
    for label, argv in (
        ("cli project glossary", ["glossary", "import", str(pid), path]),
        ("cli module glossary", ["glossary", "module", "import", str(gid), path]),
    ):
        code, out, err = run_cli([*argv, "--source", SRC, "--target", TGT])
        if code == 0:
            _fail(f"{label}: must exit non-zero, got 0: {out[:200]}")
        _check_msg(label, out + err)
    # MCP tool (edit_glossary action=import) against the real app
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    mcp_server.BASE_URL = "http://testserver"
    mcp_server.TOKEN = _key()
    mcp_server.OUTPUT_DIR = pathlib.Path(_TMP)
    mcp_server._client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), timeout=60
    )
    try:
        asyncio.run(
            mcp_server.call_tool_unvalidated(
                "edit_glossary",
                {
                    "action": "import",
                    "glossary_id": gid,
                    "data": BAD_CSV.decode(),
                    "source": SRC,
                    "target": TGT,
                },
            )
        )
    except Exception as exc:
        _check_msg("mcp edit_glossary", exc)
    else:
        _fail("mcp edit_glossary import of a bad header must raise")
    with Session(engine) as s:
        n = s.exec(select(func.count()).select_from(GlossaryEntry)).one()
    if n:
        _fail(f"a refused CSV must write nothing, found {n} entries")
    print("  csv-header: refused naming the columns on web x2, api x2, cli x2 and mcp, 0 rows OK")


if __name__ == "__main__":
    test_chunk_bound()
    test_lock_free_between_chunks()
    test_failure_halfway_then_retry()
    test_writer_between_chunks_is_not_duplicated()
    test_edit_or_delete_during_import_is_reread_and_counted()
    test_partial_failure_is_honest_indexed_and_silent_about_internals()
    test_json_export_is_streamed()
    test_cut_off_json_export_is_detectable()
    test_csv_header_every_surface()
    print("IMPORT CHUNKS 615 OK")
