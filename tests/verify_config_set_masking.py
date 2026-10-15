"""#519 - `config set` never prints or audits a secret or a credential-bearing URL.

  1. the confirm prompt of a security-sensitive secret key (sso_client_secret) shows the key and
     a mask, never the value;
  2. the success line / --json value of that set shows the mask;
  3. the audit detail of a URL key that read surfaces strip (otel_endpoint) carries no
     `user:pass@` and no query string, on the CLI door AND on the /api door;
  4. `settings.mask_value` is the one masking seam (same output as `display_value`);
  5. the test-instance tool writes web-login.txt / api-key files owner-only (0600).

Offline; throwaway LOC_TMS_DATA_DIR. ASCII-only prints.
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cfgmask_")

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import cmd_config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import AuditLog, Role
from app.services import apikeys, security, settings

init_db()

SSO_VALUE = "QZXsecretSsoValue0123456789abcdef"
URL_USER = "svcuser"
URL_PW = "QZXurlPassword0123456789"
URL_QUERY_VAL = "QZXqueryToken0123456789"
CRED_URL = f"https://{URL_USER}:{URL_PW}@collector.example.com:4318/v1/traces?x={URL_QUERY_VAL}"
API_URL = f"https://searx.example.com/search?x={URL_QUERY_VAL}"
ADMIN_PW = "cfgmask-admin-pw-1234"


def ok(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def audit_rows(action):
    with Session(engine) as s:
        return s.exec(select(AuditLog).where(AuditLog.action == action)).all()


def leaks(text):
    return [t for t in (SSO_VALUE, URL_PW, URL_QUERY_VAL, f"{URL_USER}:") if t in text]


def test_mask_value_seam():
    ok(settings.mask_value("sso_client_secret", SSO_VALUE) == "(set)", "secret masked")
    ok(settings.mask_value("sso_client_secret", "") == "(unset)", "empty secret is (unset)")
    shown = settings.mask_value("otel_endpoint", CRED_URL)
    ok(not leaks(shown), f"url credentials survive mask_value: {shown}")
    ok("collector.example.com" in shown, "the host stays visible")
    ok(settings.mask_value("org_name", "Acme") == "Acme", "plain keys pass through")
    print("  [mask_value] one masking seam for secrets and credential-bearing URLs OK")


def test_cli_prompt_and_success_line():
    prompts = []
    real_confirm = cmd_config.confirm

    def spy(emit, prompt, assume_yes):
        prompts.append(prompt)
        return True

    cmd_config.confirm = spy
    try:
        rc, out = run_cli(["config", "set", "sso_client_secret", SSO_VALUE])
    finally:
        cmd_config.confirm = real_confirm
    ok(rc == 0, f"config set sso_client_secret failed: rc={rc} {out[:300]}")
    ok(prompts, "a security-sensitive key must ask for confirmation")
    ok(
        not any(SSO_VALUE in p for p in prompts),
        f"the confirm prompt echoed the secret: {prompts}",
    )
    ok("sso_client_secret" in prompts[0], f"the prompt must name the key: {prompts}")
    ok(SSO_VALUE not in out, f"the success output echoed the secret: {out}")
    rows = audit_rows("config.set")
    ok(not any(SSO_VALUE in (r.detail or "") for r in rows), "the audit row carries the secret")
    print("  [CLI] confirm prompt, success line and audit hold a mask, never the secret OK")


def test_cli_url_audit():
    rc, out = run_cli(["config", "set", "otel_endpoint", CRED_URL])
    ok(rc == 0, f"config set otel_endpoint failed: rc={rc} {out[:300]}")
    ok(not leaks(out), f"the success output echoed URL credentials: {out}")
    rows = [r for r in audit_rows("config.set") if r.target == "otel_endpoint"]
    ok(rows, "config set must write an audit row")
    ok(
        not any(leaks((r.detail or "") + (r.target or "")) for r in rows),
        f"the audit detail carries URL credentials: {[r.detail for r in rows]}",
    )
    ok(settings.get("otel_endpoint") == CRED_URL, "the stored value must stay the real value")
    print("  [CLI] a credential-bearing URL is masked in output and audit; stored value intact OK")


def test_api_url_audit():
    admin = security.create_user("cfgmask@x.com", "Cfg Mask", Role.ADMIN, ADMIN_PW)
    _row, raw = apikeys.create_key(admin.id, "cfgmask", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    settings.invalidate_cache()
    api = TestClient(__import__("app.main", fromlist=["app"]).app)
    key = "local_base_url"
    r = api.post(
        "/api/admin/config",
        headers={"X-Loc-Token": raw},
        json={"settings": {key: API_URL}, "confirm": [key]},
    )
    ok(r.status_code == 200, f"/api config set refused: {r.status_code} {r.text[:300]}")
    ok(not leaks(r.text), f"the /api response echoed URL credentials: {r.text}")
    rows = [r for r in audit_rows("config.set") if key in (r.detail or "")]
    ok(rows, "the /api config set must write an audit row naming the key")
    ok(
        not any(leaks(r.detail or "") for r in rows),
        f"the /api audit detail carries URL credentials: {[r.detail for r in rows]}",
    )
    print("  [/api] the audit detail of a credential-bearing URL key is masked OK")


def test_test_instance_secret_files():
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))
    import scripts.test_instance as ti

    modes = []
    real_chmod = os.chmod

    def spy_chmod(path, mode, *a, **k):
        modes.append(mode)
        return real_chmod(path, mode, *a, **k)

    os.chmod = spy_chmod
    try:
        target = Path(tempfile.mkdtemp(prefix="loctms_ti_")) / "web-login.txt"
        target.write_text("old\n", encoding="utf-8")  # a pre-existing, default-mode file
        ti._write_secret(target, "line")
    finally:
        os.chmod = real_chmod
    ok(target.read_text(encoding="utf-8") == "line\n", "the file content is unchanged")
    ok(0o600 in modes, f"_write_secret must set owner-only permissions (chmod calls: {modes})")
    if os.name == "posix":
        ok((target.stat().st_mode & 0o077) == 0, "group/other bits must be clear")
    print("  [test-instance] secret files are written owner-only (0600) OK")


def main():
    test_mask_value_seam()
    test_cli_prompt_and_success_line()
    test_cli_url_audit()
    test_api_url_audit()
    test_test_instance_secret_files()
    print("CONFIG SET MASKING OK")


if __name__ == "__main__":
    main()
