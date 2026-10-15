"""Issue #615: big TMX/CSV imports and TM exports stop starving the rest of the app.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Covers (the timing numbers for 30k/100k/300k units live in the hand-off, measured on a real
server; this suite pins behaviour):
  csv-header   -- a glossary CSV whose header has no `source_term` column (`source,target`)
                  used to answer ok:true with 0 entries. It is now refused with the columns
                  found, on the /api route and the service, and nothing is written; the
                  documented header still imports.
  raw-export   -- GET /api/projects/{pid}/tm/export?raw=true streams the TMX itself with the
                  effective locales and the unit count in X-TM-* headers; its <tu> set equals
                  the JSON export's (same rows, same text, same meta props); the JSON default
                  is unchanged; the web download streams the same rows too.
  raw-cut      -- a stream that fails after the 200 ends without its closing </tmx>, and the
                  importer refuses that file (a cut-off export never looks complete).
  stream-pages -- the stream pages by id (more units than one page) and a unit with markup
                  characters round-trips through a TMX re-import.
  gil-interval -- app startup applies config.GIL_SWITCH_INTERVAL_S (0.5 ms on Linux, CPython's
                  5 ms on Windows) so one CPU-heavy thread cannot stretch every other request
                  by 5 ms per GIL hand-off.
  sorted-insert -- a TMX import still stores every unit (the insert is hash-ordered now).

Final line: "BIG IMPORTS 615 OK".
"""

import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bigimports615_")

from fastapi.testclient import TestClient
from lxml import etree
from sqlmodel import Session, func, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import GlossaryEntry, Project, Role, TargetLocale, User
from app.services import apikeys, glossary, settings, tm

init_db()
SRC, TGT = "en-US", "de-DE"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


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


_KEY = []


def _key():
    if not _KEY:
        with Session(engine) as s:
            u = User(email="bi615-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
        _row, raw = apikeys.create_key(uid, "bi615", scopes="api docs")
        _KEY.append(raw)
    return _KEY[0]


def _tmx(n, prefix="Sentence"):
    units = "".join(
        f'<tu><tuv xml:lang="{SRC}"><seg>{prefix} {i} &amp; more &lt;b&gt;</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>Satz {i} &amp; mehr</seg></tuv></tu>'
        for i in range(n)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="{SRC}"/>'
        f"<body>{units}</body></tmx>"
    ).encode()


def _tus(xml_bytes):
    root = etree.fromstring(xml_bytes)
    out = {}
    for tu in root.iter("tu"):
        langs = {tuv.get(XML_LANG): tuv.findtext("seg") for tuv in tu.iter("tuv")}
        out[langs[SRC]] = (
            langs[TGT],
            tu.get("creationtool"),
            tuple(sorted((p.get("type"), p.text) for p in tu.iter("prop"))),
        )
    return out


def test_csv_header_refused():
    pid = _project("bi615-csv")
    bad = b"source,target\nhello,hallo\nworld,welt\n"
    with Session(engine) as s:
        try:
            glossary.import_csv(s, pid, bad, SRC, TGT)
        except ValueError as exc:
            if "source_term" not in str(exc) or "source, target" not in str(exc):
                _fail(f"the refusal must name the missing column and the found ones: {exc}")
        else:
            _fail("a CSV without a source_term column must be refused, not imported as 0 rows")
        n = s.exec(select(func.count()).select_from(GlossaryEntry)).one()
        if n:
            _fail(f"a refused CSV must write nothing, found {n} entries")
    h = {"X-Loc-Token": _key()}
    c = TestClient(app)
    r = c.post(
        f"/api/projects/{pid}/glossary/import",
        json={
            "source": SRC,
            "target": TGT,
            "csv_b64": base64.b64encode(bad).decode(),
            "filename": "g.csv",
        },
        headers=h,
    )
    if r.status_code != 400 or "source_term" not in (r.json().get("error") or ""):
        _fail(f"/api must answer 400 naming source_term: {r.status_code} {r.text[:200]}")
    good = b"source_term,target_term\nhello,hallo\nworld,welt\n"
    r = c.post(
        f"/api/projects/{pid}/glossary/import",
        json={
            "source": SRC,
            "target": TGT,
            "csv_b64": base64.b64encode(good).decode(),
            "filename": "g.csv",
        },
        headers=h,
    )
    if r.status_code != 200 or r.json().get("entries") != 2:
        _fail(f"the documented header must still import: {r.status_code} {r.text[:200]}")
    # an empty file keeps importing nothing (no header to judge)
    with Session(engine) as s:
        res = glossary.import_csv(s, pid, b"", SRC, TGT)
        if res["entries"] != 0:
            _fail(f"an empty CSV imports nothing: {res}")
    print("  csv-header: `source,target` refused naming the columns, valid header imports OK")


def test_raw_export_equals_json_export():
    pid = _project("bi615-export")
    n = tm.TMX_EXPORT_PAGE + 137  # more than one page
    with Session(engine) as s:
        res = tm.import_tmx(s, pid, _tmx(n), SRC, TGT)
        if res["imported"] != n:
            _fail(f"sorted insert must still store every unit: {res}")
        # a human-approved row carries meta props + creationtool in the export
        tm.write_back(s, pid, "Approved by a person", "Von einer Person freigegeben", SRC, TGT)
    h = {"X-Loc-Token": _key()}
    c = TestClient(app)
    j = c.get(f"/api/projects/{pid}/tm/export", params={"source": SRC, "target": TGT}, headers=h)
    if j.status_code != 200 or "tmx_b64" not in j.json():
        _fail(f"the JSON default must be unchanged: {j.status_code} {j.text[:120]}")
    jbody = j.json()
    json_tus = _tus(base64.b64decode(jbody["tmx_b64"]))
    r = c.get(
        f"/api/projects/{pid}/tm/export",
        params={"source": SRC, "target": TGT, "raw": "true"},
        headers=h,
    )
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith(
        "application/x-tmx+xml"
    ):
        _fail(f"raw export must stream TMX: {r.status_code} {r.headers.get('content-type')}")
    if r.headers.get("x-tm-count") != str(jbody["count"]) or jbody["count"] != n + 1:
        _fail(f"X-TM-Count must equal the JSON count: {dict(r.headers)} vs {jbody['count']}")
    if (r.headers.get("x-tm-source"), r.headers.get("x-tm-target")) != (SRC, TGT):
        _fail(f"X-TM-Source/Target must echo the effective locales: {dict(r.headers)}")
    raw_tus = _tus(r.content)
    if raw_tus != json_tus:
        miss = set(json_tus) ^ set(raw_tus)
        diff = [k for k in json_tus if k in raw_tus and json_tus[k] != raw_tus[k]]
        _fail(f"raw and JSON exports differ: only-in-one={list(miss)[:3]} changed={diff[:3]}")
    if json_tus["Approved by a person"][1] is None:
        _fail("the approved row must carry creationtool provenance in both exports")
    # the web download streams the same units
    settings.set_many({"security_auth_required": "0"})  # the web route is session-gated
    settings.invalidate_cache()
    try:
        w = c.get(f"/projects/{pid}/tm/export?source={SRC}&target={TGT}")
    finally:
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    if w.status_code != 200 or _tus(w.content) != json_tus:
        _fail(f"the web download must carry the same units: {w.status_code}")
    # the stream round-trips through a re-import (markup characters survive)
    pid2 = _project("bi615-roundtrip")
    with Session(engine) as s:
        res = tm.import_tmx(s, pid2, r.content, SRC, TGT)
        if res["imported"] + res["skipped"] < n:
            _fail(f"the streamed TMX must re-import: {res}")
        hit = tm.lookup(s, pid2, "Sentence 5 & more <b>", SRC, TGT)
        if not hit or hit["match_pct"] != 100:
            _fail("a unit with & and < must survive stream -> import")
    pages = n // tm.TMX_EXPORT_PAGE + 1
    print(f"  raw-export: {n + 1} units over {pages} pages equal the JSON export OK")


def test_raw_export_headers_and_auth():
    """Review: the raw stream is gated exactly like the JSON export, and a hostile locale
    query value cannot 500 the response or inject a header."""
    pid = _project("bi615-hdr")
    h = {"X-Loc-Token": _key()}
    c = TestClient(app, raise_server_exceptions=False)
    for src in ("日本語", "en-US\r\nX-Evil: 1"):
        r = c.get(
            f"/api/projects/{pid}/tm/export",
            params={"source": src, "target": TGT, "raw": "true"},
            headers=h,
        )
        if r.status_code != 200 or "x-evil" in r.headers:
            _fail(f"a hostile source value must not break or inject: {r.status_code}")
    if c.get(
        f"/api/projects/{pid}/tm/export", params={"source": SRC, "target": TGT, "raw": "true"}
    ).status_code not in (401, 403):
        _fail("the raw export must require the same credentials as the JSON export")
    if (
        c.get(
            "/api/projects/999999/tm/export",
            params={"source": SRC, "target": TGT, "raw": "true"},
            headers=h,
        ).status_code
        != 404
    ):
        _fail("the raw export of a missing project must be 404 like the JSON export")
    print("  raw-headers: hostile locale values are encoded, auth and 404 match the JSON route OK")


def test_raw_export_cut_short_is_not_valid_tmx():
    """Gate: a failure after the 200 is sent cannot change the status, so a cut-off stream must
    never look like a complete export. The closing </tmx> is the end marker: a stream that
    fails part way ends without it, and the TMX importer refuses that file outright."""
    pid = _project("bi615-cut")
    n = tm.TMX_EXPORT_PAGE + 10  # two pages: the failure lands after the first page is sent
    with Session(engine) as s:
        tm.import_tmx(s, pid, _tmx(n), SRC, TGT)
    real = tm._tmx_stamp_tu
    calls = []

    def stamp(tu, e):
        calls.append(1)
        if len(calls) > tm.TMX_EXPORT_PAGE:
            raise ValueError("All strings must be XML compatible")  # a bad stored row
        return real(tu, e)

    tm._tmx_stamp_tu = stamp
    body, err = b"", None
    try:
        with Session(engine) as s:
            _src, _tgt, count, pieces = tm.export_tmx_stream(s, pid, SRC, TGT)
            try:
                for piece in pieces:
                    body += piece
            except ValueError as exc:
                err = exc
    finally:
        tm._tmx_stamp_tu = real
    if err is None or count != n:
        _fail(f"the stream must raise part way (count {count}): {err}")
    if b"<tu" not in body or body.rstrip().endswith(b"</tmx>"):
        _fail("a cut-off stream must have sent data but never its closing </tmx>")
    pid2 = _project("bi615-cut-reimport")
    with Session(engine) as s:
        try:
            res = tm.import_tmx(s, pid2, body, SRC, TGT)
        except Exception:  # any refusal is the point
            res = None
        stored = s.exec(
            select(func.count()).select_from(tm.SegmentTM).where(tm.SegmentTM.project_id == pid2)
        ).one()
    if (res is not None and res.get("imported")) or stored:
        _fail(f"a cut-off export must not import as if complete: {res} stored={stored}")
    print("  raw-cut: a stream that fails part way ends without </tmx> and will not import OK")


def test_gil_interval_applied_at_startup():
    want = 0.005 if os.name == "nt" else 0.0005  # Windows measured 50-90% slower at 0.5 ms
    if not os.environ.get("LOC_TMS_GIL_SWITCH_INTERVAL_S") and want != config.GIL_SWITCH_INTERVAL_S:
        _fail(f"default switch interval must be {want}: {config.GIL_SWITCH_INTERVAL_S}")
    before = sys.getswitchinterval()
    sys.setswitchinterval(0.005)
    try:
        with TestClient(app):  # runs the startup events
            got = sys.getswitchinterval()
        if abs(got - config.GIL_SWITCH_INTERVAL_S) > 1e-9:
            _fail(f"startup must apply the configured switch interval, got {got}")
    finally:
        sys.setswitchinterval(before)
    print("  gil-interval: startup sets sys.setswitchinterval to 0.5 ms OK")


def main():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    test_csv_header_refused()
    test_raw_export_equals_json_export()
    test_raw_export_headers_and_auth()
    test_raw_export_cut_short_is_not_valid_tmx()
    test_gil_interval_applied_at_startup()
    print("BIG IMPORTS 615 OK")


if __name__ == "__main__":
    main()
