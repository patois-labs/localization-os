"""FX-4 credential removal and legacy URL display regressions, offline."""

import asyncio
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_fx4_")
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, EngineProfile
from app.services import apikeys, egress, engines, jev_inspector, security, settings

init_db()
errors = []
p = engines.create_profile(
    "clear",
    "jev",
    api_key="fixture",  # pregate: allow-secret offline fixture
)
try:
    p = engines.update_profile(p.id, clear_api_key=True)
    assert not p.api_key_enc and not p.enabled
except (TypeError, AssertionError) as exc:
    errors.append("#205 explicit clear: " + str(exc))
try:
    engines.create_profile("userinfo", "local", base_url="http://user:secret@127.0.0.1:8077")
except ValueError as exc:
    assert "API key" in str(exc)
else:
    errors.append("#206 userinfo accepted")
assert not errors, errors


def command(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli(args)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


local = engines.create_profile(
    "local",
    "jev_local",
    base_url="http://100.64.0.1:8077",
    api_key="fixture",  # pregate: allow-secret offline fixture
)
for blank in ("", "   "):
    assert engines.update_profile(local.id, api_key=blank).api_key_enc
code, out = command(["engine", "update", str(local.id), "--clear-key"])
assert code == 0 and "no API key" in out, out
assert not engines.get_profile(local.id).api_key_enc
assert not engines.update_profile(p.id, enabled=True).enabled
assert not engines.test_profile(p.id)[0]
pair = engines.create_profile(
    "paired",
    "amazon",
    api_key="fixture",  # pregate: allow-secret offline fixture
    cfg={"access_key_secret": "paired-fixture"},  # pregate: allow-secret offline fixture
)
for blank in ("", "   "):
    engines.update_profile(pair.id, cfg={"access_key_secret": blank})
    assert engines.profile_secret(pair.id) == "paired-fixture", "M7: blank must keep paired secret"
code, out = command(["engine", "update", str(pair.id), "--clear-access-key-secret"])
assert code == 0, out
assert not engines.profile_secret(pair.id)
client = TestClient(app)
user = security.create_user("fx4@test.invalid", "FX4", "admin", "fx4-test-password")
_, token = apikeys.create_key(user.id, "fx4", scopes="api docs admin")
settings.set_many({"remote_admin_enabled": "1"})
headers = {"X-Loc-Token": token}
url = f"/api/admin/engines/{pair.id}/update"
for flag in ("clear_api_key", "clear_access_key_secret"):
    assert client.post(url, json={flag: "false"}, headers=headers).status_code == 400
assert (
    client.post(url, json={"clear_api_key": True}, headers=headers).json()["engine"]["key_set"]
    is False
)
engines.update_profile(
    pair.id,
    api_key="fixture",  # pregate: allow-secret offline fixture
    cfg={"access_key_secret": "fixture"},  # pregate: allow-secret offline fixture
)
with Session(engine) as s:
    before_web = {row.id for row in s.exec(select(AuditLog))}
r = client.post(
    f"/setup-ai/engines/{pair.id}",
    data={
        "clear_api_key": "1",  # pregate: allow-secret explicit removal control
        "clear_access_key_secret": "1",  # pregate: allow-secret explicit removal control
    },
    follow_redirects=False,
)
assert r.status_code == 303, r.text
assert not engines.get_profile(pair.id).api_key_enc and not engines.profile_secret(pair.id)
with Session(engine) as s:
    web_details = [
        row.detail
        for row in s.exec(select(AuditLog).where(AuditLog.action == "engine.update"))
        if row.id not in before_web
    ]
    details = [
        row.detail for row in s.exec(select(AuditLog).where(AuditLog.action == "engine.update"))
    ]
assert len(web_details) == 1 and "API key removed" in web_details[0], "M14: web API-key audit"
assert "access key secret removed" in web_details[0], "M14: web paired-secret audit"
assert any("API key removed" in detail for detail in details)
assert any("access key secret removed" in detail for detail in details)
assert all("fixture" not in detail for detail in details)
engines.update_profile(pair.id, api_key="fixture")  # pregate: allow-secret offline fixture
for blank in ("", "   "):
    assert client.post(url, json={"api_key": blank}, headers=headers).json()["engine"]["key_set"]
r = client.post(
    url,
    json={"clear_api_key": True, "api_key": "replacement"},  # pregate: allow-secret offline fixture
    headers=headers,
)
assert (
    r.status_code == 400 and engines.dec_key(engines.get_profile(pair.id).api_key_enc) == "fixture"
)
engines.update_profile(
    pair.id,
    cfg={"access_key_secret": "fixture"},  # pregate: allow-secret offline fixture
)
r = client.post(url, json={"clear_access_key_secret": True}, headers=headers)
assert r.status_code == 200 and not engines.profile_secret(pair.id)
bad = "http://user:secret@127.0.0.1:8077"
for endpoint, body in (
    ("/api/admin/engines", {"name": "bad", "kind": "local", "base_url": bad}),
    (url, {"base_url": bad}),
):
    r = client.post(endpoint, json=body, headers=headers)
    assert r.status_code == 400 and "API key" in r.text and "user:secret" not in r.text
for args in (["create", "--name", "bad", "--kind", "local"], ["update", str(local.id)]):
    code, out = command(["engine", *args, "--base-url", bad])
    assert code == 2 and "API key" in out and "user:secret" not in out
for endpoint in ("/setup-ai/engines", f"/setup-ai/engines/{local.id}"):
    r = client.post(
        endpoint, data={"name": "bad", "kind": "local", "base_url": bad}, follow_redirects=False
    )
    assert r.status_code == 303 and "engine_error" in r.headers["location"], r.text
with Session(engine) as s:
    legacy = s.get(EngineProfile, local.id)
    legacy.base_url = bad
    s.add(legacy)
    s.commit()
legacy = engines.get_profile(local.id)
assert engines.engine_row(legacy)["base_url"] == "http://127.0.0.1:8077"
assert "userinfo" in str(jev_inspector.inspect_payload(legacy)["warnings"])
for args in (
    ["engine", "show", str(local.id)],
    ["--json", "engine", "list"],
    ["--json", "engine", "show", str(local.id)],
):
    code, out = command(args)
    assert code == 0 and "user:secret" not in out
assert "user:secret" not in client.get("/setup-ai").text
assert (
    engines.display_base_url("http://user:secret@tail@127.0.0.1:8077") == "http://127.0.0.1:8077"
), "M15: redact through last at sign"

# Collect independent fix-round failures so one defect cannot hide another red test.
fix_errors = []


def fix_check(label, check):
    try:
        check()
    except (AssertionError, ValueError) as exc:
        fix_errors.append(f"{label}: {exc}")


def legacy_edits():
    shown = engines.display_base_url(bad)
    page = client.get("/setup-ai").text
    for body in ({"name": "legacy API rename"}, {"base_url": shown, "model": "new-model"}):
        response = client.post(f"/api/admin/engines/{local.id}/update", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert engines.get_profile(local.id).base_url == bad
    for extra in (["--name", "legacy CLI rename"], ["--base-url", shown, "--model", "cli-model"]):
        code, output = command(["engine", "update", str(local.id), *extra])
        assert code == 0, output
        assert engines.get_profile(local.id).base_url == bad
    response = client.post(
        f"/setup-ai/engines/{local.id}",
        data={"name": "legacy web rename", "base_url": shown, "enabled": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "engine_saved=1" in response.headers["location"]
    assert engines.get_profile(local.id).base_url == bad, "web save stripped legacy credentials"
    assert "This URL has stored credentials; move them to the API key field" in page
    try:
        engines.update_profile(local.id, base_url="http://other:secret@127.0.0.1:8077")
    except ValueError:
        pass
    else:
        raise AssertionError("changed URL userinfo accepted")
    assert (
        engines.update_profile(local.id, base_url="http://127.0.0.1:8078").base_url
        == "http://127.0.0.1:8078"
    )
    with Session(engine) as s:
        row = s.get(EngineProfile, local.id)
        row.base_url = bad
        s.add(row)
        s.commit()


def legacy_setting():
    from app.models import Setting

    try:
        settings.validate_value("local_base_url", bad)
    except settings.InvalidSettingValue as exc:
        assert "API key" in str(exc) and "user:secret" not in str(exc)
    else:
        raise AssertionError("local_base_url accepted userinfo")
    code, output = command(["config", "set", "local_base_url", bad])
    assert code == 2 and "API key" in output and "user:secret" not in output
    response = client.post("/setup-ai/local", data={"base_url": bad}, follow_redirects=False)
    assert response.status_code == 303 and "key_error=" in response.headers["location"]
    with Session(engine) as s:
        row = s.get(Setting, "local_base_url")
        if row is None:
            row = Setting(key="local_base_url", value=bad)
        row.value = bad
        s.add(row)
        s.commit()
    settings.invalidate_cache()
    settings.apply_to_config()
    assert settings.get("local_base_url") == bad
    assert "user:secret" not in client.get("/setup-ai").text


def engine_table():
    doc = (Path(__file__).resolve().parents[1] / "docs/CLI.md").read_text(encoding="utf-8")
    block = doc[doc.index("| `engine list`") : doc.index("| `engine import-jev-env`")]
    assert all(line.startswith("|") for line in block.splitlines()), "engine table interrupted"


def low_url_edges():
    try:
        engines.create_profile(
            "malformed", "local", base_url="http://user:secret\uff20127.0.0.1:8077"
        )
    except ValueError as exc:
        assert "secret" not in str(exc), "LOW-3 malformed URL echoed credentials"
    else:
        raise AssertionError("malformed URL accepted")
    assert "secret" not in engines.display_base_url("user:secret@127.0.0.1:8077"), (
        "LOW-4 scheme-less credentials shown"
    )


def sampling_form():
    """#342 review: a /setup-ai/local post that OMITS the sampling fields keeps the saved
    values (it must not wipe local_seed); a present-but-blank seed clears it; a bad value
    is refused."""
    url = "http://127.0.0.1:8077"
    settings.set_many(
        {"local_temperature": "0.4", "local_judge_temperature": "0.1", "local_seed": "9"}
    )
    r = client.post("/setup-ai/local", data={"base_url": url}, follow_redirects=False)
    assert r.status_code == 303 and "key_error=" not in r.headers["location"], r.headers
    assert settings.get("local_temperature") == "0.4", settings.get("local_temperature")
    assert settings.get("local_judge_temperature") == "0.1"
    assert settings.get("local_seed") == "9", "an omitted seed field wiped the saved seed"
    r = client.post(
        "/setup-ai/local",
        data={
            "base_url": url,
            "local_seed": "",
            "local_temperature": "0.6",
            "local_sampling_form": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "key_error=" not in r.headers["location"], r.headers
    assert (
        settings.get("local_seed") in ("", None) and settings.get("local_temperature") == "0.6"
    ), (settings.get("local_seed"), settings.get("local_temperature"), r.headers)
    r = client.post(
        "/setup-ai/local",
        data={"base_url": url, "local_temperature": "9"},
        follow_redirects=False,
    )
    assert "key_error=" in r.headers["location"] and settings.get("local_temperature") == "0.6", (
        r.headers,
        settings.get("local_temperature"),
    )


def warmup_checkbox():
    """#341 fix round MEDIUM-1: the Setup AI local card has a warm-up checkbox. With the
    sampling marker an unchecked box saves "0" and a checked one "1"; a post WITHOUT the
    marker (older client) leaves the saved value alone; the card renders its state."""
    url = "http://127.0.0.1:8077"
    form = {"base_url": url, "local_sampling_form": "1"}
    settings.set_many({"local_model_warmup": "1"})
    r = client.post("/setup-ai/local", data=form, follow_redirects=False)
    assert r.status_code == 303 and "key_error=" not in r.headers["location"], r.headers
    assert settings.get("local_model_warmup") == "0", "unchecked box must save 0"
    assert 'name="local_model_warmup"' in client.get("/setup-ai").text
    assert (
        'value="1" checked'
        not in client.get("/setup-ai")
        .text.split('name="local_model_warmup"')[1]
        .split("</label>")[0]
    ), "box rendered checked while off"
    r = client.post(
        "/setup-ai/local", data={**form, "local_model_warmup": "1"}, follow_redirects=False
    )
    assert settings.get("local_model_warmup") == "1", "checked box must save 1"
    tail = client.get("/setup-ai").text.split('name="local_model_warmup"')[1].split("</label>")[0]
    assert "checked" in tail, "box not rendered checked while on"
    settings.set_many({"local_model_warmup": "0"})
    r = client.post("/setup-ai/local", data={"base_url": url}, follow_redirects=False)
    assert r.status_code == 303 and settings.get("local_model_warmup") == "0", (
        "a post without the marker flipped the setting"
    )
    settings.set_many({"local_model_warmup": "1"})
    client.post("/setup-ai/local", data={"base_url": url}, follow_redirects=False)
    assert settings.get("local_model_warmup") == "1", "marker-less post flipped it off"
    try:
        settings.validate_value("local_model_warmup", "banana")
    except settings.InvalidSettingValue:
        pass
    else:
        raise AssertionError("LOW-4: local_model_warmup accepted 'banana'")
    for ok in ("0", "1", "true", "OFF", "yes"):
        settings.validate_value("local_model_warmup", ok)


fix_check("#342 sampling form", sampling_form)
fix_check("#341 warmup checkbox", warmup_checkbox)
fix_check("M1 legacy edit", legacy_edits)
fix_check("M2 command table", engine_table)
fix_check("L legacy setting", legacy_setting)
fix_check("LOW URL edges", low_url_edges)
assert not fix_errors, fix_errors
with (
    patch.dict(engines._PROBES, {"jev_local": lambda *args: (True, "reachable")}),
    patch.object(
        egress, "validate_local_engine_target", wraps=egress.validate_local_engine_target
    ) as validate,
):
    ok, message = engines.test_profile(
        local.id, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert ok and "userinfo" in message and "user:secret" not in message
    assert validate.call_args.args[0] == bad
try:
    egress.validate_local_engine_target("http://127.0.0.1:8077@169.254.169.254")
except egress.EgressBlocked:
    pass
else:
    raise AssertionError("SSRF must judge real host")
mcp_dir = Path(__file__).resolve().parents[1] / "integrations" / "mcp-server"
sys.path.insert(0, str(mcp_dir))
spec = importlib.util.spec_from_file_location("fx4_mcp", mcp_dir / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def local_client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
    )


with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", token):
    for action in ("create_engine", "update_engine"):
        try:
            asyncio.run(
                server.manage_install(
                    action=action,
                    engine_id=local.id,
                    name="MCP bad",
                    kind="local",
                    base_url=bad + "/changed",
                )
            )
        except Exception as exc:
            assert "API key" in str(exc) and "user:secret" not in str(exc)
        else:
            raise AssertionError("MCP accepted URL credentials")

# Auth-on form writes require CSRF before the existing update handler runs.
from tests._csrf import csrf_for

settings.set_many({"security_auth_required": "1"})
auth_client = TestClient(app)
csrf_for(auth_client)
r = auth_client.post(
    "/auth/login",
    data={
        "email": user.email,
        "password": "fx4-test-password",  # pregate: allow-secret offline login fixture
    },
)
csrf_for(auth_client)
engines.update_profile(pair.id, api_key="fixture")  # pregate: allow-secret offline fixture
auth_client.headers.pop("X-CSRF-Token", None)
r = auth_client.post(
    f"/setup-ai/engines/{pair.id}",
    data={"clear_api_key": "1"},  # pregate: allow-secret explicit removal control
    follow_redirects=False,
)
assert r.status_code in (303, 403) and engines.get_profile(pair.id).api_key_enc
csrf_for(auth_client)
r = auth_client.post(
    f"/setup-ai/engines/{pair.id}",
    data={"clear_api_key": "1"},  # pregate: allow-secret explicit removal control
    follow_redirects=False,
)
assert r.status_code == 303 and not engines.get_profile(pair.id).api_key_enc
print("verify_fx4_engine_hygiene OK")
