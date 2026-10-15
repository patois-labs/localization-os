"""Trados Studio .sdltm import into segment TM (format batch, id sdltm).

An .sdltm is a SQLite database: `translation_units` rows hold the source/target segment as
XML (<Segment><Elements><Text><Value>..</Value></Text><Tag>..</Tag></Elements>
<CultureName>..</CultureName></Segment>) and `translation_memories` holds the TM's languages.
The fixtures below are HAND-MADE: this suite creates the SQLite schema itself (no third-party
TM data).

Pinned:
  1. reader: text + Trados tags -> the segment token scheme (g = paired by Anchor, x = point;
     target tags follow the source number through AlignmentAnchor / TagID); CultureName;
     UTF-16 BLOB segments; a file without translation_memories takes CultureName.
  2. service: clean units import through tm.import_tmx (dedupe, integrity gate: tagged units
     withheld as tagged_source, placeholder loss as placeholder_integrity), unreadable units
     counted in skipped_unreadable with sdltm_* codes, row numbers = SDLTM positions;
     re-import is a no-op, a changed target is `updated`.
  3. languages: a TM of another pair is refused before any write; a reversed pair imports.
  4. hostile: not SQLite / no table / a VIEW named translation_units / missing columns /
     corrupt database -> SdltmError; DOCTYPE + entity, oversize XML -> per-unit skip; row cap;
     the temp copy is removed; the database is opened read-only (the input never changes).
  5. surfaces: /api (tmx_b64), web form (redirect counters + page text, accept list),
     CLI (local), MCP (file_path reads the binary file; description names SDLTM).

Hermetic: throwaway LOC_TMS_DATA_DIR. Final line: "SDLTM IMPORT OK".
"""

import base64
import glob
import json
import os
import sqlite3
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_sdltm_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.filters.base import InputLimitError
from app.main import app
from app.models import Project, Role, SegmentTM, TargetLocale
from app.services import apikeys, security, settings, tm, tm_sdltm
from tests._cli_runner import run_cli

init_db()
_SRC, _TGT = "en-US", "de-DE"
_L, _R = "⟦", "⟧"
client = TestClient(app)


def _tok(name):
    return _L + name + _R


def _fail(msg):
    print("FAIL:", ascii(msg))
    sys.exit(1)


# --- fixture builder ---------------------------------------------------------------------
def _text(v):
    v = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<Text><Value>{v}</Value></Text>"


def _tag(kind, anchor="", align="", tag_id=""):
    return (
        f"<Tag><Type>{kind}</Type><Anchor>{anchor}</Anchor>"
        f"<AlignmentAnchor>{align}</AlignmentAnchor><TagID>{tag_id}</TagID>"
        "<TextEquivalent /></Tag>"
    )


def _seg(parts, culture):
    return (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<Segment xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<Elements>{''.join(parts)}</Elements><CultureName>{culture}</CultureName></Segment>"
    )


def _plain(src, tgt, s=_SRC, t=_TGT):
    return (_seg([_text(src)], s), _seg([_text(tgt)], t))


def _build(path, units, *, langs=(_SRC, _TGT), with_tm_table=True, tu_columns=None):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    if with_tm_table:
        con.execute(
            "CREATE TABLE translation_memories (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, "
            "source_language TEXT, target_language TEXT, copyright TEXT, description TEXT, "
            "settings INTEGER, creation_user TEXT, creation_date DATETIME)"
        )
        con.execute(
            "INSERT INTO translation_memories (guid, name, source_language, target_language) "
            "VALUES ('g', 'hand-made', ?, ?)",
            langs,
        )
    cols = tu_columns or (
        "id INTEGER PRIMARY KEY, guid TEXT, translation_memory_id INTEGER, source_hash INTEGER, "
        "source_segment TEXT, target_hash INTEGER, target_segment TEXT, creation_date DATETIME, "
        "creation_user TEXT, change_date DATETIME, change_user TEXT, last_used_date DATETIME, "
        "last_used_user TEXT, usage_counter INTEGER, flags INTEGER, source_token_data BLOB, "
        "target_token_data BLOB, alignment_data BLOB"
    )
    con.execute(f"CREATE TABLE translation_units ({cols})")
    if tu_columns is None:
        for src, tgt in units:
            con.execute(
                "INSERT INTO translation_units (guid, translation_memory_id, source_segment, "
                "target_segment, usage_counter, flags) VALUES ('u', 1, ?, ?, 0, 0)",
                (src, tgt),
            )
    con.commit()
    con.close()
    with open(path, "rb") as fh:
        return fh.read()


_CLEAN = [
    ("Welcome back", "Willkommen zuruck"),
    ("Save your work", "Speichern Sie Ihre Arbeit"),
    ("You have {count} items", "Sie haben {count} Artikel"),
    ("Sign out", "Abmelden"),
]
_TAGGED = (
    _seg(
        [_text("Click "), _tag("Start", "1", "1", "1"), _text("Save"), _tag("End", "1", "1", "1")],
        _SRC,
    ),
    _seg(
        [
            _tag("Start", "1", "1", "1"),
            _text("Speichern"),
            _tag("End", "1", "1", "1"),
            _text(" klicken"),
        ],
        _TGT,
    ),
)
_PH_DROP = _plain("Hello {name}", "Hallo")
_BAD_XML = ("<Segment><Elements><Text><Value>broken", _seg([_text("x")], _TGT))
_ENTITY = (
    '<!DOCTYPE Segment [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
    "<Segment><Elements><Text><Value>&b;</Value></Text></Elements></Segment>",
    _seg([_text("x")], _TGT),
)
_EMPTY_TGT = (_seg([_text("Lonely source")], _SRC), _seg([], _TGT))
_UNSUPPORTED = (
    _seg([_text("Date "), "<Number><Value>5</Value></Number>"], _SRC),
    _seg([_text("x")], _TGT),
)
_MIXED = [_plain(s, t) for s, t in _CLEAN] + [
    _TAGGED,
    _PH_DROP,
    _BAD_XML,
    _ENTITY,
    _EMPTY_TGT,
    _UNSUPPORTED,
]


def _project(name, src=_SRC, tgt=_TGT):
    with Session(engine) as s:
        p = Project(name=name, source_locale=src)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=tgt))
        s.commit()
        return p.id


def _stored(pid):
    with Session(engine) as s:
        return {
            r.source_text: r.target_text
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
        }


def _path(name):
    return os.path.join(_TMP, name)


def _leftovers():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "loc-sdltm-*")))


# --- 1. reader ---------------------------------------------------------------------------
def test_reader():
    src, tgt, sl, tl = tm_sdltm._unit_pair(*_TAGGED)
    if src != "Click " + _tok("g1") + "Save" + _tok("/g1"):
        _fail(f"source tokens: {src!r}")
    if tgt != _tok("g1") + "Speichern" + _tok("/g1") + " klicken":
        _fail(f"target tokens: {tgt!r}")
    if (sl, tl) != (_SRC, _TGT):
        _fail(f"cultures: {sl} {tl}")
    # Two tags whose order flips in the target keep their source numbers (AlignmentAnchor).
    s2 = _seg(
        [
            _tag("Start", "1", "1", "1"),
            _text("A"),
            _tag("End", "1", "1", "1"),
            _text(" "),
            _tag("Standalone", "2", "2", "2"),
            _tag("TextPlaceholder", "3", "3", "3"),
        ],
        _SRC,
    )
    t2 = _seg(
        [
            _tag("TextPlaceholder", "3", "3", "3"),
            _tag("Standalone", "9", "2", "2"),
            _text(" "),
            _tag("Start", "7", "1", "1"),
            _text("B"),
            _tag("End", "7", "1", "1"),
        ],
        _TGT,
    )
    s, t, _a, _b = tm_sdltm._unit_pair(s2, t2)
    if s != _tok("g1") + "A" + _tok("/g1") + " " + _tok("x2") + _tok("x3"):
        _fail(f"source numbering: {s!r}")
    if t != _tok("x3") + _tok("x2") + " " + _tok("g1") + "B" + _tok("/g1"):
        _fail(f"target follows AlignmentAnchor: {t!r}")
    # An unaligned target tag gets a fresh number (the integrity gate then reports it).
    t3 = _seg([_tag("Standalone", "5", "", "77"), _text("A")], _TGT)
    _s, t, _a, _b = tm_sdltm._unit_pair(_seg([_text("A")], _SRC), t3)
    if t != _tok("x1") + "A":
        _fail(f"fresh target tag: {t!r}")
    # UTF-16 BLOB segments with a BOM, no translation_memories table (CultureName decides).
    path = _path("utf16.sdltm")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE translation_units "
        "(id INTEGER PRIMARY KEY, source_segment BLOB, target_segment BLOB)"
    )
    a, b = _plain("Grüße", "Greetings ☃", _SRC, _TGT)
    con.execute(
        "INSERT INTO translation_units (source_segment, target_segment) VALUES (?, ?)",
        (a.encode("utf-16"), b.encode("utf-16")),
    )
    con.commit()
    con.close()
    with open(path, "rb") as fh:
        read = tm_sdltm.read_sdltm_units(fh.read())
    if read["tm_languages"] is not None or read["units"][0][2:] != (
        "Grüße",
        "Greetings ☃",
        _SRC,
        _TGT,
    ):
        _fail(f"utf-16 blobs / culture fallback: {read}")
    print("  1. reader: tokens, alignment, UTF-16 blobs, CultureName fallback OK")


# --- 2. service --------------------------------------------------------------------------
def test_service():
    pid = _project("sdltm-svc")
    data = _build(_path("mixed.sdltm"), _MIXED)
    before = _leftovers()
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, data, _SRC, _TGT)
    if _leftovers() - before:
        _fail("the temp copy must be removed")
    with open(_path("mixed.sdltm"), "rb") as fh:
        if fh.read() != data:
            _fail("the input database must not change")
    stored = _stored(pid)
    if set(stored) != {c[0] for c in _CLEAN} or stored["Sign out"] != "Abmelden":
        _fail(f"clean units only: {sorted(stored)}")
    if res["imported"] != 4 or res["skipped_integrity"] != 2 or res["skipped_unreadable"] != 4:
        _fail(f"counts: {res}")
    codes = res["skipped_by_code"]
    want = {
        "tagged_source": 1,
        "placeholder_integrity": 1,
        "sdltm_bad_xml": 2,
        "sdltm_empty": 1,
        "sdltm_unsupported_element": 1,
    }
    if codes != want:
        _fail(f"codes: {codes}")
    rows = {r["row"]: r["code"] for r in res["skipped_rows"]}
    if rows != {
        5: "tagged_source",
        6: "placeholder_integrity",
        7: "sdltm_bad_xml",
        8: "sdltm_bad_xml",
        9: "sdltm_empty",
        10: "sdltm_unsupported_element",
    }:
        _fail(f"rows are SDLTM positions: {rows}")
    if (
        res["format"] != "sdltm"
        or res["sdltm_units"] != 10
        or res["sdltm_languages"] != [_SRC, _TGT]
    ):
        _fail(f"format fields: {res}")
    # Re-import: nothing new; a changed target merges into the existing entry.
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, data, _SRC, _TGT)
    if res["imported"] != 0 or res["skipped"] != 4:
        _fail(f"re-import must be a no-op: {res}")
    data2 = _build(_path("rev.sdltm"), [_plain("Sign out", "Ausloggen")])
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, data2, _SRC, _TGT)
    if res["updated"] != 1 or _stored(pid)["Sign out"] != "Ausloggen" or len(_stored(pid)) != 4:
        _fail(f"changed target must update in place: {res}")
    # acknowledge_placeholder_loss passes through.
    pid2 = _project("sdltm-ack")
    with Session(engine) as s:
        res = tm.import_tmx(s, pid2, data, _SRC, _TGT, acknowledge_placeholder_loss=True)
    if res["acknowledged_placeholder_loss"] != 1 or "Hello {name}" not in _stored(pid2):
        _fail(f"acknowledge: {res}")
    print("  2. service: import, gate, unreadable codes, positions, re-import, merge OK")


# --- 3. languages ------------------------------------------------------------------------
def test_languages():
    pid = _project("sdltm-fr", _SRC, "fr-FR")
    data = _build(_path("de.sdltm"), [_plain("Hi", "Hallo")])
    with Session(engine) as s:
        try:
            tm.import_tmx(s, pid, data, _SRC, "fr-FR")
            _fail("a de-DE memory must not import as fr-FR")
        except tm.SdltmError as exc:
            if "en-US -> de-DE" not in str(exc):
                _fail(f"message names the memory's pair: {exc}")
    if _stored(pid):
        _fail("nothing may be written on a refused pair")
    # Reversed: a de-DE -> en-US memory imports into an en-US -> de-DE project.
    pid = _project("sdltm-rev")
    data = _build(
        _path("rev2.sdltm"), [_plain("Hallo Welt", "Hello world", _TGT, _SRC)], langs=(_TGT, _SRC)
    )
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, data, _SRC, _TGT)
    if res["imported"] != 1 or _stored(pid) != {"Hello world": "Hallo Welt"}:
        _fail(f"reversed pair: {res} {_stored(pid)}")
    # A unit whose own CultureName is another language is skipped with sdltm_language.
    pid = _project("sdltm-unitlang")
    data = _build(
        _path("ul.sdltm"), [_plain("A one", "B eins"), _plain("C", "D", "ja-JP", "ko-KR")]
    )
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, data, _SRC, _TGT)
    if res["imported"] != 1 or res["skipped_by_code"].get("sdltm_language") != 1:
        _fail(f"unit language skip: {res}")
    print("  3. languages: other pair refused, reversed imports, unit language skipped OK")


# --- 4. hostile --------------------------------------------------------------------------
def _readb(p):
    with open(p, "rb") as fh:
        return fh.read()


def _expect_sdltm_error(data, needle, label):
    pid = _project("sdltm-bad-" + label)
    with Session(engine) as s:
        try:
            tm.import_tmx(s, pid, data, _SRC, _TGT)
        except tm.SdltmError as exc:
            if needle not in str(exc):
                _fail(f"{label}: {exc}")
            return
    _fail(f"{label}: must be refused")


def test_hostile():
    if tm_sdltm.is_sdltm(b"<tmx/>") or not tm_sdltm.is_sdltm(b"SQLite format 3\x00rest"):
        _fail("is_sdltm sniff")
    # no translation_units table
    p = _path("empty.sqlite")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE other (x)")
    con.commit()
    con.close()
    _expect_sdltm_error(_readb(p), "no translation_units table", "notable")
    # a VIEW pretending to be the table (could run arbitrary recursive SQL)
    p = _path("view.sqlite")
    con = sqlite3.connect(p)
    con.execute(
        "CREATE VIEW translation_units AS WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
        "SELECT x+1 FROM c) SELECT x AS id, 'a' AS source_segment, 'b' AS target_segment FROM c"
    )
    con.commit()
    con.close()
    _expect_sdltm_error(_readb(p), "no translation_units table", "view")
    # missing segment columns
    data = _build(_path("cols.sdltm"), [], tu_columns="id INTEGER PRIMARY KEY, src TEXT, tgt TEXT")
    _expect_sdltm_error(data, "source_segment/target_segment", "cols")
    # a GENERATED segment column (an expression the reader would evaluate on every row) is
    # not a real column: refused before any row is read
    data = _build(
        _path("gen.sdltm"),
        [],
        tu_columns="id INTEGER PRIMARY KEY, target_segment TEXT, source_segment TEXT "
        "GENERATED ALWAYS AS (hex(zeroblob(400000000))) VIRTUAL",
    )
    _expect_sdltm_error(data, "source_segment/target_segment", "generated")
    # corrupt database: valid header, garbage pages
    good = _build(_path("corrupt.sdltm"), [_plain("a", "b")])
    _expect_sdltm_error(good[:100] + b"\xff" * (len(good) - 100), "could not read", "corrupt")
    _expect_sdltm_error(b"SQLite format 3\x00" + b"\x00" * 50, "could not read", "short")
    # oversize segment XML -> per-unit skip; billion laughs covered in the service test
    big = _seg([_text("x" * (tm_sdltm.SDLTM_MAX_SEGMENT_XML + 10))], _SRC)
    data = _build(_path("big.sdltm"), [(big, _seg([_text("y")], _TGT)), _plain("ok", "gut")])
    pid = _project("sdltm-big")
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, data, _SRC, _TGT)
    if res["imported"] != 1 or res["skipped_by_code"].get("sdltm_too_large") != 1:
        _fail(f"oversize: {res}")
    # row cap
    saved = config.MAX_DOCUMENT_ROWS
    config.MAX_DOCUMENT_ROWS = 2
    try:
        data = _build(_path("cap.sdltm"), [_plain(f"r{i}", f"z{i}") for i in range(3)])
        pid = _project("sdltm-cap")
        with Session(engine) as s:
            try:
                tm.import_tmx(s, pid, data, _SRC, _TGT)
                _fail("row cap must refuse")
            except InputLimitError:
                pass
        if _stored(pid):
            _fail("nothing may be written over the row cap")
    finally:
        config.MAX_DOCUMENT_ROWS = saved
    # the read-only connection refuses writes and other tables
    conn = tm_sdltm._open_readonly(_path("mixed.sdltm"))
    try:
        defensive = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
        if defensive is not None and hasattr(conn, "getconfig") and not conn.getconfig(defensive):
            _fail("SQLite defensive mode is off on the untrusted connection")
        for sql in (
            "DELETE FROM translation_units",
            "ATTACH DATABASE ':memory:' AS x",
            "SELECT load_extension('x')",
        ):
            try:
                conn.execute(sql)
                _fail(f"read-only connection allowed: {sql}")
            except sqlite3.DatabaseError:
                pass
    finally:
        conn.close()
    print("  4. hostile: sniff, no table, view, columns, corrupt, oversize, row cap, read-only OK")


# --- 5. surfaces -------------------------------------------------------------------------
def _admin_token():
    user = security.create_user("sdltm-admin@x.com", "Sd", Role.ADMIN, "sdltm-password-123")
    _row, token = apikeys.create_key(user.id, "sdltm", scopes="api docs")
    return token


def test_surfaces():
    data = _build(_path("surf.sdltm"), _MIXED)
    # API
    pid = _project("sdltm-api")
    h = {"X-Loc-Token": _admin_token()}
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        headers=h,
        json={"tmx_b64": base64.b64encode(data).decode(), "source": _SRC, "target": _TGT},
    )
    j = r.json()
    if r.status_code != 200 or j.get("imported") != 4 or j.get("skipped_unreadable") != 4:
        _fail(f"/api: {r.status_code} {j}")
    if j.get("format") != "sdltm" or j.get("sdltm_units") != 10:
        _fail(f"/api format fields: {j}")
    pid_fr = _project("sdltm-api-fr", _SRC, "fr-FR")
    r = client.post(
        f"/api/projects/{pid_fr}/tm/import",
        headers=h,
        json={"tmx_b64": base64.b64encode(data).decode(), "source": _SRC, "target": "fr-FR"},
    )
    if r.status_code != 400 or "en-US -> de-DE" not in r.json().get("error", ""):
        _fail(f"/api wrong pair: {r.status_code} {r.text}")
    # web
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    pid = _project("sdltm-web")
    page = client.get(f"/projects/{pid}").text
    if ".sdltm" not in page:
        _fail("the TM import form must accept .sdltm")
    r = client.post(
        f"/projects/{pid}/tm/import",
        data={"source_locale": _SRC, "target_locale": _TGT},
        files={"file": ("memory.sdltm", data, "application/octet-stream")},
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    if r.status_code != 303 or "tm_imported=4" not in loc or "tm_skipped_unreadable=4" not in loc:
        _fail(f"web redirect: {r.status_code} {loc}")
    if "could not read 4 SDLTM unit(s)" not in client.get(loc).text:
        _fail("project page must say how many SDLTM units were unreadable")
    pid_fr = _project("sdltm-web-fr", _SRC, "fr-FR")
    r = client.post(
        f"/projects/{pid_fr}/tm/import",
        data={"source_locale": _SRC, "target_locale": "fr-FR"},
        files={"file": ("memory.sdltm", data, "application/octet-stream")},
        follow_redirects=False,
    )
    if r.status_code != 303 or "de-DE memory" not in client.get(r.headers["location"]).text:
        _fail(f"web wrong pair must say why: {r.headers.get('location')}")
    # CLI (local)
    pid = _project("sdltm-cli")
    path = _path("cli.sdltm")
    with open(path, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["tm", "import", str(pid), path, "--source", _SRC, "--target", _TGT])
    text = out + err
    if code != 0 or "Imported 4" not in text or "could not read 4 SDLTM unit(s)" not in text:
        _fail(f"CLI: rc={code} {text}")
    if "[sdltm_bad_xml]" not in text or not text.isascii():
        _fail(f"CLI lists unreadable units in ASCII: {text}")
    code, out, _ = run_cli(
        ["--json", "tm", "import", str(pid), path, "--source", _SRC, "--target", _TGT]
    )
    j = json.loads(out.strip().splitlines()[-1])
    if code != 0 or j.get("format") != "sdltm" or j.get("skipped") != 4:
        _fail(f"CLI --json: rc={code} {j}")
    # MCP
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import registry
    import server

    tool = next(t for t in registry.TOOLS if t.name == "manage_tm")
    if (
        "SDLTM" not in tool.summary
        or ".sdltm" not in tool.input_schema["properties"]["file_path"]["description"]
    ):
        _fail("manage_tm must name SDLTM")
    sent = []

    async def fake_post(client_, p, body, *a, **k):
        sent.append(body)
        return {"ok": True}

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    import asyncio

    server._post_json, saved = fake_post, server._post_json
    server._client, saved_c = (lambda: _Ctx()), server._client
    server._allow_local_file_reads(True, roots=[os.path.dirname(path)])  # #755 input folder
    try:
        asyncio.run(
            server.manage_tm(
                "import", project_id=1, file_path=path, source_locale=_SRC, target_locale=_TGT
            )
        )
    finally:
        server._post_json = saved
        server._client = saved_c
        server._allow_local_file_reads(False)
    if base64.b64decode(sent[0]["tmx_b64"]) != data:
        _fail("MCP file_path must send the .sdltm bytes unchanged")
    print("  5. surfaces: /api, web, CLI, MCP OK")


def main():
    test_reader()
    test_service()
    test_languages()
    test_hostile()
    test_surfaces()
    print("SDLTM IMPORT OK")


if __name__ == "__main__":
    main()
