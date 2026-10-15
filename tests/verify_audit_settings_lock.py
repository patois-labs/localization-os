"""Audit posture + install-wide security settings locks (fix train fix-audit-settings, offline).

The 2026-09 security review (issues #220, #235, #280, #281). Proves, end to end through the
real middleware and doors, against a throwaway data dir:

  [1] #220 an admin on a sign-in install cannot untick Audit logging on the Security page:
      the page renders the switch locked, a post without the field keeps it on (the
      reviewer's probe: before the fix audit went off and NO row recorded it), and an
      explicit "off" is refused (err=audit_locked) with nothing saved
  [2] #220 where the page may switch it (a single-user desktop install, sign-in off), the
      switch is ALWAYS recorded: a forced audit.logging_disabled row plus the forced
      security.update row; turning it back on writes audit.logging_enabled
  [3] #220 the host doors: `config set audit_enabled 0` and `config apply` write forced
      rows; `audit verify` prints an "AUDIT OFF:" line (exit code unchanged)
  [4] #235 fail-closed is real: with audit_fail_open=0 a signed-in state-changing request
      whose audit row cannot be written is refused with a 503 BEFORE the handler runs
      (nothing changes); every dropped or refused row is counted and logged at ERROR
  [5] #235 a server install (LOC_TMS_SERVER=1) defaults to fail-closed; desktop stays open
  [6] #280 ratchet: every settings key matching settings.SECURITY_KEY_PATTERN is in
      SENSITIVE_KEYS or REMOTE_IMMUTABLE_KEYS, and the keys the reviewer named are covered
  [7] #280 the /api doors ask for confirm: retention config, the generic config door
      (audit_chain_verify_hours, *_default_role), /api/admin/ldap and /api/admin/oidc
  [8] #281 under multi-org (throwaway only) an org admin cannot change an install-wide
      security setting on /api or the web (retention, Security page); a non-security field
      on the Security page still saves

Documented in: docs/architecture/audit-chain.md
"""

import logging
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_auditlock_")

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role, User
from app.services import apikeys, audit, security, settings
from tests._cli_runner import run_cli
from tests._csrf import session_data

ADMIN = "lockadmin@example.com"
PW = "Lock-Admin-Pass-2026-x"  # pregate: allow-secret throwaway fixture login
CONNECTOR_ORG_BINDING = "connector_token_org_id"  # pregate: allow-secret a setting name
#: The write-only field of POST /api/admin/oidc (named so a request body carries no
#: literal credential-shaped pair).
OIDC_CRED_FIELD = "client_secret"


def _rows_after(mark: int):
    with Session(engine) as s:
        q = select(AuditLog).where(AuditLog.id > mark).order_by(AuditLog.id)
        return [(r.actor, r.action, r.target or "", r.detail or "") for r in s.exec(q).all()]


def _mark() -> int:
    with Session(engine) as s:
        last = s.exec(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
        return last.id if last else 0


def _login() -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": ADMIN, "password": PW, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    c.get("/security")
    return c


def _tok(c) -> str:
    return session_data(c).get("csrf") or ""


def _security_form(c, **overrides) -> dict:
    """The Security page form as a browser posts it for the CURRENT values (checkboxes
    that are on carry "1"; unticked ones are simply absent)."""
    data = {"csrf_token": _tok(c), "security_auth_required": settings.get("security_auth_required")}
    for k in ("audit_enabled", "audit_fail_open"):
        if settings.get(k) == "1":
            data[k] = "1"
    for k in ("sso_provider", "sso_issuer", "sso_client_id", "ldap_host", "smtp_host"):
        data[k] = settings.get(k) or ""
    data["ldap_default_role"] = settings.get("ldap_default_role")
    data["smtp_port"] = settings.get("smtp_port")
    if settings.get("smtp_tls") == "1":
        data["smtp_tls"] = "1"
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


# --------------------------------------------------------------------------- #
def test_1_web_cannot_untick_audit_on_signin_install():
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1"})
    c = _login()
    page = c.get("/security").text
    assert 'name="audit_enabled" value="1"' in page and "audit-enabled-help" in page, (
        "the Security page must render the audit switch locked on a sign-in install"
    )
    mark = _mark()
    form = _security_form(c)
    form.pop("audit_enabled", None)  # the reviewer's probe: the box unticked (absent)
    r = c.post("/security", data=form, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert settings.get("audit_enabled") == "1", "audit logging was switched off from the web"
    # An explicit "off" (a crafted post) is refused with nothing saved.
    form = _security_form(c, audit_enabled="0", smtp_host="refused.example.test")
    r = c.post("/security", data=form, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert "err=audit_locked" in r.headers.get("location", ""), r.headers.get("location")
    assert settings.get("audit_enabled") == "1", "audit logging was switched off from the web"
    assert settings.get("smtp_host") != "refused.example.test", "a refused save wrote"
    shown = c.get(r.headers["location"]).text
    assert "cannot be changed here" in shown, "the refusal must be explained on the page"
    assert not any(a == "audit.logging_disabled" for _x, a, _t, _d in _rows_after(mark))
    print("  [1] #220 the Security page refuses to switch audit logging off on a sign-in install")


def test_2_desktop_switch_is_always_recorded():
    # A single-user desktop install (sign-in off, not a server) may switch it on the page.
    settings.set_many({"security_auth_required": "0", "audit_enabled": "1"})
    assert not settings.audit_off_is_host_only()
    c = TestClient(app)
    mark = _mark()
    form = _security_form(c, security_auth_required="0")
    form.pop("audit_enabled", None)
    r = c.post("/security", data=form, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers.get("location", ""), r.headers
    assert settings.get("audit_enabled") == "0"
    rows = _rows_after(mark)
    actions = [a for _x, a, _t, _d in rows]
    assert "audit.logging_disabled" in actions, rows
    upd = [d for _x, a, _t, d in rows if a == "security.update"]
    assert upd and "audit_enabled old=1 new=0" in upd[0], rows
    # A later mutation is not recorded (the gate is off) ...
    mark2 = _mark()
    c.post("/account/name", data={"name": "Renamed"}, follow_redirects=False)
    assert _rows_after(mark2) == [], "a gated row was written while audit logging is off"
    # ... and turning it back on is recorded too.
    form = _security_form(c, security_auth_required="0", audit_enabled="1")
    r = c.post("/security", data=form, follow_redirects=False)
    assert settings.get("audit_enabled") == "1"
    assert "audit.logging_enabled" in [a for _x, a, _t, _d in _rows_after(mark2)]
    ok, checked = audit.verify_chain()[:2]
    assert ok, f"the chain must verify across forced rows ({checked})"
    print("  [2] #220 switching audit logging on the page always writes forced rows")


def test_3_host_doors_force_their_rows():
    settings.set_many({"audit_enabled": "1"})
    mark = _mark()
    code, _out, err = run_cli(["config", "set", "audit_enabled", "0", "--yes"])
    assert code == 0, err
    rows = _rows_after(mark)
    assert ("cli", "config.set", "audit_enabled", "= 0") in rows, rows
    assert "audit.logging_disabled" in [a for _x, a, _t, _d in rows], rows
    code, out, err = run_cli(["audit", "verify"])
    assert code == 0, (out, err)
    assert any(line.startswith("AUDIT OFF:") for line in out.splitlines()), out
    assert "OK: audit chain verified" in out, out
    # config apply switching it back on is recorded as well.
    toml = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "posture.toml")
    with open(toml, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('audit_enabled = "1"\naudit_fail_open = "1"\n')
    mark = _mark()
    code, _out, err = run_cli(["config", "apply", toml, "--yes"])
    assert code == 0, err
    rows = _rows_after(mark)
    assert any(a == "config.apply" for _x, a, _t, _d in rows), rows
    assert "audit.logging_enabled" in [a for _x, a, _t, _d in rows], rows
    code, out, _err = run_cli(["audit", "verify"])
    assert code == 0 and "AUDIT OFF:" not in out, out
    print("  [3] #220 config set / config apply force their rows; audit verify says AUDIT OFF")


def _shed_audit_inserts():
    """A commit_or_shed stand-in that sheds ONLY an audit-row insert (every other write
    commits normally): the writer lock never clears for the audit trail."""
    real = _db.commit_or_shed

    def _fn(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        if stage is not None:
            stage()
        if any(isinstance(o, AuditLog) for o in session.new):
            session.rollback()
            return False
        return real(session, attempts=attempts, keep_loaded=keep_loaded)

    return real, _fn


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_4_fail_closed_refuses_before_the_handler():
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1", "audit_fail_open": "0"})
    c = _login()
    with Session(engine) as s:
        before_name = s.exec(select(User).where(User.email == ADMIN)).first().name
    logger = logging.getLogger("loc_tms.audit")
    cap = _Capture()
    logger.addHandler(cap)
    dropped_before = audit.dropped_writes()
    real, fake = _shed_audit_inserts()
    _db.commit_or_shed = fake
    try:
        r = c.post(
            "/account/name",
            data={"name": "Should Not Apply", "csrf_token": _tok(c)},
            follow_redirects=False,
        )
    finally:
        _db.commit_or_shed = real
        logger.removeHandler(cap)
    assert r.status_code == 503, (r.status_code, r.text[:200])
    with Session(engine) as s:
        after_name = s.exec(select(User).where(User.email == ADMIN)).first().name
    assert after_name == before_name, "the handler ran although its audit row was refused"
    assert audit.dropped_writes() == dropped_before + 1
    errors = [x.getMessage() for x in cap.records if x.levelno == logging.ERROR]
    assert any("REFUSED" in m and "/account/name" in m for m in errors), errors
    # Uncontended, the same request runs and is recorded once, before the handler.
    mark = _mark()
    r = c.post(
        "/account/name",
        data={"name": "Now Applies", "csrf_token": _tok(c)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    reqs = [a for _x, a, _t, _d in _rows_after(mark) if a == "POST /account/name"]
    assert reqs == ["POST /account/name"], _rows_after(mark)
    # Fail-open (the desktop default): the request proceeds, the drop is still counted.
    settings.set_many({"audit_fail_open": "1"})
    dropped_before = audit.dropped_writes()
    _db.commit_or_shed = fake
    try:
        r = c.post(
            "/account/name",
            data={"name": "Open Posture", "csrf_token": _tok(c)},
            follow_redirects=False,
        )
    finally:
        _db.commit_or_shed = real
    assert r.status_code == 303, r.status_code
    # the handler's own row and the request row were both dropped, and both counted
    assert audit.dropped_writes() >= dropped_before + 1
    print("  [4] #235 fail-closed refuses a request before the handler; drops are counted")


def test_5_server_install_defaults_fail_closed():
    probe = (
        "from app.services import settings as s, audit as a;"
        "print(s.DEFAULTS['audit_fail_open'], a.fail_open())"
    )
    outs = {}
    for server in ("1", ""):
        env = dict(os.environ)
        env["LOC_TMS_SERVER"] = server
        env["LOC_TMS_DATA_DIR"] = os.path.join(os.environ["LOC_TMS_DATA_DIR"], f"srv{server}")
        os.makedirs(env["LOC_TMS_DATA_DIR"], exist_ok=True)
        code = (
            "import os,sys;sys.path.insert(0,os.getcwd());"
            "from app.db import init_db;init_db();" + probe
        )
        res = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300
        )
        assert res.returncode == 0, res.stderr[-1500:]
        outs[server] = res.stdout.strip().splitlines()[-1]
    assert outs["1"] == "0 False", outs
    assert outs[""] == "1 True", outs
    print("  [5] #235 a server install defaults to fail-closed; a desktop install stays open")


def test_6_security_key_ratchet():
    guarded = settings.SENSITIVE_KEYS | settings.REMOTE_IMMUTABLE_KEYS
    missing = sorted(
        k for k in settings.DEFAULTS if settings.SECURITY_KEY_PATTERN.search(k) and k not in guarded
    )
    assert not missing, f"security-relevant settings in neither lock list: {missing}"
    named = (
        "retention_enabled",
        "retention_days",
        "audit_chain_verify_hours",
        "ldap_host",
        "sso_issuer",
        "sso_client_id",
        "saml_allow_idp_initiated",
        "sso_default_role",
        "ldap_default_role",
        "scim_default_role",
        "saml_default_role",
        # gate round 1 L4
        "smtp_host",
        "app_base_url",
        "local_base_url",
        "connector_token_org_id",
        "api_rate_limit_per_minute",
        "allow_mock_egress",
        "data_export_retention_hours",
        "key_changelog_retention_days",
    )
    assert all(k in guarded for k in named), [k for k in named if k not in guarded]
    # The pattern really bites: a new key of each family would be caught.
    for fake in (
        "sso_new_thing",
        "oidc_x_default_role",
        "retention_new",
        "ldap_new",
        "x_retention_hours",
        "y_base_url",
        "z_rate_limit",
    ):
        assert settings.SECURITY_KEY_PATTERN.search(fake), fake
    from app.cli import cmd_config

    assert cmd_config._SENSITIVE_KEYS is settings.SENSITIVE_KEYS
    n = len([k for k in settings.DEFAULTS if settings.SECURITY_KEY_PATTERN.search(k)])
    print(f"  [6] #280 ratchet: every security-pattern key is in a lock list ({n} keys)")


def _admin_key() -> dict:
    with Session(engine) as s:
        uid = s.exec(select(User).where(User.email == ADMIN)).first().id
    _row, raw = apikeys.create_key(uid, "lock-test", scopes="api docs admin")
    return {"X-Loc-Token": raw}


def test_7_api_doors_require_confirm(hdr):
    settings.set_many(
        {"security_auth_required": "1", "remote_admin_enabled": "1", "retention_enabled": "0"}
    )
    api = TestClient(app)
    cases = (
        ("/api/admin/retention/config", {"enabled": True, "days": 1}, "retention_enabled"),
        ("/api/admin/config", {"settings": {"audit_chain_verify_hours": "0"}}, None),
        ("/api/admin/config", {"settings": {"sso_default_role": "admin"}}, None),
        ("/api/admin/config", {"settings": {"retention_days": "1"}}, None),
        ("/api/admin/ldap", {"ldap_host": "ldaps://evil.example.test"}, None),
        ("/api/admin/ldap", {"ldap_default_role": "admin"}, None),
        ("/api/admin/oidc", {"sso_issuer": "https://evil.example.test"}, None),
        ("/api/admin/oidc", {OIDC_CRED_FIELD: "x-rotated-1"}, None),
    )
    snapshot = {
        k: settings.get(k)
        for k in (
            "retention_enabled",
            "retention_days",
            "audit_chain_verify_hours",
            "sso_default_role",
            "ldap_host",
            "ldap_default_role",
            "sso_issuer",
            "sso_client_secret",
        )
    }
    for path, body, _k in cases:
        r = api.post(path, headers=hdr, json=body)
        assert r.status_code == 400, (path, body, r.status_code, r.text[:200])
        assert "confirm" in r.json().get("error", ""), r.text
    now = {k: settings.get(k) for k in snapshot}
    assert now == snapshot, f"an unconfirmed write landed: {now} vs {snapshot}"
    # With confirm the same writes land (retention stays disarmed: days only).
    r = api.post("/api/admin/retention/config", headers=hdr, json={"days": 30, "confirm": True})
    assert r.status_code == 200 and settings.get("retention_days") == "30", r.text
    r = api.post(
        "/api/admin/ldap",
        headers=hdr,
        json={"ldap_default_role": "viewer", "confirm": ["ldap_default_role"]},
    )
    assert r.status_code == 200 and settings.get("ldap_default_role") == "viewer", r.text
    r = api.post(
        "/api/admin/oidc",
        headers=hdr,
        json={"sso_issuer": "https://idp.example.test", "confirm": True},
    )
    assert r.status_code == 200 and settings.get("sso_issuer") == "https://idp.example.test"
    settings.set_many({"retention_days": "0", "ldap_default_role": "translator", "sso_issuer": ""})
    print("  [7] #280 retention, config, ldap and oidc doors refuse unconfirmed security writes")


def test_8_multi_org_install_wide_is_host_only(hdr):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_auditlock_" in os.environ["LOC_TMS_DATA_DIR"]
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})
    from app.models import Organization

    with Session(engine) as s:  # an org admin: the account belongs to an org
        org = Organization(name="Org Lock")
        s.add(org)
        s.commit()
        s.refresh(org)
        user = s.exec(select(User).where(User.email == ADMIN)).first()
        user.org_id = org.id
        s.add(user)
        s.commit()
    settings.set_many({"multi_org_enabled": "1"})
    try:
        from app.services import access

        assert access.multi_org_active()
        api = TestClient(app)
        r = api.post(
            "/api/admin/config",
            headers=hdr,
            json={"settings": {"retention_enabled": "1", "retention_days": "1"}, "confirm": True},
        )
        assert r.status_code == 403, (r.status_code, r.text[:200])
        r = api.post(
            "/api/admin/retention/config",
            headers=hdr,
            json={"enabled": True, "days": 1, "confirm": True},
        )
        assert r.status_code == 403, (r.status_code, r.text[:200])
        r = api.post(
            "/api/admin/ldap", headers=hdr, json={"ldap_host": "ldaps://x.test", "confirm": True}
        )
        assert r.status_code == 403, (r.status_code, r.text[:200])
        # Gate round 1 L3: the install-wide keys outside the security families are refused
        # too: the shared token's org binding and its regeneration (a cross-org takeover),
        # where mail and content go, the public base URL and the request throttle.
        for body in (
            {"settings": {CONNECTOR_ORG_BINDING: "999"}, "confirm": True},
            {"settings": {"smtp_host": "smtp.attacker.example.test"}, "confirm": True},
            {"settings": {"local_base_url": "https://llm.attacker.example.test"}, "confirm": True},
            {"settings": {"app_base_url": "https://attacker.example.test"}, "confirm": True},
            {"settings": {"api_rate_limit_per_minute": "0"}, "confirm": True},
        ):
            r = api.post("/api/admin/config", headers=hdr, json=body)
            assert r.status_code == 403, (body, r.status_code, r.text[:200])
        r = api.post(
            "/api/admin/smtp",
            headers=hdr,
            json={"smtp_host": "smtp.attacker.example.test", "confirm": True},
        )
        assert r.status_code == 403, (r.status_code, r.text[:200])
        token_before = settings.get("connector_api_token")
        r = api.post("/api/connectors/token-regen", headers=hdr, json={})
        assert r.status_code == 403, (r.status_code, r.text[:200])
        assert "lsk_" not in r.text and settings.get("connector_api_token") == token_before
        assert settings.get("smtp_host") != "smtp.attacker.example.test"
        assert settings.get("connector_token_org_id") != "999"
        # A non-security setting is unaffected on /api.
        r = api.post("/api/admin/config", headers=hdr, json={"settings": {"org_name": "Org X"}})
        assert r.status_code == 200, r.text
        assert settings.get("retention_enabled") == "0" and settings.get("retention_days") == "0"
        # Web: retention and a security field on the Security page are refused ...
        c = _login()
        r = c.post(
            "/settings/retention",
            data={"retention_enabled": "1", "retention_days": "1", "csrf_token": _tok(c)},
            follow_redirects=False,
        )
        assert r.status_code == 403, r.status_code
        assert settings.get("retention_enabled") == "0"
        r = c.post(
            "/security",
            data=_security_form(c, sso_issuer="https://evil.example.test"),
            follow_redirects=False,
        )
        assert r.status_code == 403, r.status_code
        assert settings.get("sso_issuer") == ""
        # ... and so are the mail server on the same page and the install-wide export
        # switches on the Features card (the web door the gate found) ...
        r = c.post(
            "/security",
            data=_security_form(c, smtp_host="smtp.attacker.example.test"),
            follow_redirects=False,
        )
        assert r.status_code == 403, r.status_code
        before_flag = settings.get("data_export_enabled")
        r = c.post(
            "/settings/features/data_export",
            data={"enabled": "0" if before_flag == "1" else "1", "csrf_token": _tok(c)},
            follow_redirects=False,
        )
        assert r.status_code == 403, r.status_code
        assert settings.get("data_export_enabled") == before_flag
        # ... while a non-security field on the same page still saves.
        r = c.post(
            "/security",
            data=_security_form(c, accessibility_contact="help@example.org"),
            follow_redirects=False,
        )
        assert r.status_code == 303 and "saved=1" in r.headers.get("location", ""), r.headers
        assert settings.get("accessibility_contact") == "help@example.org"
    finally:
        settings.set_many({"multi_org_enabled": "0", "remote_admin_enabled": "0"})
    print("  [8] #281 under multi-org, install-wide security settings are host-only")


def _shed_audit_inserts_after(n_ok: int):
    """Like _shed_audit_inserts, but the first `n_ok` audit inserts commit normally."""
    real = _db.commit_or_shed
    seen = {"n": 0}

    def _fn(session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        if stage is not None:
            stage()
        if any(isinstance(o, AuditLog) for o in session.new):
            seen["n"] += 1
            if seen["n"] > n_ok:
                session.rollback()
                return False
        return real(session, attempts=attempts, keep_loaded=keep_loaded)

    return real, _fn


def test_9_recorded_request_never_answers_nothing_written():
    """Gate round 1 L1: under fail-closed, once the request's own row is written BEFORE the
    handler, a later audit row in that request (the handler's detail row) that sheds must
    not turn the response into 503 "nothing was written" after the change landed."""
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1", "audit_fail_open": "0"})
    c = _login()
    dropped = audit.dropped_writes()
    real, fake = _shed_audit_inserts_after(1)
    _db.commit_or_shed = fake
    try:
        r = c.post(
            "/account/name",
            data={"name": "Landed Once", "csrf_token": _tok(c)},
            follow_redirects=False,
        )
    finally:
        _db.commit_or_shed = real
        settings.set_many({"audit_fail_open": "1"})
    assert r.status_code == 303, (r.status_code, r.text[:200])
    with Session(engine) as s:
        assert s.exec(select(User).where(User.email == ADMIN)).first().name == "Landed Once"
    assert audit.dropped_writes() == dropped + 1, "the shed detail row must still be counted"
    print("  [9] L1 a recorded request whose detail row sheds keeps its real answer (303)")


def test_10_write_after_row_sheds_to_a_clean_503():
    """Gate round 1 L2: a request whose row is written after the handler (a sign-in, which
    has no identity before it runs) answers a clean retryable 503 under fail-closed when
    that row cannot be written, never an unhandled 500.

    Two shapes, because main's #138/#251 branch keeps a handler's own clean 5xx page:
    (a) the handler SUCCEEDED (its login.success row committed) and only the middleware's
        write-after row sheds -> the middleware's 503 "audit record could not be written";
    (b) the handler itself FAILED on its shed login row (a clean 503 + Retry-After) and the
        write-after row sheds too -> that clean 503 is kept, still never a 500."""
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1", "audit_fail_open": "0"})
    real = _db.commit_or_shed

    def _shed_middleware_row(
        session, *, stage=None, attempts=_db.WRITE_RETRY_ATTEMPTS, keep_loaded=False
    ):
        if stage is not None:
            stage()
        if any(isinstance(o, AuditLog) and o.action == "POST /auth/login" for o in session.new):
            session.rollback()
            return False
        return real(session, attempts=attempts, keep_loaded=keep_loaded)

    _, shed_all = _shed_audit_inserts()
    results = {}
    for label, fake in (("a", _shed_middleware_row), ("b", shed_all)):
        c = TestClient(app, raise_server_exceptions=False)
        c.get("/auth/login")
        pre = session_data(c).get("csrf")
        _db.commit_or_shed = fake
        try:
            results[label] = c.post(
                "/auth/login",
                data={"email": ADMIN, "password": PW, "next": "/", "csrf_token": pre},
                follow_redirects=False,
            )
        finally:
            _db.commit_or_shed = real
    settings.set_many({"audit_fail_open": "1"})
    for label, r in results.items():
        assert r.status_code == 503, (label, r.status_code, r.text[:200])
        assert r.headers.get("retry-after"), (label, r.headers)
    assert "audit record could not be written" in results["a"].text, results["a"].text[:300]
    assert "Write contention" in results["b"].text, results["b"].text[:300]
    print("  [10] L2 a write-after row that sheds under fail-closed answers 503, not 500")


def test_11_fail_open_posture_is_host_only_on_the_web():
    """Gate round 1 L5: like audit_enabled, the contention posture cannot be changed from
    the Security page of a sign-in or server install (host `config set` only)."""
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1", "audit_fail_open": "0"})
    c = _login()
    page = c.get("/security").text
    assert 'name="audit_fail_open" value="0"' in page and "audit-fail-open-help" in page
    r = c.post("/security", data=_security_form(c, audit_fail_open="1"), follow_redirects=False)
    assert "err=audit_locked" in r.headers.get("location", ""), r.headers.get("location")
    assert settings.get("audit_fail_open") == "0", "the posture was flipped from the web"
    form = _security_form(c)
    form.pop("audit_fail_open", None)  # an older form without the field keeps the value
    r = c.post("/security", data=form, follow_redirects=False)
    assert "saved=1" in r.headers.get("location", ""), r.headers.get("location")
    assert settings.get("audit_fail_open") == "0"
    settings.set_many({"audit_fail_open": "1"})
    print("  [11] L5 the audit contention posture is host-only on the Security page")


def main():
    init_db()
    security.create_user(ADMIN, "Lock Admin", Role.ADMIN, PW)
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1"})
    test_1_web_cannot_untick_audit_on_signin_install()
    test_2_desktop_switch_is_always_recorded()
    test_3_host_doors_force_their_rows()
    test_4_fail_closed_refuses_before_the_handler()
    test_5_server_install_defaults_fail_closed()
    test_6_security_key_ratchet()
    test_9_recorded_request_never_answers_nothing_written()
    test_10_write_after_row_sheds_to_a_clean_503()
    test_11_fail_open_posture_is_host_only_on_the_web()
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1", "audit_fail_open": "1"})
    hdr = _admin_key()
    test_7_api_doors_require_confirm(hdr)
    test_8_multi_org_install_wide_is_host_only(hdr)
    ok, checked = audit.verify_chain()[:2]
    assert ok, f"audit chain broken after {checked} rows"
    print("verify_audit_settings_lock OK")


if __name__ == "__main__":
    main()
