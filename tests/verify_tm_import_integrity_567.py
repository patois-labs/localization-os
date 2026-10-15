"""Issue #567: TMX import applies the placeholder/tag integrity gate segment approve applies.

#562 made segment approve withhold a TM write whose target drops or adds a placeholder
({0}, %s, {{name}}) unless acknowledged, and never write a pair with damaged or present
inline tags. TMX import (tm.import_tmx) wrote targets with no such check, so the same broken
target could enter TM through a file and be reused as a 100% match.

Policy under test (the ONE detector: tm_common.integrity_skip -> quality.placeholder_violation,
quality.tag_damage, tm.is_tm_eligible; same codes as approve):
  * a unit whose target breaks placeholders is WITHHELD (code placeholder_integrity), unless
    acknowledge_placeholder_loss=True;
  * a unit with inline tags in the source (tagged_source) or damaged tags (tag_integrity) is
    ALWAYS withheld, acknowledgement or not;
  * the import keeps going: the other units import, the result carries skipped_integrity,
    skipped_by_code and the first TMX_IMPORT_REPORT_ROWS rows {row, code, reason, source};
  * an existing TM row is never updated to a withheld target;
  * the #571/#576 caps and chunked flush are unchanged (more than 20 withheld units: exact
    counts, bounded row list);
  * surfaces: /api (JSON boolean flag, string is 400), web form field + redirect counters,
    CLI (--acknowledge-placeholder-loss, rows listed), MCP schema and handler.

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII only (inline-tag brackets built from escapes).
Final line: "TM IMPORT INTEGRITY 567 OK".
"""

import base64
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_tmimp567_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Project, Role, SegmentTM, TargetLocale
from app.services import apikeys, security, settings, tm
from tests._cli_runner import run_cli

init_db()
_SRC, _TGT = "en-US", "es-ES"
_L, _R = "⟦", "⟧"  # inline-fidelity token brackets


def _tok(name):
    return _L + name + _R


client = TestClient(app)


def _fail(msg):
    print("FAIL:", ascii(msg))
    sys.exit(1)


def _project(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale=_SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        s.commit()
        return p.id


def _tmx(pairs) -> bytes:
    def esc(t):
        return t.replace("&", "&amp;").replace("<", "&lt;")

    tus = "".join(
        f'<tu><tuv xml:lang="{_SRC}"><seg>{esc(s)}</seg></tuv>'
        f'<tuv xml:lang="{_TGT}"><seg>{esc(t)}</seg></tuv></tu>'
        for s, t in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<tmx version="1.4"><header srclang="{_SRC}"/><body>{tus}</body></tmx>'
    ).encode()


_CLEAN = ("Welcome back", "Bienvenido de nuevo")
_PH_OK = ("You have {count} items", "Tienes {count} articulos")
_PH_DROP = ("Hello {name}", "Hola")
_PH_ADD = ("Plain greeting", "Saludo {extra}")
_PCT_DROP = ("Value is %s today", "El valor es hoy")
_TAGGED_SRC = ("Click " + _tok("g1") + "here" + _tok("/g1"), "Haga clic " + _tok("g1") + "aqui")
_TAG_ADDED = ("Plain tag text", "Texto " + _tok("g9") + "plano")
_MIXED = [_CLEAN, _PH_DROP, _PH_OK, _PH_ADD, _PCT_DROP, _TAGGED_SRC, _TAG_ADDED]


def _stored(pid):
    with Session(engine) as s:
        return {
            r.source_text: r.target_text
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
        }


def _import(pid, pairs, **kw):
    with Session(engine) as s:
        return tm.import_tmx(s, pid, _tmx(pairs), _SRC, _TGT, **kw)


def test_service_gate():
    pid = _project("imp567-svc")
    res = _import(pid, _MIXED)
    stored = _stored(pid)
    if set(stored) != {_CLEAN[0], _PH_OK[0]}:
        _fail(f"only the clean units may import by default: {sorted(stored)}")
    if res["imported"] != 2 or res["skipped_integrity"] != 5:
        _fail(f"counts: {res}")
    codes = res["skipped_by_code"]
    if codes != {"placeholder_integrity": 3, "tagged_source": 1, "tag_integrity": 1}:
        _fail(f"codes: {codes}")
    rows = res["skipped_rows"]
    if len(rows) != 5 or {r["code"] for r in rows} != set(codes):
        _fail(f"row report: {rows}")
    first = rows[0]
    if first["row"] != 2 or first["source"] != _PH_DROP[0] or "placeholder" not in first["reason"]:
        _fail(f"row report must name the unit (1-based <tu> index), source and reason: {first}")
    if res["acknowledged_placeholder_loss"] != 0:
        _fail(res)
    print("  1. service: broken placeholders/tags withheld with codes, clean units import OK")


def test_acknowledge():
    pid = _project("imp567-ack")
    res = _import(pid, _MIXED, acknowledge_placeholder_loss=True)
    stored = _stored(pid)
    want = {_CLEAN[0], _PH_OK[0], _PH_DROP[0], _PH_ADD[0], _PCT_DROP[0]}
    if set(stored) != want:
        _fail(f"acknowledged placeholder losses import, tags never: {sorted(stored)}")
    if res["skipped_by_code"] != {"tagged_source": 1, "tag_integrity": 1}:
        _fail(f"tag breakage is never acknowledgeable: {res['skipped_by_code']}")
    if res["acknowledged_placeholder_loss"] != 3 or res["imported"] != 5:
        _fail(res)
    print("  2. acknowledge: placeholder losses import, tag breakage still withheld OK")


def test_existing_row_not_updated():
    pid = _project("imp567-upd")
    _import(pid, [("Hello {name}", "Hola {name}")])
    res = _import(pid, [("Hello {name}", "Hola")])
    if _stored(pid)["Hello {name}"] != "Hola {name}":
        _fail("a withheld target must never overwrite an existing good TM row")
    if res["updated"] != 0 or res["skipped_integrity"] != 1:
        _fail(res)
    print("  3. an existing row is not updated to a withheld target OK")


def test_report_bounded_counts_exact():
    pid = _project("imp567-cap")
    n = tm.TMX_IMPORT_REPORT_ROWS + 15
    pairs = [(f"Item {i} {{n}}", f"Elemento {i}") for i in range(n)] + [_CLEAN]
    res = _import(pid, pairs)
    if res["skipped_integrity"] != n or res["skipped_by_code"] != {"placeholder_integrity": n}:
        _fail(f"counts must be exact beyond the report cap: {res['skipped_integrity']}")
    if len(res["skipped_rows"]) != tm.TMX_IMPORT_REPORT_ROWS:
        _fail(f"row list is bounded: {len(res['skipped_rows'])}")
    if res["imported"] != 1:
        _fail(res)
    # chunked flush boundary still fine with withheld units interleaved
    saved = tm.TMX_IMPORT_FLUSH_BATCH
    tm.TMX_IMPORT_FLUSH_BATCH = 7
    try:
        pid2 = _project("imp567-chunk")
        r2 = _import(pid2, pairs)
    finally:
        tm.TMX_IMPORT_FLUSH_BATCH = saved
    if r2["imported"] != 1 or r2["skipped_integrity"] != n or len(_stored(pid2)) != 1:
        _fail(r2)
    print("  4. row report bounded, counts exact, chunked flush unaffected OK")


def _admin_token():
    user = security.create_user("imp567-admin@x.com", "Imp", Role.ADMIN, "imp567-password-123")
    _row, token = apikeys.create_key(user.id, "imp567", scopes="api docs")
    return token


def test_api():
    token = _admin_token()
    h = {"X-Loc-Token": token}
    pid = _project("imp567-api")
    b64 = base64.b64encode(_tmx(_MIXED)).decode()
    body = {"tmx_b64": b64, "source": _SRC, "target": _TGT}
    r = client.post(f"/api/projects/{pid}/tm/import", headers=h, json=body)
    j = r.json()
    if r.status_code != 200 or j.get("skipped_integrity") != 5 or j.get("imported") != 2:
        _fail(f"/api import must report withheld units, not fail: {r.status_code} {j}")
    if not j.get("skipped_rows") or j["skipped_by_code"].get("placeholder_integrity") != 3:
        _fail(f"/api must carry codes and rows: {j}")
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        headers=h,
        json={**body, "acknowledge_placeholder_loss": "true"},
    )
    if r.status_code != 400:
        _fail(f"a non-boolean acknowledge flag must be a 400: {r.status_code} {r.text}")
    r = client.post(
        f"/api/projects/{pid}/tm/import",
        headers=h,
        json={**body, "acknowledge_placeholder_loss": True},
    )
    j = r.json()
    if r.status_code != 200 or j.get("acknowledged_placeholder_loss") != 3:
        _fail(f"/api acknowledged import: {r.status_code} {j}")
    print("  5. /api: report, string flag 400, boolean flag imports OK")


def test_web():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    pid = _project("imp567-web")
    files = {"file": ("m.tmx", _tmx(_MIXED), "application/xml")}
    form = {"source_locale": _SRC, "target_locale": _TGT}
    r = client.post(f"/projects/{pid}/tm/import", data=form, files=files, follow_redirects=False)
    loc = r.headers.get("location", "")
    if r.status_code != 303 or "tm_skipped_integrity=5" not in loc or "tm_imported=2" not in loc:
        _fail(f"web import must report withheld units: {r.status_code} {loc}")
    page = client.get(loc).text
    if "with broken placeholders or inline tags" not in page:
        _fail("project page must say units were withheld")
    pid2 = _project("imp567-web2")
    r = client.post(
        f"/projects/{pid2}/tm/import",
        data={**form, "acknowledge_placeholder_loss": "1"},
        files={"file": ("m.tmx", _tmx(_MIXED), "application/xml")},
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    if "tm_skipped_integrity=2" not in loc or "tm_ack_loss=3" not in loc:
        _fail(f"web acknowledged import: {loc}")
    print("  6. web: counters in the redirect, acknowledge field imports OK")


def test_cli():
    pid = _project("imp567-cli")
    path = os.path.join(_TMP, "m.tmx")
    with open(path, "wb") as fh:
        fh.write(_tmx(_MIXED))
    code, out, err = run_cli(["tm", "import", str(pid), path, "--source", _SRC, "--target", _TGT])
    text = out + err
    if code != 0 or "withheld 5" not in text or "[placeholder_integrity]" not in text:
        _fail(f"CLI must list the withheld units: rc={code} {text}")
    if "--acknowledge-placeholder-loss" not in text:
        _fail(f"CLI must say how to acknowledge: {text}")
    if not text.isascii():
        _fail("CLI output must be ASCII")
    code, out, _ = run_cli(
        [
            "--json",
            "tm",
            "import",
            str(pid),
            path,
            "--source",
            _SRC,
            "--target",
            _TGT,
            "--acknowledge-placeholder-loss",
        ]
    )
    j = json.loads(out.strip().splitlines()[-1])
    if code != 0 or j.get("acknowledged_placeholder_loss") != 3 or j.get("skipped_integrity") != 2:
        _fail(f"CLI --acknowledge-placeholder-loss: rc={code} {j}")
    print("  7. CLI: withheld rows listed, --acknowledge-placeholder-loss imports OK")


def test_mcp():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import registry

    tool = next((t for t in registry.TOOLS if t.name == "manage_tm"), None)
    props = tool.input_schema.get("properties", {}) if tool else {}
    if props.get("acknowledge_placeholder_loss", {}).get("type") != "boolean":
        _fail(f"manage_tm must expose acknowledge_placeholder_loss: {sorted(props)}")
    import server

    sent = []

    async def fake_post(client, path, body, *a, **k):
        sent.append((path, body))
        return {"ok": True}

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    import asyncio

    server._post_json, saved = fake_post, server._post_json
    server._client, saved_c = (lambda: _Ctx()), server._client
    try:
        asyncio.run(
            server.manage_tm(
                "import", project_id=1, data="<tmx/>", source_locale="a", target_locale="b"
            )
        )
        asyncio.run(
            server.manage_tm(
                "import",
                project_id=1,
                data="<tmx/>",
                source_locale="a",
                target_locale="b",
                acknowledge_placeholder_loss=True,
            )
        )
        asyncio.run(
            server.manage_tm(
                "import",
                project_id=1,
                data="<tmx/>",
                source_locale="a",
                target_locale="b",
                acknowledge_placeholder_loss="yes",
            )
        )
    finally:
        server._post_json = saved
        server._client = saved_c
    flags = [b.get("acknowledge_placeholder_loss") for _p, b in sent]
    if flags != [None, True, None]:
        _fail(f"MCP forwards the flag only for a real boolean true: {flags}")
    print("  8. MCP: schema + handler forward the flag only when true OK")


def main():
    test_service_gate()
    test_acknowledge()
    test_existing_row_not_updated()
    test_report_bounded_counts_exact()
    test_api()
    test_web()
    test_cli()
    test_mcp()
    print("TM IMPORT INTEGRITY 567 OK")


if __name__ == "__main__":
    main()
