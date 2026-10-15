"""Issue #567: a manual TM edit applies the placeholder/tag integrity gate segment approve applies.

tm_module.edit_entry stored any non-hedge target in place, so a linguist (or an API client)
could drop {count} from a TM target and it kept matching at 100%. The edit now runs the
same check as approve (#562): tm_common.integrity_skip over (entry source, new target).

Policy under test:
  * placeholder drop/add -> REFUSED (TMEditIntegrityRefused, code placeholder_integrity)
    unless acknowledge_placeholder_loss=True; the acknowledged edit is audited;
  * inline-tag breakage / a tagged source -> REFUSED always (code tag_integrity /
    tagged_source), the acknowledgement does not apply;
  * a clean edit, a reorder of placeholders and an identical (no-op) target are unchanged;
  * surfaces map it to a clear 4xx: /api 400 with the code (string flag is 400), web redirect
    with the message and a form checkbox, CLI exit non-zero with --acknowledge-placeholder-loss,
    MCP manage_tm(action=edit) forwards the flag only for a real boolean true.

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII only. Final line: "TM EDIT INTEGRITY 567 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmedit567_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Project, Role, SegmentTM
from app.services import apikeys, security, settings, tm, tm_module
from tests._cli_runner import run_cli

init_db()
_L, _R = "⟦", "⟧"
client = TestClient(app)


def _tok(name):
    return _L + name + _R


def _fail(msg):
    print("FAIL:", ascii(msg))
    sys.exit(1)


def _entry(source, target):
    """A TM row written the way import/approve would for a CLEAN pair (direct insert so the
    tagged-source fixture can exist at all: write_back refuses it)."""
    with Session(engine) as s:
        p = Project(name="edit567-" + source[:8], source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        row = SegmentTM(
            project_id=p.id,
            source_locale="en-US",
            target_locale="es-ES",
            source_text=source,
            target_text=target,
            source_hash=tm.hash_text(source, "en-US", "es-ES"),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _target(eid):
    with Session(engine) as s:
        return s.get(SegmentTM, eid).target_text


def test_service():
    eid = _entry("You have {count} items", "Tienes {count} articulos")
    # Resolve the refusal class lazily so this section fails BEHAVIORALLY on a pre-#567 tree
    # (the dropped-placeholder edit saves there) rather than on a missing attribute.
    refused = getattr(tm_module, "TMEditIntegrityRefused", tm_module.TMEditRefused)
    with Session(engine) as s:
        try:
            tm_module.edit_entry(s, eid, "Tienes articulos")
            _fail("a dropped placeholder must be refused")
        except refused as exc:
            if exc.code != "placeholder_integrity" or "placeholder" not in str(exc):
                _fail(f"refusal must carry the code and a reason: {exc.code} {exc}")
        if not issubclass(tm_module.TMEditIntegrityRefused, tm_module.TMEditRefused):
            _fail("must stay a TMEditRefused so existing surfaces map it to a clean 4xx")
        try:
            tm_module.edit_entry(s, eid, "Tienes {count} y {extra} articulos")
            _fail("an added placeholder must be refused")
        except tm_module.TMEditIntegrityRefused:
            pass
    if _target(eid) != "Tienes {count} articulos":
        _fail("a refused edit stores nothing")
    with Session(engine) as s:
        tm_module.edit_entry(s, eid, "Hay {count} articulos aqui")  # reorder-safe, clean
        tm_module.edit_entry(s, eid, "Hay {count} articulos aqui")  # no-op
    if _target(eid) != "Hay {count} articulos aqui":
        _fail("a placeholder-clean edit still saves")
    with Session(engine) as s:
        tm_module.edit_entry(s, eid, "Hay articulos", acknowledge_placeholder_loss=True, actor="t")
        rows = [
            r.detail for r in s.exec(select(AuditLog).where(AuditLog.action == "tm.edit")).all()
        ]
    if _target(eid) != "Hay articulos":
        _fail("an acknowledged placeholder loss saves")
    if not any("placeholder_loss_acknowledged" in d for d in rows):
        _fail(f"an acknowledged loss is audited: {rows}")
    print("  1. service: drop/add refused with code, clean saves, acknowledged saves + audit OK")


def test_tags_never_acknowledgeable():
    src = "Click " + _tok("g1") + "here" + _tok("/g1")
    eid = _entry(src, "Haga clic " + _tok("g1") + "aqui" + _tok("/g1"))
    with Session(engine) as s:
        try:
            tm_module.edit_entry(s, eid, "Haga clic aqui", acknowledge_placeholder_loss=True)
            _fail("a tagged-source entry is never editable into TM")
        except tm_module.TMEditIntegrityRefused as exc:
            if exc.code != "tagged_source":
                _fail(exc.code)
    eid2 = _entry("Plain tag text", "Texto plano")
    with Session(engine) as s:
        try:
            tm_module.edit_entry(
                s, eid2, "Texto " + _tok("g9") + "plano", acknowledge_placeholder_loss=True
            )
            _fail("adding inline tags is refused even when acknowledged")
        except tm_module.TMEditIntegrityRefused as exc:
            if exc.code != "tag_integrity":
                _fail(exc.code)
    print("  2. tags: tagged source and stray tokens refused even when acknowledged OK")


def test_api():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        user = security.create_user("edit567@x.com", "E", Role.ADMIN, "edit567-password-123")
        _row, token = apikeys.create_key(user.id, "edit567", scopes="api docs")
        h = {"X-Loc-Token": token}
        eid = _entry("Hello {name}", "Hola {name}")
        r = client.post(f"/api/tm/{eid}/edit", headers=h, json={"target": "Hola"})
        j = r.json()
        if r.status_code != 400 or j.get("code") != "placeholder_integrity" or not j.get("error"):
            _fail(f"/api edit must 400 with the code: {r.status_code} {j}")
        r = client.post(
            f"/api/tm/{eid}/edit",
            headers=h,
            json={"target": "Hola", "acknowledge_placeholder_loss": "true"},
        )
        if r.status_code != 400 or "boolean" not in r.json().get("error", ""):
            _fail(f"a non-boolean flag is a 400: {r.status_code} {r.text}")
        if _target(eid) != "Hola {name}":
            _fail("nothing stored by a refused edit")
        r = client.post(
            f"/api/tm/{eid}/edit",
            headers=h,
            json={"target": "Hola", "acknowledge_placeholder_loss": True},
        )
        if r.status_code != 200 or _target(eid) != "Hola":
            _fail(f"/api acknowledged edit: {r.status_code} {r.text}")
        eid2 = _entry("Plain api text", "Texto plano api")
        r = client.post(
            f"/api/tm/{eid2}/edit",
            headers=h,
            json={"target": "Texto " + _tok("g1"), "acknowledge_placeholder_loss": True},
        )
        if r.status_code != 400 or r.json().get("code") != "tag_integrity":
            _fail(f"/api tag breakage is never acknowledgeable: {r.status_code} {r.text}")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  3. /api: 400 + code, string flag 400, boolean flag saves, tags still 400 OK")


def test_web():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    eid = _entry("Web {name} text", "Texto {name} web")
    r = client.post(f"/tm/{eid}/edit", data={"target": "Texto web"}, follow_redirects=False)
    loc = r.headers.get("location", "")
    if r.status_code != 303 or "err=" not in loc or "placeholder_integrity" not in loc:
        _fail(f"web edit must redirect with the refusal: {r.status_code} {loc}")
    page = client.get(loc).text
    if 'name="acknowledge_placeholder_loss"' not in page:
        _fail("the TM entry page must offer the acknowledge checkbox")
    if _target(eid) != "Texto {name} web":
        _fail("refused web edit stores nothing")
    r = client.post(
        f"/tm/{eid}/edit",
        data={"target": "Texto web", "acknowledge_placeholder_loss": "1"},
        follow_redirects=False,
    )
    if "edited=1" not in r.headers.get("location", "") or _target(eid) != "Texto web":
        _fail(f"web acknowledged edit: {r.headers.get('location')}")
    print("  4. web: refusal message + checkbox, acknowledged edit saves OK")


def test_cli():
    eid = _entry("Cli {name} text", "Texto {name} cli")
    code, out, err = run_cli(["tm", "edit", str(eid), "--target", "Texto cli"])
    text = out + err
    if code == 0 or "placeholder_integrity" not in text:
        _fail(f"CLI edit must fail and name the code: rc={code} {text}")
    if _target(eid) != "Texto {name} cli":
        _fail("refused CLI edit stores nothing")
    code, out, err = run_cli(
        ["tm", "edit", str(eid), "--target", "Texto cli", "--acknowledge-placeholder-loss"]
    )
    if code != 0 or _target(eid) != "Texto cli":
        _fail(f"CLI acknowledged edit: rc={code} {out}{err}")
    print("  5. CLI: refused with the code, --acknowledge-placeholder-loss saves OK")


def test_mcp():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import asyncio

    import server

    sent = []

    async def fake_post(client_, path, body, *a, **k):
        sent.append(body)
        return {"ok": True}

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    saved, saved_c = server._post_json, server._client
    server._post_json, server._client = fake_post, (lambda: _Ctx())
    try:
        for ack in (False, True, "yes"):
            asyncio.run(
                server.manage_tm(
                    "edit", entry_id=1, target_text="x", acknowledge_placeholder_loss=ack
                )
            )
    finally:
        server._post_json, server._client = saved, saved_c
    flags = [b.get("acknowledge_placeholder_loss") for b in sent]
    if flags != [None, True, None]:
        _fail(f"MCP edit forwards the flag only for a real boolean true: {flags}")
    print("  6. MCP: manage_tm edit forwards the flag only when true OK")


def main():
    test_service()
    test_tags_never_acknowledgeable()
    test_api()
    test_web()
    test_cli()
    test_mcp()
    print("TM EDIT INTEGRITY 567 OK")


if __name__ == "__main__":
    main()
