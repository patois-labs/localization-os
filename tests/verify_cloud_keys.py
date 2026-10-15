"""In-app cloud API key management (Claude/Gemini/OpenAI) — fully offline.

Hermetic: throwaway LOC_TMS_DATA_DIR, provider env keys cleared (so config._ENV_*
start empty and the built-ins run the offline MockProvider), ASCII-only prints,
FastAPI TestClient for the Setup AI routes. ZERO real network calls.

Covers:
  * set -> the Setting row is Fernet-encrypted (raw != plaintext) and decrypts back;
    the cloud_key_*_enc keys are NOT in settings.DEFAULTS (kept off all_values()).
  * precedence: a stored key overrides the env-original; remove falls back to the
    env-original; remove with no env -> keyless.
  * provider_is_live flips after set/remove in the SAME process (no restart), and
    the effective config.* attribute updates.
  * trim-whitespace/surrounding-quotes on save.
  * NO surface echoes the full key: Setup AI page HTML after a save, the /settings
    page, `config list`, and `providers key show` (human + --json).
  * audit rows exist for set/remove with the last4 ONLY (never the key).
  * RBAC: translator AND linguist are blocked from the key routes (admin-only —
    setting a key enables real spend, like raising the spend cap); admin allowed.
  * CLI providers key set/show/clear happy paths + parity registration.
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cloudkeys_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Setting
from app.services import cloud_keys, security, settings
from app.services.providers import provider_is_live
from tests._csrf import csrf_for, sign_out

init_db()


# --- helpers -----------------------------------------------------------------
def _clear_all():
    """Remove every app-stored cloud key and reset the env-originals to empty, so
    each test starts from the keyless baseline (offline mock everywhere)."""
    for p in cloud_keys.CLOUD_PROVIDERS:
        settings.set_secret(cloud_keys._setting_key(p), "")
    config._ENV_ANTHROPIC_API_KEY = ""
    config._ENV_GEMINI_API_KEY = ""
    config._ENV_OPENAI_API_KEY = ""
    settings.apply_to_config()


def _audit_rows(action):
    with Session(engine) as s:
        return list(s.exec(select(AuditLog).where(AuditLog.action == action)).all())


# --- tests -------------------------------------------------------------------
def test_encrypted_at_rest_and_roundtrip():
    _clear_all()
    plaintext = "sk-ant-secret-abcd1234"
    cloud_keys.set_key("claude", plaintext, actor="tester")
    with Session(engine) as s:
        row = s.get(Setting, "cloud_key_claude_enc")
    assert row is not None and row.value, "the encrypted setting row must exist"
    assert plaintext not in row.value, "the stored value must be ciphertext, not plaintext"
    assert settings.get_secret("cloud_key_claude_enc") == plaintext, "must decrypt back"
    # DELIBERATELY kept out of DEFAULTS so it never surfaces in all_values().
    assert "cloud_key_claude_enc" not in settings.DEFAULTS
    assert "cloud_key_claude_enc" not in settings.all_values()
    print("  set -> Setting row is Fernet ciphertext, decrypts back, off DEFAULTS/all_values OK")


def test_precedence_and_liveness():
    _clear_all()
    # keyless baseline
    assert cloud_keys.source("gemini") == "none"
    assert provider_is_live("gemini") is False
    # env-only: simulate a .env-sourced key (the import-time original).
    config._ENV_GEMINI_API_KEY = "env-gem-0000"
    settings.apply_to_config()
    assert cloud_keys.source("gemini") == "env"
    assert cloud_keys.effective_key("gemini") == "env-gem-0000"
    assert config.GEMINI_API_KEY == "env-gem-0000" and provider_is_live("gemini") is True
    # stored OVERRIDES env (no restart — apply_to_config ran inside set_key).
    cloud_keys.set_key("gemini", "app-gem-9999", actor="tester")
    assert cloud_keys.source("gemini") == "app"
    assert cloud_keys.effective_key("gemini") == "app-gem-9999"
    assert config.GEMINI_API_KEY == "app-gem-9999" and provider_is_live("gemini") is True
    # remove -> falls back to the env key.
    had = cloud_keys.remove_key("gemini", actor="tester")
    assert had is True
    assert cloud_keys.source("gemini") == "env"
    assert config.GEMINI_API_KEY == "env-gem-0000" and provider_is_live("gemini") is True
    # remove again with the env cleared -> keyless (mock).
    config._ENV_GEMINI_API_KEY = ""
    settings.apply_to_config()
    cloud_keys.remove_key("gemini", actor="tester")
    assert cloud_keys.source("gemini") == "none"
    assert config.GEMINI_API_KEY == "" and provider_is_live("gemini") is False
    print("  precedence stored>env>none; live flips after set/remove without restart OK")


def test_trim_whitespace_and_quotes():
    _clear_all()
    cloud_keys.set_key("openai", '  "sk-openai-quoted-4242"  ', actor="tester")
    assert cloud_keys.stored_key("openai") == "sk-openai-quoted-4242", "quotes+ws trimmed"
    assert cloud_keys.last4("openai") == "4242"
    # empty / whitespace-only is refused (never blanks a stored key).
    try:
        cloud_keys.set_key("openai", "   ", actor="tester")
        raise AssertionError("empty key must raise ValueError")
    except ValueError:
        pass
    assert cloud_keys.stored_key("openai") == "sk-openai-quoted-4242", (
        "still set after refused empty"
    )
    print("  save trims surrounding whitespace/quotes; empty save refused OK")


def test_audit_has_last4_only():
    _clear_all()
    secret = "sk-audit-secret-7777"
    cloud_keys.set_key("claude", secret, actor="auditor")
    cloud_keys.remove_key("claude", actor="auditor")
    set_rows = _audit_rows("cloud_key.set")
    rem_rows = _audit_rows("cloud_key.remove")
    assert set_rows and rem_rows, "set + remove must be audited"
    for r in set_rows + rem_rows:
        assert secret not in (r.detail or ""), "audit detail must never contain the key"
    assert any("7777" in (r.detail or "") for r in set_rows), "set audit carries the last4"
    print("  audit rows for set/remove carry last4 only, never the key OK")


def test_web_routes_no_leak_and_immediate_effect():
    _clear_all()
    c = TestClient(app)
    secret = "sk-web-claude-supersecret-5150"
    r = c.post("/setup-ai/keys/claude", data={"api_key": secret}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    # took effect immediately (live) + stored.
    assert cloud_keys.source("claude") == "app" and provider_is_live("claude") is True
    # the page renders the fingerprint but NEVER the full key.
    page = c.get("/setup-ai").text
    assert secret not in page, "the Setup AI page must never echo the full key"
    assert "5150" in page, "the last4 fingerprint should render"
    assert "key set in app" in page, "the app-source badge should render"
    # the password input renders empty (no value attribute carrying the key).
    assert 'value=""' in page and secret not in page
    # the settings page must not leak it either.
    spage = c.get("/settings").text
    assert secret not in spage, "the /settings page must never dump the key"
    # remove via the web route -> keyless (no env), live flips off.
    r2 = c.post("/setup-ai/keys/claude/delete", follow_redirects=False)
    assert r2.status_code == 303
    assert cloud_keys.source("claude") == "none" and provider_is_live("claude") is False
    # unknown provider is refused cleanly (no 500).
    r3 = c.post("/setup-ai/keys/bogus", data={"api_key": "x"}, follow_redirects=False)
    assert r3.status_code == 303 and "key_error" in r3.headers["location"]
    print("  web set/remove: immediate effect, no key in page/settings HTML, unknown refused OK")


def test_rbac_translator_blocked_admin_ok():
    # Flip auth ON with the standard seeded users (admin/linguist/translator).
    security.seed_initial()
    c = TestClient(app)

    def login(who, pw):
        csrf_for(c)
        r = c.post(
            "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
        )
        if r.status_code == 303:
            csrf_for(c)
        return r

    # translator (user1) blocked from set + delete.
    assert login("user1", "user1").status_code == 303
    assert (
        c.post(
            "/setup-ai/keys/claude", data={"api_key": "sk-nope"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert c.post("/setup-ai/keys/claude/delete", follow_redirects=False).status_code == 403
    sign_out(c, follow_redirects=False)
    # linguist ALSO blocked: setting a key ENABLES real spend (same class of act
    # as raising the spend cap, which is admin-only — a linguist must not do it).
    sign_out(c, follow_redirects=False)
    assert login("linguist", "linguist").status_code == 303
    assert (
        c.post(
            "/setup-ai/keys/claude", data={"api_key": "sk-nope"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert c.post("/setup-ai/keys/claude/delete", follow_redirects=False).status_code == 403
    sign_out(c, follow_redirects=False)
    # admin allowed (set + delete).
    assert login("admin", "admin").status_code == 303
    assert (
        c.post(
            "/setup-ai/keys/openai", data={"api_key": "sk-admin-ok-8080"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert cloud_keys.stored_key("openai") == "sk-admin-ok-8080"
    assert c.post("/setup-ai/keys/openai/delete", follow_redirects=False).status_code == 303
    assert cloud_keys.stored_key("openai") == ""
    sign_out(c, follow_redirects=False)
    # restore auth OFF for any following test / clean baseline.
    settings.set_many({"security_auth_required": "0"})
    print("  RBAC: translator + linguist 403 on set/delete; admin allowed OK")


def test_cli_set_show_clear():
    _clear_all()
    from app.cli.__main__ import main

    secret = "sk-cli-claude-c0ffee99"
    assert main(["providers", "key", "set", "claude", "--key", secret]) == 0
    assert cloud_keys.source("claude") == "app" and cloud_keys.last4("claude") == "ee99"
    # show (human) — never prints the key, shows source + fingerprint.
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["providers", "key", "show"]) == 0
    out = buf.getvalue()
    assert secret not in out and "app" in out and "ee99" in out
    # show (--json) — never contains the key value.
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["--json", "providers", "key", "show"]) == 0
    jout = buf.getvalue()
    assert secret not in jout and '"last4": "ee99"' in jout
    # `config list` must not leak the ciphertext/key either (kept off DEFAULTS).
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["config", "list"])
    assert secret not in buf.getvalue() and "cloud_key_claude_enc" not in buf.getvalue()
    # clear.
    assert main(["providers", "key", "clear", "claude"]) == 0
    assert cloud_keys.source("claude") == "none"
    # unknown provider -> argparse usage error (SystemExit 2), never stored.
    try:
        main(["providers", "key", "set", "bogus", "--key", "x"])
        raise AssertionError("a bad provider must be rejected by argparse")
    except SystemExit as e:
        assert e.code == 2
    print(
        "  CLI set/show(human+json)/clear: never echo key; config list clean; bad provider rejected OK"  # noqa: E501
    )


def test_cli_parity_registration():
    from app.cli.__main__ import build_parser

    def leaves(parser, prefix=()):
        subs = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
        if not subs:
            yield " ".join(prefix)
            return
        for a in subs:
            for name, sub in a.choices.items():
                yield from leaves(sub, prefix + (name,))  # noqa: RUF005

    paths = set(leaves(build_parser()))
    for leaf in ("providers key set", "providers key show", "providers key clear"):
        assert leaf in paths, f"missing CLI leaf: {leaf}"
    print("  CLI parity: providers key set/show/clear registered as real leaves OK")


if __name__ == "__main__":
    test_encrypted_at_rest_and_roundtrip()
    test_precedence_and_liveness()
    test_trim_whitespace_and_quotes()
    test_audit_has_last4_only()
    test_web_routes_no_leak_and_immediate_effect()
    test_rbac_translator_blocked_admin_ok()
    test_cli_set_show_clear()
    test_cli_parity_registration()
    print("CLOUD_KEYS OK")
