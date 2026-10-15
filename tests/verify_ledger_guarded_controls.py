"""Guarded Ledger controls, exercised through the suite runner."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_ledger_guard_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role, TapAction, TapRun, TapStatus
from app.services import (
    apikeys,
    audit,
    ledger_controls,
    prompt_tap,
    security,
    settings,
    tap_envelope,
)
from tests._csrf import csrf_for

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def refused(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("ceremony accepted an invalid phrase")


def _save_settings(*keys):
    """Snapshot the given setting values so a test restores EXACTLY what it found rather than a
    hardcoded default. This suite shares ONE DB and main() runs in list order, so a test that
    leaves shared settings behind is latent ordering fragility (gate round-3 LOW-4)."""
    return {k: settings.get(k) for k in keys}


def _restore_settings(saved):
    settings.set_many({k: v for k, v in saved.items() if v is not None})


def _admin_detail():
    """The administrator health detail map. Since #270 the anonymous /health carries no
    detail; /admin/health serves it from the DB-free _health_details builder."""
    from starlette.requests import Request

    from app.web.health import _health_details

    scope = {"type": "http", "method": "GET", "path": "/admin/health", "headers": []}
    return json.loads(_health_details(Request(scope)).body)["detail"]


def test_generic_setting_refused():
    try:
        settings.validate_value("prompt_tap_envelopes_enabled", "0")
    except settings.InvalidSettingValue:
        pass
    else:
        raise AssertionError("generic envelope setting write must be refused")


def test_surfaces():
    settings.set_many({"remote_admin_enabled": "1", "security_auth_required": "0"})
    user = security.create_user(
        "ledger-admin@example.com", "Ledger admin", Role.ADMIN, "ledger-password-123"
    )
    _, key = apikeys.create_key(user.id, "ledger", scopes="api docs admin")
    headers = {"X-Loc-Token": key}
    with TestClient(app) as client:
        csrf_for(client)
        for phrase in ("", "wrong", "disable envelope recording "):
            r = client.post("/prompt-tap/envelopes/off", data={"confirm": phrase})
            assert r.status_code == 400, r.text
            r = client.post(
                "/api/admin/ledger/envelopes/off", json={"confirm": phrase}, headers=headers
            )
            assert r.status_code == 400, r.text
            assert cli("ledger", "envelopes", "off", "--confirm", phrase).returncode != 0
        r = client.post("/settings", data={"org_name": "test", "prompt_tap_envelopes_enabled": "0"})
        assert r.status_code == 400, r.text
        r = client.post(
            "/api/admin/config",
            json={"settings": {"prompt_tap_envelopes_enabled": "0"}, "confirm": True},
            headers=headers,
        )
        assert r.status_code == 400, r.text
        assert cli("config", "set", "prompt_tap_envelopes_enabled", "0", "--yes").returncode != 0
        # MCP manage_install delegates to this exact /api/admin/config path.
        mcp = (ROOT / "integrations/mcp-server/server.py").read_text(encoding="utf-8")
        assert '"/api/admin/config"' in mcp
        r = client.post(
            "/prompt-tap/envelopes/off",
            data={"confirm": ledger_controls.DISABLE_PHRASE},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert _admin_detail().get("ledger_envelopes") == "off"
        for url in ("/prompt-tap", "/prompt-tap/runs"):
            assert "envelope recording is OFF" in client.get(url).text
        r = client.post("/api/admin/ledger/envelopes/on", json={}, headers=headers)
        assert r.status_code == 200, r.text
        assert "envelope recording is OFF" not in client.get("/prompt-tap").text
        assert "ledger_envelopes" not in _admin_detail()
        settings.set_many({"remote_admin_enabled": "0"})
        # MED-2: the WEB ceremony no longer depends on remote_admin_enabled -> a wrong phrase
        # REACHES the handler (400), never the old bare 404 (full default-install coverage is
        # in test_web_ceremony_default_install). A wrong phrase leaves envelope state ON.
        assert (
            client.post("/prompt-tap/envelopes/off", data={"confirm": "wrong"}).status_code == 400
        )
        # The /api twin KEEPS the R3 remote lock -> 404 while the switch is off.
        assert client.post("/api/admin/ledger/purge", json={}, headers=headers).status_code == 404
        settings.set_many({"remote_admin_enabled": "1"})
        for url in ("/prompt-tap/purge", "/api/admin/ledger/purge"):
            kwargs = (
                {"data": {"before": "2020-01-01", "confirm": "purge envelopes before 2019-01-01"}}
                if url.startswith("/prompt")
                else {"json": {"before": "2020-01-01", "confirm": "wrong"}, "headers": headers}
            )
            assert client.post(url, **kwargs).status_code == 400
        body = client.get("/api/prompt-tap/actions", headers=headers).json()
        assert body["ledger_status"]["envelopes_enabled"] is True
    for verb, extra in (("off", ["--confirm", ledger_controls.DISABLE_PHRASE]), ("on", [])):
        out = cli("ledger", "envelopes", verb, *extra)
        assert out.returncode == 0, out.stderr
    with Session(engine) as session:
        actions = session.exec(select(AuditLog.action)).all()
        assert "ledger.envelopes.disable" in actions and "ledger.envelopes.enable" in actions


def seed(status=TapStatus.COMPLETED, finished=datetime(2019, 1, 1), child_status=None):
    aid, rid = tap_envelope.new_id(), tap_envelope.new_id()
    with Session(engine) as session:
        session.add(TapAction(id=aid, status=status, finished_at=finished))
        session.commit()
        session.add(
            TapRun(id=rid, action_id=aid, status=child_status or status, finished_at=finished)
        )
        session.commit()
    prompt_tap._actions_dir().mkdir(parents=True, exist_ok=True)
    prompt_tap._action_path(aid).write_text('{"request": {}, "meta": {}}\n', encoding="utf-8")
    return aid


def test_purge_and_rollback():
    old = [seed(), seed()]
    keep = [
        seed(TapStatus.RUNNING),
        seed(TapStatus.PAUSED),
        seed(finished=datetime(2020, 1, 1)),
        seed(child_status=TapStatus.PAUSED),
        seed(finished=None),
    ]
    args = {"before": "2020-01-01", "confirm": "purge envelopes before 2020-01-01"}
    refused(lambda: ledger_controls.purge(before=args["before"]))
    refused(
        lambda: ledger_controls.purge(before="20200101", confirm="purge envelopes before 20200101")
    )
    result = ledger_controls.purge(**args, batch_size=1)
    assert (result["actions"], result["runs"], result["raw_files"]) == (2, 2, 2), result
    out = cli("ledger", "purge", "--before", args["before"], "--confirm", args["confirm"])
    assert out.returncode == 0 and "'actions': 2" in out.stdout, (out.stdout, out.stderr)
    assert (
        cli(
            "ledger", "purge", "--before", args["before"], "--confirm", "wrong", "--apply"
        ).returncode
        != 0
    )
    with patch.object(
        ledger_controls, "_audit_row", side_effect=RuntimeError("injected batch failure")
    ):
        try:
            ledger_controls.purge(**args, apply=True, batch_size=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("batch failure missing")
    with Session(engine) as session:
        for aid in old + keep:
            assert session.get(TapAction, aid) is not None
            assert prompt_tap._action_path(aid).exists()
    result = ledger_controls.purge(**args, apply=True, batch_size=1, actor="purge-test")
    assert result["actions"] == 2
    with Session(engine) as session:
        for aid in old:
            assert session.get(TapAction, aid) is None
            assert not prompt_tap._action_path(aid).exists()
        for aid in keep:
            assert session.get(TapAction, aid) is not None
        rows = session.exec(
            select(AuditLog).where(AuditLog.action == "ledger.envelopes.purge")
        ).all()
        assert len(rows) == 2 and all(row.actor == "purge-test" for row in rows)


def test_raw_notice_and_help():
    settings.set_many({"prompt_tap_enabled": "0"})
    st = ledger_controls.status()
    assert st["retained_raw_files"] >= 5 and st["oldest_raw_date"]
    # MED-1: the render path serves a CACHED scan; drop the cache so the page reflects the
    # files this test relies on (an earlier test's cached render may hold a stale count).
    ledger_controls.invalidate_scan_cache()
    with TestClient(app) as client:
        page = client.get("/prompt-tap").text
        assert f"{st['retained_raw_files']} retained raw files" in page
        assert st["oldest_raw_date"] in page and "FROZEN" in page
    out = cli("prompt-tap", "--help")
    assert "stops all writes" not in out.stdout and "FROZEN" in out.stdout
    # LOW-4: the FULL canonical block lives in ONE doc (the Ledger owner, connectors.md); the
    # other three carry a pointer to it plus their surface-specific line, so a future
    # correction is made once, not four times (and the ratchet cannot pass on stale copies).
    canonical = (ROOT / "docs/architecture/connectors.md").read_text(encoding="utf-8")
    assert (
        "disable envelope recording" in canonical
        and "purge envelopes before" in canonical
        and "FROZEN" in canonical
        and "reconcile_staged_purges" in canonical
    ), "connectors.md must hold the canonical guarded-controls block"
    pointer = "docs/architecture/connectors.md"
    for name, surface in (
        ("docs/CLI.md", 'ledger envelopes off --confirm "disable envelope recording"'),
        ("app/capabilities/llm.md", "ledger_summary"),
        ("app/templates/help.html", "disable envelope recording"),
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert pointer in text and surface in text, name


def test_audit_failure_is_atomic_and_chain_valid():
    ledger_controls.set_envelopes(True)
    with patch.object(ledger_controls, "_audit_row", side_effect=RuntimeError("audit failed")):
        try:
            ledger_controls.set_envelopes(False, confirm=ledger_controls.DISABLE_PHRASE)
        except RuntimeError:
            pass
        else:
            raise AssertionError("audit failure did not abort the toggle")
    assert tap_envelope.envelopes_enabled()
    result = audit.verify_chain()
    assert result[0], result


def test_cli_and_web_apply():
    args = {"before": "2020-01-01", "confirm": "purge envelopes before 2020-01-01"}
    aid = seed()
    out = cli(
        "ledger", "purge", "--before", args["before"], "--confirm", args["confirm"], "--apply"
    )
    assert out.returncode == 0, out.stderr
    with Session(engine) as session:
        assert session.get(TapAction, aid) is None
    aid = seed()
    with TestClient(app) as client:
        csrf_for(client)
        # LOW-1: the web twin answers the browser form with the normal page flow (redirect +
        # flash), not a bare JSON document. LOW-3: the flash rides a SIGNED, one-shot SESSION
        # value, so the redirect location is the bare page (no forgeable counts on the query).
        result = client.post("/prompt-tap/purge", data=args, follow_redirects=False)
        assert result.status_code == 303, result.text
        loc = result.headers["location"]
        assert loc == "/prompt-tap" and "purged" not in loc, loc
        assert prompt_tap._action_path(aid).exists()  # dry-run kept the file
        # The banner renders ONCE (from the session), then is popped: a second GET is clean.
        page = client.get("/prompt-tap").text
        assert "Purge preview" in page and "1 actions" in page, "flash not rendered from session"
        assert "Purge preview" not in client.get("/prompt-tap").text, "flash was not one-shot"
        # LOW-3: a crafted URL cannot forge the confirmation banner (it is not read from the query).
        forged = client.get("/prompt-tap?purged=applied&purge_actions=999999").text
        assert "Purge applied" not in forged and "999999" not in forged, "URL forged the flash"
        result = client.post(
            "/prompt-tap/purge", data={**args, "apply": "1"}, follow_redirects=False
        )
        assert result.status_code == 303, result.text
        assert result.headers["location"] == "/prompt-tap", result.headers["location"]
        assert not prompt_tap._action_path(aid).exists()
        assert "Purge applied" in client.get("/prompt-tap").text


def test_admin_scope_and_role():
    from app.models import User
    from app.web import api_cli

    user = security.create_user(
        "ledger-reader@example.com", "Reader", Role.TRANSLATOR, "ledger-password-123"
    )
    _, scoped = apikeys.create_key(user.id, "ledger-reader", scopes="api admin")
    _, unscoped = apikeys.create_key(user.id, "ledger-reader", scopes="api docs")
    with TestClient(app) as client:
        csrf_for(client)
        with patch.object(security, "auth_required", return_value=True):
            for key in (scoped, unscoped):
                response = client.post(
                    "/api/admin/ledger/envelopes/off",
                    json={"confirm": ledger_controls.DISABLE_PHRASE},
                    headers={"X-Loc-Token": key},
                )
                assert response.status_code == 403, response.text
        with (
            patch.object(security, "session_user", return_value=User(role=Role.TRANSLATOR)),
            patch.object(security, "auth_required", return_value=True),
        ):
            assert client.post("/prompt-tap/purge", data={}).status_code == 403
    # R3 and the human-only MCP exclusion must remain on BOTH mutating endpoints.
    import inspect

    for function in (api_cli.api_ledger_envelopes, api_cli.api_ledger_purge):
        assert "_r3_gate(request)" in inspect.getsource(function)


def test_mcp_generic_refusal_and_status():
    import asyncio
    import json

    import httpx

    sys.path.insert(0, str(ROOT / "integrations/mcp-server"))
    import server as mcp_server

    user = security.create_user("ledger-mcp@example.com", "MCP", Role.ADMIN, "ledger-password-123")
    _, key = apikeys.create_key(user.id, "ledger-mcp", scopes="api docs admin")

    def client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Loc-Token": key},
        )

    async def exercise():
        with patch.object(mcp_server, "_client", client), patch.object(mcp_server, "TOKEN", key):
            try:
                await mcp_server.manage_install(
                    "set_config",
                    settings={"prompt_tap_envelopes_enabled": "0"},
                    confirm=["prompt_tap_envelopes_enabled"],
                )
            except mcp_server.ToolError as exc:
                assert "guarded" in str(exc), str(exc)
            else:
                raise AssertionError("MCP generic settings bypassed the ceremony")
            result = json.loads(await mcp_server.ledger_summary())
            assert result["ledger_status"]["envelopes_enabled"] is True

    asyncio.run(exercise())


def test_detail_banner_and_scoped_raw_counts():
    import json

    aid = seed()
    with Session(engine) as session:
        run = session.exec(select(TapRun).where(TapRun.action_id == aid)).one()
        rid = run.id
        action = session.get(TapAction, aid)
        action.org_id = 98765
        action.customer_id = 87654
        session.add(action)
        session.commit()
    prompt_tap._action_path(aid).write_text(
        json.dumps({"request": {}, "meta": {"action_id": aid, "run_id": rid}}) + "\n",
        encoding="utf-8",
    )
    assert ledger_controls.status(org_id=98765, scoped=True)["retained_raw_files"] == 1
    assert ledger_controls.status(org_id=None, scoped=True)["retained_raw_files"] == 0
    assert ledger_controls.status(customer_id=87654)["retained_raw_files"] == 1
    settings.set_many({"audit_enabled": "0"})
    try:
        ledger_controls.set_envelopes(False, confirm=ledger_controls.DISABLE_PHRASE, actor="forced")
        with TestClient(app) as client:
            response = client.get(f"/prompt-tap/runs/{rid}")
            assert response.status_code == 200, response.text
            assert "envelope recording is OFF" in response.text
            ledger_controls.set_envelopes(True, actor="forced")
            assert "envelope recording is OFF" not in client.get(f"/prompt-tap/runs/{rid}").text
        with Session(engine) as session:
            rows = session.exec(select(AuditLog).where(AuditLog.actor == "forced")).all()
            assert {row.action for row in rows} == {
                "ledger.envelopes.disable",
                "ledger.envelopes.enable",
            }
    finally:
        settings.set_many({"audit_enabled": "1"})


def test_web_ceremony_default_install():
    """MED-2: on a DEFAULT install (remote_admin_enabled unset) the WEB ceremony is reachable
    by an admin and refuses a non-admin with the normal 403 page, never a bare 404. The /api
    twin keeps the remote-switch lock. LOW-4: a REAL logged-in admin session (auth ON) drives a
    SUCCESSFUL disable and re-enable through the form, so the positive admin rung is exercised."""
    from app.models import User

    saved = _save_settings("remote_admin_enabled", "security_auth_required")
    settings.set_many({"remote_admin_enabled": "0", "security_auth_required": "0"})
    with TestClient(app) as client:
        csrf_for(client)
        # Admin (auth off -> _admin_block is a no-op): a wrong phrase REACHES the ceremony and
        # is refused 400 (not the old bare 404 from the remote-switch lock).
        r = client.post("/prompt-tap/envelopes/off", data={"confirm": "wrong"})
        assert r.status_code == 400, r.text
        # The ceremony card renders only for a caller who may drive it.
        assert "disable envelope recording" in client.get("/prompt-tap").text
        # Non-admin: the normal admin-block page (403), never a 404.
        with (
            patch.object(security, "session_user", return_value=User(role=Role.TRANSLATOR)),
            patch.object(security, "auth_required", return_value=True),
        ):
            r = client.post(
                "/prompt-tap/envelopes/off", data={"confirm": ledger_controls.DISABLE_PHRASE}
            )
            assert r.status_code == 403, r.text
            assert "administrators" in r.text.lower()

    # LOW-4: the SUCCESSFUL path with the login wall ON and remote_admin_enabled OFF -- a real
    # admin session (not an auth-off no-op) disables, then re-enables, through the real form.
    ledger_controls.set_envelopes(True, actor="low4-setup")  # baseline ON
    admin = security.create_user(
        "ledger-low4@example.com", "Low4 Admin", Role.ADMIN, "ledger-password-123"
    )
    settings.set_many({"remote_admin_enabled": "0", "security_auth_required": "1"})
    try:
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post("/auth/login", data={"email": admin.email, "password": "ledger-password-123"})
        assert r.status_code == 303, ("login", r.status_code, r.text)
        csrf_for(cl)  # the token rotates on login
        r = cl.post("/prompt-tap/envelopes/off", data={"confirm": ledger_controls.DISABLE_PHRASE})
        assert r.status_code == 303, r.text
        assert tap_envelope.envelopes_enabled() is False
        page = cl.get("/prompt-tap").text
        assert "envelope recording is OFF" in page  # the banner
        assert (
            cl.get("/admin/health").json()["detail"].get("ledger_envelopes") == "off"
        )  # health word
        r = cl.post("/prompt-tap/envelopes/on", data={})
        assert r.status_code == 303, r.text
        assert tap_envelope.envelopes_enabled() is True
        assert "envelope recording is OFF" not in cl.get("/prompt-tap").text
    finally:
        _restore_settings(saved)  # restore BOTH keys this test moved (gate round-3 LOW-4)
        ledger_controls.set_envelopes(True, actor="low4-cleanup")


def test_settings_save_is_sync():
    """MED-3: POST /settings stays a SYNC handler (no async event-loop regression) and refuses
    the envelope key via a declared Form field, not a second `await request.form()` parse."""
    import inspect

    from app.web import admin as admin_web

    # A SYNC handler runs in FastAPI's threadpool (the async version moved blocking set_many /
    # apply_to_config onto the event loop), and being sync it cannot re-parse the form; the
    # key is refused via a declared Form parameter instead.
    assert not inspect.iscoroutinefunction(admin_web.settings_save)
    params = inspect.signature(admin_web.settings_save).parameters
    assert "prompt_tap_envelopes_enabled" in params


def test_audit_rows_carry_ip():
    """MED-4: the ceremony audit rows record the client IP, like the sibling destructive web
    admin actions (retention), rather than dropping it."""
    settings.set_many({"remote_admin_enabled": "1", "security_auth_required": "0"})
    ledger_controls.set_envelopes(True, actor="ip-setup")
    with TestClient(app) as client:
        csrf_for(client)
        # A sibling destructive web admin action, same client, as the IP reference.
        client.post("/settings/retention", data={"retention_enabled": "1", "retention_days": "7"})
        r = client.post(
            "/prompt-tap/envelopes/off",
            data={"confirm": ledger_controls.DISABLE_PHRASE},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
    with Session(engine) as session:
        ref = session.exec(
            select(AuditLog)
            .where(AuditLog.action == "retention.policy")
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).first()
        row = session.exec(
            select(AuditLog)
            .where(AuditLog.action == "ledger.envelopes.disable")
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).first()
        assert ref is not None and ref.ip, "retention sibling should record an IP"
        assert row is not None and row.ip == ref.ip, (getattr(row, "ip", None), ref.ip)
    ledger_controls.set_envelopes(True, actor="ip-cleanup")


def test_status_scan_throttled_and_bounded():
    """MED-1: the retained-file directory scan is cached (at most one per hour on the render
    path) and the scoped count is bounded by the on-disk stems, not the whole id space."""
    ledger_controls.invalidate_scan_cache()
    org = 424242
    with Session(engine) as session:
        for _ in range(20):  # many envelope rows in the org, NO file on disk
            session.add(
                TapAction(
                    id=tap_envelope.new_id(),
                    status=TapStatus.COMPLETED,
                    finished_at=datetime(2019, 1, 1),
                    org_id=org,
                )
            )
        aid = tap_envelope.new_id()  # exactly one action with a file on disk
        session.add(
            TapAction(
                id=aid, status=TapStatus.COMPLETED, finished_at=datetime(2019, 1, 1), org_id=org
            )
        )
        session.commit()
    prompt_tap._actions_dir().mkdir(parents=True, exist_ok=True)
    prompt_tap._action_path(aid).write_text('{"request": {}, "meta": {}}\n', encoding="utf-8")
    st = ledger_controls.status(org_id=org, scoped=True)
    assert st["retained_raw_files"] == 1, st  # file-driven, not the 21 rows
    # Bounded: with NO files on disk the scoped read opens NO id-space select at all.
    ledger_controls.invalidate_scan_cache()
    with (
        patch.object(ledger_controls, "_scan_raw_dir", return_value={"entries": [], "staged": 0}),
        patch.object(
            ledger_controls, "Session", side_effect=AssertionError("unbounded select on empty disk")
        ),
    ):
        assert ledger_controls.status(org_id=org, scoped=True)["retained_raw_files"] == 0
    # Throttle: N cached renders do at most ONE real directory scan per hour.
    ledger_controls.invalidate_scan_cache()
    calls = {"n": 0}
    real = ledger_controls._scan_raw_dir

    def counting():
        calls["n"] += 1
        return real()

    with patch.object(ledger_controls, "_scan_raw_dir", side_effect=counting):
        for _ in range(5):
            ledger_controls.status(use_cache=True)
    assert calls["n"] == 1, calls
    ledger_controls.invalidate_scan_cache()


def test_purge_staging_reconciled():
    """MED-5: reconciliation settles staged *.purge-* files a hard kill left behind -- at boot
    and inside the TTL sweep, even while raw capture is OFF; status() counts the debt."""
    ad = prompt_tap._actions_dir()
    ad.mkdir(parents=True, exist_ok=True)
    # (a) kill AFTER commit: the TapAction row is gone -> the staged file is deleted.
    gone = tap_envelope.new_id()
    committed_stage = ad / f"{gone}.purge-{'a' * 8}"
    committed_stage.write_text("x\n", encoding="utf-8")
    # (b) kill BEFORE commit: the row still exists -> the file is restored to <stem>.jsonl.
    kept = seed()  # a TapAction row + its <stem>.jsonl
    staged_kept = ad / f"{kept}.purge-{'b' * 8}"
    prompt_tap._action_path(kept).rename(staged_kept)
    assert ledger_controls.status()["staged_purge_files"] == 2
    # Capture OFF, driven THROUGH the TTL sweep (which early-returns capture_disabled but must
    # still reconcile first). grace=0 so these freshly-created stages are eligible, and reset the
    # reconcile throttle so the sweep's (non-forced) reconcile actually scans this tick.
    settings.set_many({"prompt_tap_enabled": "0", "ledger_purge_reconcile_grace_seconds": "0"})
    ledger_controls._reset_reconcile_throttle()
    try:
        res = prompt_tap.sweep_expired_captures()
        assert res.get("skipped") == "capture_disabled", res
        assert not committed_stage.exists()  # committed purge -> removed
        assert prompt_tap._action_path(kept).exists()  # uncommitted purge -> restored
        assert not staged_kept.exists()
        assert ledger_controls.status()["staged_purge_files"] == 0
    finally:
        settings.set_many(
            {"prompt_tap_enabled": "1", "ledger_purge_reconcile_grace_seconds": "600"}
        )
        ledger_controls._reset_reconcile_throttle()


def test_purge_cutoff_must_be_past():
    """LOW-2 (round 1): a today/future cutoff is refused even with a matching typed phrase."""
    today = datetime.now(timezone.utc).date().isoformat()
    future = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    for bad in (today, future):
        refused(
            lambda bad=bad: ledger_controls.purge(
                before=bad, confirm=f"purge envelopes before {bad}"
            )
        )


def test_ceremony_audit_shares_setting_transaction():
    """MED-1 (round 2): the guarded ceremony's audit row is written by the hand-rolled
    _audit_row INSIDE the same transaction as the setting flip. A failure AFTER the row is
    added but BEFORE the commit must roll back BOTH -- no orphan audit row, no setting change
    (the ratified atomicity that forbids replacing _audit_row with the self-committing
    audit.log). test_detail_banner_and_scoped_raw_counts separately pins that the row is written
    even when audit_enabled='0'."""
    ledger_controls.set_envelopes(True, actor="med1-setup")
    real = ledger_controls._audit_row

    def add_then_fail(session, *a, **k):
        real(session, *a, **k)  # actually add + flush the AuditLog row in THIS session
        raise RuntimeError("post-audit, pre-commit failure")

    with Session(engine) as s:
        before = len(
            s.exec(select(AuditLog).where(AuditLog.action == "ledger.envelopes.disable")).all()
        )
    with patch.object(ledger_controls, "_audit_row", side_effect=add_then_fail):
        try:
            ledger_controls.set_envelopes(
                False, confirm=ledger_controls.DISABLE_PHRASE, actor="med1"
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("failure did not abort the ceremony")
    assert tap_envelope.envelopes_enabled(), "setting flip was not rolled back"
    with Session(engine) as s:
        after = len(
            s.exec(select(AuditLog).where(AuditLog.action == "ledger.envelopes.disable")).all()
        )
    assert after == before, (before, after)  # the row rolled back WITH the setting -> neither
    assert audit.verify_chain()[0], "chain broken by a half-written ceremony"


def _purge_args(before="2020-01-01"):
    return {"before": before, "confirm": f"purge envelopes before {before}"}


def test_reconcile_races_inflight_purge():
    """HIGH-1 (round 2): a reconciliation landing INSIDE a purge's rename->commit window must
    NOT resurrect the raw file the purge is deleting, and the purge must not raise. The grace
    window is set to 0 here so the in-process staging LOCK is the only thing that can prevent
    the race (isolating part a); the audit count must match what was actually removed."""
    settings.set_many({"ledger_purge_reconcile_grace_seconds": "0"})
    ledger_controls._reset_reconcile_throttle()
    try:
        # This suite shares ONE DB across tests, so earlier tests leave terminal 2019 actions
        # and raw files behind. Clear that eligible residue first (no race patched yet) so the
        # single seeded action below is the ONLY thing this raced purge removes -> deterministic
        # counts (exactly one staged file lands inside the race window).
        ledger_controls.purge(**_purge_args(), apply=True, actor="race-clear")
        aid = seed()  # a terminal 2019 action + its <aid>.jsonl raw file
        path = prompt_tap._action_path(aid)
        assert path.exists()
        real = ledger_controls._audit_row
        raced = {}

        def race(session, *a, **k):
            # Called INSIDE the purge txn, AFTER staging (rename to .purge-), BEFORE commit.
            # A concurrent scheduler tick reconciles from ANOTHER thread while the purge holds
            # _staging_lock: it must be turned away (part a), not resurrect the staged file.
            def _tick():
                raced["res"] = ledger_controls.reconcile_staged_purges(force=True)

            t = threading.Thread(target=_tick)
            t.start()
            t.join(5)
            return real(session, *a, **k)

        with patch.object(ledger_controls, "_audit_row", side_effect=race):
            result = ledger_controls.purge(
                **_purge_args(), apply=True, batch_size=10, actor="race-test"
            )
        assert not path.exists(), "the purged raw file was resurrected by the race"
        assert result["raw_files"] == 1, result
        assert raced["res"]["skipped"] == "purge_in_progress", raced  # the lock turned it away
        with Session(engine) as s:
            row = s.exec(
                select(AuditLog)
                .where(AuditLog.action == "ledger.envelopes.purge")
                .order_by(AuditLog.id.desc())
                .limit(1)
            ).first()
        # (d) the audit count matches the files actually removed, re-counted after the commit.
        assert json.loads(row.detail)["raw_files"] == result["raw_files"] == 1, row.detail
    finally:
        settings.set_many({"ledger_purge_reconcile_grace_seconds": "600"})
        ledger_controls._reset_reconcile_throttle()


def test_reconcile_grace_and_row_truth():
    """HIGH-1 (round 2, part b): reconciliation leaves a YOUNG staged file alone (it may belong
    to a live purge in ANOTHER process the in-process lock cannot see) but settles an OLD orphan
    by row existence -- removed when the row is gone, restored when the row survives."""
    ad = prompt_tap._actions_dir()
    ad.mkdir(parents=True, exist_ok=True)
    saved = _save_settings("ledger_purge_reconcile_grace_seconds")  # restore what we found (LOW-3)
    settings.set_many({"ledger_purge_reconcile_grace_seconds": "600"})
    ledger_controls._reset_reconcile_throttle()
    # YOUNG: staged through the REAL rename path (production `_staged_path` carries the staging
    # epoch in the name) from a raw file whose OWN mtime is days old -- so "young" comes from the
    # staging moment, not a freshly WRITTEN mtime (gate round-3 HIGH-1 fixed this fixture, which
    # was wrong-in-direction: it proved a fresh-mtime file is deferred, never that the files the
    # product actually stages carry a fresh age).
    young_kept = seed()
    young_raw = prompt_tap._action_path(young_kept)
    os.utime(young_raw, (time.time() - 3 * 86400,) * 2)
    young_stage = ledger_controls._staged_path(young_raw)
    young_raw.rename(young_stage)
    try:
        old_gone = tap_envelope.new_id()
        old_gone_stage = ad / f"{old_gone}.purge-{'d' * 8}"  # row never existed -> orphan
        old_gone_stage.write_text("x\n", encoding="utf-8")
        old_kept = seed()  # a TapAction row + <stem>.jsonl
        old_kept_stage = ad / f"{old_kept}.purge-{'e' * 8}"
        prompt_tap._action_path(old_kept).rename(old_kept_stage)
        old = time.time() - 3600  # older than the 600s grace window
        for p in (old_gone_stage, old_kept_stage):
            os.utime(p, (old, old))
        ledger_controls.reconcile_staged_purges(force=True)
        assert young_stage.exists(), "a young staged file must be left alone (grace window)"
        assert not old_gone_stage.exists(), "an old orphan (row gone) must be removed"
        assert prompt_tap._action_path(old_kept).exists(), "old uncommitted (row present) restored"
        assert not old_kept_stage.exists()
    finally:
        if young_stage.exists():
            young_stage.unlink()  # do not pollute later staged-file counts
        _restore_settings(saved)
        ledger_controls._reset_reconcile_throttle()


def test_reconcile_grace_ages_from_staging():
    """HIGH-1 (round 3): the grace window ages from the STAGING moment, not the raw capture.
    A purge only ever stages a raw file whose mtime is DAYS old (terminal action, finished
    before a PAST UTC cutoff), and `path.rename` preserves that mtime -- so an mtime grace would
    ALWAYS treat a real staged file as old and never protect it, re-opening the cross-process
    race. Staging a 3-day-old raw file through the production `_staged_path` (epoch carried in the
    name), the DEFAULT 600s grace must DEFER it; only an explicit grace=0 override reconciles it.
    This falsifying test FAILS under the old mtime cutoff (the 3-day mtime is outside any sane
    grace -> the file is reconciled, so `staged` vanishes and the raw name reappears)."""
    ad = prompt_tap._actions_dir()
    ad.mkdir(parents=True, exist_ok=True)
    saved = _save_settings("ledger_purge_reconcile_grace_seconds")  # restore what we found (LOW-3)
    # Clear any staged residue earlier tests left, so this pass's counts are about MY file only.
    settings.set_many({"ledger_purge_reconcile_grace_seconds": "0"})
    ledger_controls._reset_reconcile_throttle()
    ledger_controls.reconcile_staged_purges(force=True)
    settings.set_many({"ledger_purge_reconcile_grace_seconds": "600"})
    ledger_controls._reset_reconcile_throttle()
    try:
        aid = seed()  # a TapAction row + <aid>.jsonl
        raw = prompt_tap._action_path(aid)
        old = time.time() - 3 * 86400  # the capture mtime is DAYS old, as any purge target's is
        os.utime(raw, (old, old))
        staged = ledger_controls._staged_path(raw)  # production naming: staging epoch in the name
        raw.rename(staged)  # the real rename path -- the mtime stays 3 days old
        assert staged.exists() and not raw.exists()
        # DEFAULT grace: freshly staged -> DEFERRED, never restored (the falsifying assertions).
        res = ledger_controls.reconcile_staged_purges(force=True)
        assert staged.exists(), "a freshly staged file must be deferred at the default grace"
        assert not raw.exists(), "a deferred file must not be restored to its live name"
        assert res["deferred"] == 1, res
        assert res["restored"] == 0 and res["removed"] == 0, res
        # Opposite polarity: grace=0 makes the SAME staged file eligible -> row present -> restored.
        settings.set_many({"ledger_purge_reconcile_grace_seconds": "0"})
        ledger_controls._reset_reconcile_throttle()
        res0 = ledger_controls.reconcile_staged_purges(force=True)
        assert raw.exists(), "grace=0 must reconcile the staged file (row present -> restored)"
        assert not staged.exists()
        assert res0["restored"] == 1 and res0["deferred"] == 0, res0
    finally:
        _restore_settings(saved)
        ledger_controls._reset_reconcile_throttle()


def test_reconcile_clamps_future_staging_epoch():
    """MED-1 (round 4): a staging epoch in the FUTURE must be treated as OLD residue, not young
    forever. `_staged_path` stamps int(time.time()) from the wall clock, so a future epoch means
    the host clock was ahead at staging (a VM snapshot restore, a container booted before NTP,
    a wrong RTC) and was later stepped back. Left unclamped, the reconciler defers such a file on
    every pass, so the raw payload a committed purge staged lingers under a `.purge-` name (or a
    retained envelope is never restored to its live name). Four arms staged through the real
    rename path, all with a surviving row, one force pass at the DEFAULT 600s grace:
      A fresh (epoch = now)            DEFERRED   (may be a live purge in another process)
      B legacy (no epoch, old mtime)   RESTORED   (falls back to mtime, reads as old)
      C future (epoch = now + 10y)     RESTORED   (clamped to residue -- the falsifying arm)
      D malformed (epoch 'abc')        RESTORED   (falls back to old mtime)
    Under the unclamped code C stays deferred forever, so restored == 2 and deferred == 2."""
    ad = prompt_tap._actions_dir()
    ad.mkdir(parents=True, exist_ok=True)
    saved = _save_settings("ledger_purge_reconcile_grace_seconds")  # restore what we found (LOW-3)
    # Clear residue earlier tests left, so this pass's counts are about MY four files only.
    settings.set_many({"ledger_purge_reconcile_grace_seconds": "0"})
    ledger_controls._reset_reconcile_throttle()
    ledger_controls.reconcile_staged_purges(force=True)
    settings.set_many({"ledger_purge_reconcile_grace_seconds": "600"})
    ledger_controls._reset_reconcile_throttle()
    old = time.time() - 3 * 86400  # days-old capture mtime, as any purge target's is
    a_id = seed()  # A: fresh staging (epoch = now) via the real path -> row present -> deferred
    a_raw = prompt_tap._action_path(a_id)
    os.utime(a_raw, (old, old))
    a_stage = ledger_controls._staged_path(a_raw)
    a_raw.rename(a_stage)
    b_id = seed()  # B: legacy epoch-less name, old mtime -> reads old -> restored
    b_stage = ad / f"{b_id}.purge-{'b' * 8}"
    prompt_tap._action_path(b_id).rename(b_stage)
    os.utime(b_stage, (old, old))
    c_id = seed()  # C: future epoch (now + 10y) -> clamped to residue -> restored (MED-1)
    c_stage = ad / f"{c_id}.purge-{'c' * 8}-{int(time.time()) + 10 * 365 * 86400}"
    prompt_tap._action_path(c_id).rename(c_stage)
    os.utime(c_stage, (old, old))
    d_id = seed()  # D: malformed epoch -> falls back to old mtime -> restored
    d_stage = ad / f"{d_id}.purge-{'d' * 8}-abc"
    prompt_tap._action_path(d_id).rename(d_stage)
    os.utime(d_stage, (old, old))
    try:
        res = ledger_controls.reconcile_staged_purges(force=True)
        assert a_stage.exists(), "a fresh staging epoch must be deferred at the default grace"
        assert prompt_tap._action_path(b_id).exists(), (
            "a legacy epoch-less orphan reads old -> restored"
        )
        assert prompt_tap._action_path(c_id).exists(), (
            "a future staging epoch must be clamped to OLD residue and reconciled (MED-1)"
        )
        assert not c_stage.exists()
        assert prompt_tap._action_path(d_id).exists(), (
            "a malformed epoch falls back to old mtime -> restored"
        )
        assert res["deferred"] == 1 and res["restored"] == 3, res
        assert res["removed"] == 0, res  # every row survives -> all settled by restore
    finally:
        for p in (a_stage, b_stage, c_stage, d_stage):
            if p.exists():
                p.unlink()  # do not pollute later staged-file counts
        _restore_settings(saved)
        ledger_controls._reset_reconcile_throttle()


def test_reconcile_listdir_throttled():
    """LOW-1 (round 2): the reconcile raw-dir listing is throttled -- the scheduler tick (every
    ~30s) does at most one listdir per _SWEEP_MIN_INTERVAL_S; force=True (boot, tests) bypasses."""
    ledger_controls._reset_reconcile_throttle()
    calls = {"n": 0}
    real = ledger_controls._dir_listing

    def counting(ad):
        calls["n"] += 1
        return real(ad)

    try:
        with patch.object(ledger_controls, "_dir_listing", side_effect=counting):
            for _ in range(3):
                ledger_controls.reconcile_staged_purges(force=False)
            assert calls["n"] == 1, calls  # throttled: three ticks, one listdir
            ledger_controls.reconcile_staged_purges(force=True)
            assert calls["n"] == 2, calls  # force bypasses the throttle
    finally:
        ledger_controls._reset_reconcile_throttle()


def test_sweep_invalidates_status_cache():
    """LOW-2 (round 2): the TTL sweep invalidates the Ledger status scan cache when it deletes
    files, so the render path does not keep reporting files it just reclaimed."""
    saved = _save_settings("prompt_tap_enabled", "prompt_tap_ttl_days")
    settings.set_many({"prompt_tap_enabled": "1", "prompt_tap_ttl_days": "1"})
    # No staged residue is present here, so the sweep's own reconcile settles nothing and cannot
    # invalidate the cache -- the invalidation under test comes only from the file deletion below.
    ledger_controls.invalidate_scan_cache()
    try:
        aid = seed()  # a fresh <aid>.jsonl (mtime ~= now)
        before = ledger_controls.status(use_cache=True)["retained_raw_files"]  # warm the cache
        assert before >= 1
        old = time.time() - 5 * 86400  # older than the 1-day TTL
        os.utime(prompt_tap._action_path(aid), (old, old))
        res = prompt_tap.sweep_expired_captures(force=True)
        assert res["deleted"] >= 1, res
        after = ledger_controls.status(use_cache=True)["retained_raw_files"]
        assert after == before - res["deleted"], (before, after, res["deleted"])
    finally:
        _restore_settings(saved)  # restore what this test found, not a hardcoded default (LOW-4)
        ledger_controls.invalidate_scan_cache()
        ledger_controls._reset_reconcile_throttle()


def main():
    init_db()
    tests = [
        test_generic_setting_refused,
        test_surfaces,
        test_purge_and_rollback,
        test_raw_notice_and_help,
        test_audit_failure_is_atomic_and_chain_valid,
        test_cli_and_web_apply,
        test_admin_scope_and_role,
        test_mcp_generic_refusal_and_status,
        test_detail_banner_and_scoped_raw_counts,
        test_web_ceremony_default_install,
        test_settings_save_is_sync,
        test_audit_rows_carry_ip,
        test_status_scan_throttled_and_bounded,
        test_purge_staging_reconciled,
        test_purge_cutoff_must_be_past,
        test_ceremony_audit_shares_setting_transaction,
        test_reconcile_races_inflight_purge,
        test_reconcile_grace_and_row_truth,
        test_reconcile_grace_ages_from_staging,
        test_reconcile_clamps_future_staging_epoch,
        test_reconcile_listdir_throttled,
        test_sweep_invalidates_status_cache,
    ]
    assert set(tests) == {v for k, v in globals().items() if k.startswith("test_")}
    for test in tests:
        test()
    print("LEDGER GUARDED CONTROLS OK")


if __name__ == "__main__":
    main()
