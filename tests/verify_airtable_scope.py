"""Airtable base/table scope on every caller-supplied override (security review 2026-10,
connectors-01 + connectors-02).

#268 made airtable_create_records / airtable_delete_records refuse a base/table override
that differs from the connection's configured base/table (_airtable_scope_target). The
same stored credential was still usable against ANY base/table through three other entry
points that took base/table from the caller:

  * dynamic_translate (POST /api/connectors/{cid}/airtable/webhook, CLI
    `connector airtable-translate`): READ a foreign table, persist its cells as TMS
    segments, and WRITE translated text back into it.
  * register_airtable_webhook (web + /api register, CLI `airtable-webhook register`):
    watch a foreign base with the connection's credential.
  * schema_fields (web browse/fields picker, CLI `connector browse --base --table`):
    disclose a foreign table's field names.

Each test drives a mismatched (or, for a connection with no configured base, any) override
and asserts it is REFUSED before a single request reaches the (mock) Airtable transport,
then pins that the in-scope call still works. Offline + hermetic: throwaway
LOC_TMS_DATA_DIR, provider keys blank (MockProvider), every Airtable call goes to an
httpx.MockTransport that records it.

Final line: "AIRTABLE SCOPE OK".
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_at_scope_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli import cmd_connector
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Role, User
from app.services import apikeys, security, settings
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from tests._csrf import csrf_for

_IN_BASE, _IN_TABLE = "appLOCALIZE", "Strings"
_FOREIGN_BASE, _FOREIGN_TABLE = "appFOREIGN", "Other"
_LOGIN_SECRET = "scope-pass-123456"  # pregate: allow-secret throwaway fixture login


def _tables():
    return {
        "tables": [
            {
                "id": "tblS",
                "name": _IN_TABLE,
                "fields": [
                    {"id": "fldSRC", "name": "Body", "type": "multilineText"},
                    {"id": "fldFR", "name": "Body (fr-FR)", "type": "multilineText"},
                ],
            },
            {
                "id": "tblO",
                "name": _FOREIGN_TABLE,
                "fields": [
                    {"id": "fldA", "name": "Body", "type": "multilineText"},
                    {"id": "fldB", "name": "Body (fr-FR)", "type": "multilineText"},
                ],
            },
        ]
    }


def _transport(calls):
    """Answers for ANY base, so a missing scope check is observable as recorded calls."""

    def handler(req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        calls.append((m, path))
        if m == "GET" and path.startswith("/v0/meta/bases/") and path.endswith("/tables"):
            return httpx.Response(200, json=_tables())
        if m == "POST" and path.startswith("/v0/bases/") and path.endswith("/webhooks"):
            return httpx.Response(
                200, json={"id": "achT", "macSecretBase64": "AAAA", "expirationTime": ""}
            )
        if m == "DELETE" and "/webhooks/" in path:
            return httpx.Response(200, json={})
        if m == "GET" and path.startswith("/v0/"):
            return httpx.Response(
                200, json={"records": [{"id": "recA", "fields": {"Body": "Hello world."}}]}
            )
        if m == "PATCH" and path.startswith("/v0/"):
            body = json.loads(req.content)
            return httpx.Response(200, json={"records": body["records"]})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def _mk_conn(name, base=_IN_BASE, table=_IN_TABLE):
    cfg = {"source_field": "Body", "naming_pattern": "{src} ({loc})", "source_locale": "en-US"}
    if base:
        cfg["base_id"] = base
    if table:
        cfg["table"] = table
    return cstore.create(
        "airtable", name, cfg, {"api_key": "patX"}, actor="admin"
    )  # pregate: allow-secret fake


def _foreign_calls(calls):
    return [c for c in calls if _FOREIGN_BASE in c[1] or f"/{_FOREIGN_TABLE}" in c[1]]


# ============================================================================
def test_store_dynamic_translate_scoped():
    conn = _mk_conn("dyn")
    for kw, what in (
        ({"base_id": _FOREIGN_BASE}, "base"),
        ({"table": _FOREIGN_TABLE}, "table"),
        ({"base_id": _FOREIGN_BASE, "table": _FOREIGN_TABLE}, "base+table"),
    ):
        calls = []
        ok, msg, _s = cstore.dynamic_translate(
            conn.id, record_ids=["recA"], transport=_transport(calls), **kw
        )
        assert not ok and "refused" in msg, (f"dynamic_translate foreign {what}", ok, msg)
        assert calls == [], (f"no Airtable call may run for a foreign {what}", calls)
    # in scope (override equal to config, and no override) still translates
    calls = []
    ok, msg, _s = cstore.dynamic_translate(
        conn.id, base_id=_IN_BASE, table=_IN_TABLE, record_ids=["recA"], transport=_transport(calls)
    )
    assert ok, ("in-scope dynamic_translate must still work", msg)
    assert any(m == "PATCH" for m, _p in calls) and not _foreign_calls(calls), calls
    calls = []
    ok, msg, _s = cstore.dynamic_translate(
        conn.id, record_ids=["recA"], transport=_transport(calls)
    )
    assert ok, ("config-only dynamic_translate must still work", msg)
    print("  1. dynamic_translate: foreign base/table refused before egress; in scope works")


def test_store_unconfigured_connection_fails_closed():
    conn = _mk_conn("blank", base="", table="")
    calls = []
    ok, msg, _s = cstore.dynamic_translate(
        conn.id,
        base_id=_FOREIGN_BASE,
        table=_FOREIGN_TABLE,
        record_ids=["recA"],
        transport=_transport(calls),
    )
    assert not ok and "refused" in msg and calls == [], ("blank-scope translate", msg, calls)
    raised = False
    try:
        cstore.schema_fields(conn.id, _FOREIGN_BASE, _FOREIGN_TABLE, transport=_transport(calls))
    except ConnectorError as e:
        raised = "refused" in str(e)
    assert raised and calls == [], ("blank-scope schema_fields", calls)
    settings.set_many({"app_base_url": "https://tms.example.com"})
    try:
        ok, msg = cstore.register_airtable_webhook(
            conn.id, base_id=_FOREIGN_BASE, table=_FOREIGN_TABLE, transport=_transport(calls)
        )
    finally:
        settings.set_many({"app_base_url": ""})
    assert not ok and "refused" in msg and calls == [], ("blank-scope register", msg, calls)
    print("  2. connection with no configured base/table: every override refused (fail closed)")


def test_store_schema_fields_scoped():
    conn = _mk_conn("schema")
    calls = []
    for base, table in ((_FOREIGN_BASE, _IN_TABLE), (_IN_BASE, _FOREIGN_TABLE)):
        raised = ""
        try:
            cstore.schema_fields(conn.id, base, table, transport=_transport(calls))
        except ConnectorError as e:
            raised = str(e)
        assert "refused" in raised, ("schema_fields foreign target", base, table, raised)
    assert calls == [], ("no meta call for a foreign target", calls)
    fields = cstore.schema_fields(conn.id, _IN_BASE, _IN_TABLE, transport=_transport(calls))
    assert fields == ["Body", "Body (fr-FR)"], fields
    print("  3. schema_fields: foreign base/table refused before egress; in scope lists fields")


def test_store_register_webhook_scoped():
    conn = _mk_conn("reg")
    settings.set_many({"app_base_url": "https://tms.example.com"})
    try:
        calls = []
        ok, msg = cstore.register_airtable_webhook(
            conn.id, base_id=_FOREIGN_BASE, transport=_transport(calls)
        )
        assert not ok and "refused" in msg and calls == [], ("register foreign", msg, calls)
        ok, msg = cstore.register_airtable_webhook(
            conn.id, table=_FOREIGN_TABLE, transport=_transport(calls)
        )
        assert not ok and "refused" in msg and calls == [], ("register foreign tbl", msg, calls)
        ok, msg = cstore.register_airtable_webhook(conn.id, transport=_transport(calls))
        assert ok, ("in-scope register must still work", msg)
        assert not _foreign_calls(calls), calls
    finally:
        settings.set_many({"app_base_url": ""})
    print("  4. register_airtable_webhook: foreign base/table refused; in scope registers")


# --- surfaces ----------------------------------------------------------------
def _user(email, role):
    with Session(engine) as s:
        u = User(email=email, name=email, role=role, active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _with_transport(fn, calls):
    def wrapped(*a, **kw):
        kw["transport"] = _transport(calls)
        return fn(*a, **kw)

    return wrapped


def test_api_and_web_routes_scoped():
    """A LINGUIST (can_tune_ai, not admin) on /api with a per-user key and on the web
    picker: every foreign override answers 400 and reaches no Airtable host."""
    conn = _mk_conn("routes")
    calls = []
    settings.set_many({"security_auth_required": "1", "app_base_url": "https://tms.example.com"})
    settings.invalidate_cache()
    patches = [
        mock.patch.object(cstore, name, _with_transport(getattr(cstore, name), calls))
        for name in ("dynamic_translate", "register_airtable_webhook", "schema_fields")
    ]
    for p in patches:
        p.start()
    try:
        uid = _user("ling-scope@x.test", Role.LINGUIST)
        _row, token = apikeys.create_key(uid, "scope-key")
        api = TestClient(app)
        h = {"X-Loc-Token": token}
        r = api.post(
            f"/api/connectors/{conn.id}/airtable/webhook",
            headers=h,
            json={
                "base_id": _FOREIGN_BASE,
                "table": _FOREIGN_TABLE,
                "source_field": "Body",
                "record_ids": ["recA"],
                "jobs": [{"target_field": "Body (fr-FR)", "locale": "fr-FR"}],
            },
        )
        assert r.status_code == 400 and "refused" in r.text, (r.status_code, r.text[:200])
        r = api.post(
            f"/api/connectors/{conn.id}/airtable/webhook/register",
            headers=h,
            json={"base_id": _FOREIGN_BASE, "table": _FOREIGN_TABLE},
        )
        assert r.status_code == 400 and "refused" in r.text, (r.status_code, r.text[:200])
        assert calls == [], ("/api foreign override reached Airtable", calls)
        # in scope still works on /api
        r = api.post(
            f"/api/connectors/{conn.id}/airtable/webhook",
            headers=h,
            json={"base_id": _IN_BASE, "table": _IN_TABLE, "record_ids": ["recA"]},
        )
        assert r.status_code == 200, (r.status_code, r.text[:200])
        calls.clear()

        security.create_user("ling-web@x.test", "ling-web@x.test", Role.LINGUIST, _LOGIN_SECRET)
        web = TestClient(app)
        csrf_for(web)
        r = web.post(
            "/auth/login",
            data={"email": "ling-web@x.test", "password": _LOGIN_SECRET, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(web)
        r = web.post(
            f"/connectors/connection/{conn.id}/browse/fields",
            data={"base": _FOREIGN_BASE, "table": _FOREIGN_TABLE},
        )
        assert r.status_code == 400 and r.json().get("ok") is False, (r.status_code, r.text[:200])
        assert "refused" in r.json().get("error", ""), r.text[:200]
        assert calls == [], ("web browse/fields foreign override reached Airtable", calls)
        r = web.post(
            f"/connectors/connection/{conn.id}/browse/fields",
            data={"base": _IN_BASE, "table": _IN_TABLE},
        )
        assert r.status_code == 200 and r.json()["fields"] == ["Body", "Body (fr-FR)"], r.text
    finally:
        for p in patches:
            p.stop()
        settings.set_many({"security_auth_required": "0", "app_base_url": ""})
        settings.invalidate_cache()
    print("  5. /api webhook + register and web browse/fields: LINGUIST foreign override 400")


def _cfg(cid):
    return dict(cstore.get(cid).config or {})


def test_config_retarget_needs_admin():
    """Residual of connectors-01: the configured base/table IS the scope, so moving it
    must not be a LINGUIST's way round the per-call check. /api update (no credential
    can ride it) refuses a LINGUIST base/table change; the web form allows it only when
    the same save enters a fresh credential; admins and other config edits unaffected."""
    conn = _mk_conn("retarget")
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        api = TestClient(app)
        ling = _user("ling-retarget@x.test", Role.LINGUIST)
        _row, ltok = apikeys.create_key(ling, "retarget-ling")
        admin = _user("admin-retarget@x.test", Role.ADMIN)
        _row, atok = apikeys.create_key(admin, "retarget-admin")
        for body in (
            {"config": {"base_id": _FOREIGN_BASE}},
            {"config": {"table": _FOREIGN_TABLE}},
        ):
            r = api.post(
                f"/api/connectors/{conn.id}/update", headers={"X-Loc-Token": ltok}, json=body
            )
            assert r.status_code == 403 and "Only an admin" in r.text, (r.status_code, r.text)
        assert _cfg(conn.id)["base_id"] == _IN_BASE and _cfg(conn.id)["table"] == _IN_TABLE
        # a LINGUIST still edits everything else (and may resend the same base)
        r = api.post(
            f"/api/connectors/{conn.id}/update",
            headers={"X-Loc-Token": ltok},
            json={"config": {"base_id": _IN_BASE, "naming_pattern": "{src} [{loc}]"}},
        )
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert _cfg(conn.id)["naming_pattern"] == "{src} [{loc}]"
        # an admin may retarget
        r = api.post(
            f"/api/connectors/{conn.id}/update",
            headers={"X-Loc-Token": atok},
            json={"config": {"base_id": "appADMINMOVED"}},
        )
        assert r.status_code == 200 and _cfg(conn.id)["base_id"] == "appADMINMOVED", r.text

        # web form: LINGUIST without a new credential is refused, with one is allowed
        security.create_user(
            "ling-rt-web@x.test", "ling-rt-web@x.test", Role.LINGUIST, _LOGIN_SECRET
        )
        web = TestClient(app)
        csrf_for(web)
        r = web.post(
            "/auth/login",
            data={"email": "ling-rt-web@x.test", "password": _LOGIN_SECRET, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        csrf_for(web)
        form = {
            "name": "retarget",
            "base_id": _FOREIGN_BASE,
            "table": _FOREIGN_TABLE,
            "source_field": "Body",
        }
        r = web.post(f"/connectors/connection/{conn.id}", data=form, follow_redirects=False)
        assert r.status_code == 303 and "ok=0" in r.headers["location"], r.headers
        assert _cfg(conn.id)["base_id"] == "appADMINMOVED", _cfg(conn.id)
        form["api_key"] = "patLINGUISTOWN"  # pregate: allow-secret fake fixture token
        r = web.post(f"/connectors/connection/{conn.id}", data=form, follow_redirects=False)
        assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers
        assert _cfg(conn.id)["base_id"] == _FOREIGN_BASE, _cfg(conn.id)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  6. config retarget: LINGUIST base/table move needs admin or own credential")


def test_oauth_retarget_junk_token_refused():
    """Gate finding (i419): on an OAuth-mode Airtable connection the effective credential
    is secret['oauth']['access_token']; a web-form api_key is MERGED beside it and never
    used. So a LINGUIST typing any junk PAT must NOT count as 'their own credential':
    the admin's OAuth token would otherwise be aimed at the LINGUIST's base."""
    conn = cstore.create(
        "airtable",
        "oauth-retarget",
        {"source_field": "Body", "base_id": _IN_BASE, "table": _IN_TABLE},
        {
            "auth_mode": "oauth",
            "oauth": {"access_token": "atkADMINOWNED"},  # pregate: allow-secret fake
        },
        actor="admin",
    )
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        security.create_user("ling-oauth@x.test", "ling-oauth@x.test", Role.LINGUIST, _LOGIN_SECRET)
        web = TestClient(app)
        csrf_for(web)
        r = web.post(
            "/auth/login",
            data={"email": "ling-oauth@x.test", "password": _LOGIN_SECRET, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        csrf_for(web)
        form = {
            "name": "oauth-retarget",
            "base_id": _FOREIGN_BASE,
            "table": _FOREIGN_TABLE,
            "source_field": "Body",
            "api_key": "patJUNK",  # pregate: allow-secret fake fixture token
        }
        r = web.post(f"/connectors/connection/{conn.id}", data=form, follow_redirects=False)
        assert r.status_code == 303 and "ok=0" in r.headers["location"], r.headers
        assert _cfg(conn.id)["base_id"] == _IN_BASE, _cfg(conn.id)
        # the refusal is audited
        from app.models import AuditLog

        with Session(engine) as s:
            rows = [
                a
                for a in s.query(AuditLog).all()
                if a.action == "connector.retarget_refused" and a.target == f"connection:{conn.id}"
            ]
        assert rows, "retarget refusal must write an audit row"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  8. OAuth connection: a LINGUIST junk PAT does not unlock a base/table move")


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue() + err.getvalue()


def test_cli_scoped():
    conn = _mk_conn("cli")
    calls = []
    cmd_connector._set_transport(_transport(calls))
    try:
        rc, out = _run_cli(
            ["connector", "airtable-translate", str(conn.id), "--base", _FOREIGN_BASE]
        )
        assert rc != 0 and "refused" in out, (rc, out[:300])
        rc, out = _run_cli(
            [
                "connector",
                "browse",
                str(conn.id),
                "--base",
                _FOREIGN_BASE,
                "--table",
                _FOREIGN_TABLE,
            ]
        )
        assert rc != 0 and "refused" in out, (rc, out[:300])
        assert calls == [], ("CLI foreign override reached Airtable", calls)
    finally:
        cmd_connector._set_transport(None)
    print("  7. CLI airtable-translate / browse: foreign override refused")


def main():
    init_db()
    test_store_dynamic_translate_scoped()
    test_store_unconfigured_connection_fails_closed()
    test_store_schema_fields_scoped()
    test_store_register_webhook_scoped()
    test_api_and_web_routes_scoped()
    test_config_retarget_needs_admin()
    test_cli_scoped()
    test_oauth_retarget_junk_token_refused()
    print("AIRTABLE SCOPE OK")


if __name__ == "__main__":
    main()
