"""Enterprise turn-on UX verification (offline, hermetic).

Proves app/services/enterprise_enablement.py + the /settings/enterprise web panel + the
`enterprise` CLI group + the MOVE of the scim/saml/pii toggles under the one door:

  A  PREFLIGHT red/green per feature, run against PROPOSED values, with the optional libs
     MONKEYPATCHED absent so the suite is green whether or not presidio/otel/etc. happen to
     be installed in the venv (a missing extra IS a legit preflight-red).
  B  ENABLE gates on the OFFLINE preflight: refuse on red, --force overrides; the write
     flips the enable Setting + writes config fields + records an audit row; needs_restart.
  C  DISABLE never runs a preflight (turning off is never blocked) and flips the Setting off.
  D  OPERATOR STEP-UP: operator_key_configured + verify_operator_key (SHA-256, constant-time).
  E  WEB panel (login wall ON): GET 200; enable/disable REQUIRE the operator key (read-only
     when none is set; wrong key refused; right key writes); CSRF enforced; a red preflight
     is refused without the force checkbox; NO /api route exposes enablement/preflight.
  F  THE MOVE: /settings/features/{scim|saml} POST no longer flips them (redirects to the
     panel); POST /security no longer writes pii_ner_*; the registry entries survive.

Hermetic: throwaway LOC_TMS_DATA_DIR, offline MockProvider, ASCII-only prints. Final line:
ENTERPRISE ENABLEMENT OK.
"""

import hashlib
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_enterprise_")

FIXTURE_PW = "fixturepass1234"  # pregate: allow-secret test fixture

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import AuditLog
from app.services import enterprise_enablement as ee
from app.services import security, settings
from tests._csrf import csrf_for


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _reset(keys):
    settings.set_many({k: settings.DEFAULTS.get(k, "") for k in keys})


def _no_lib(*_names):
    return False


def _all_lib(*_names):
    return True


def _audit_actions_like(prefix):
    with Session(engine) as s:
        return [
            r.action for r in s.exec(select(AuditLog).where(AuditLog.action.like(prefix))).all()
        ]


# --------------------------------------------------------------------------- #
# A. preflight red/green
# --------------------------------------------------------------------------- #
def test_preflight_otel(monkeypatch):
    # The OpenTelemetry exporter ships in the paid add-on, not the open core: the preflight is
    # honestly RED with the add-on line, whatever libs or endpoint are proposed.
    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    pf = ee.preflight("otel", proposed={"otel_endpoint": "http://collector:4318/v1/traces"})
    assert not pf.ok and "not installed" in pf.reason, pf.reason
    assert "paid add-on, not the open core" in pf.reason, pf.reason
    print("  A. otel preflight: RED with the honest 'not installed (paid add-on)' line OK")


def test_preflight_metrics(monkeypatch):
    # Prometheus metrics ship in the paid add-on: RED with the honest line, even with the lib.
    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    pf = ee.preflight("metrics")
    assert not pf.ok and "paid add-on, not the open core" in pf.reason, pf.reason
    # the admin-scope reminder is ADVISORY -- present but never fails the gate on its own.
    assert any("(reminder)" in c.name for c in pf.checks)
    print("  A. metrics preflight: RED 'not installed (paid add-on)' + advisory scope reminder OK")


def test_preflight_saml(monkeypatch):
    monkeypatch.setattr(ee, "_lib_present", _no_lib)
    assert not ee.preflight("saml", proposed={"saml_idp_metadata_xml": "<x/>"}).ok
    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    # lib present but NO metadata -> red on the metadata-present check.
    pf = ee.preflight("saml", proposed={"saml_idp_metadata_xml": ""})
    assert not pf.ok and "metadata" in pf.reason.lower(), pf.reason
    # cert without key (or vice versa) -> the consistency check is red.
    pf = ee.preflight(
        "saml",
        proposed={
            "saml_idp_metadata_xml": "",
            "saml_sp_x509_cert": "CERT",
            "saml_sp_private_key": "",
        },
    )
    assert any("cert" in c.name.lower() and not c.ok for c in pf.checks), [
        c.name for c in pf.checks
    ]
    print("  A. saml preflight: lib gate + metadata-present + cert/key consistency OK")


def test_preflight_scim():
    _reset(["scim_enabled"])
    # no active admin -> red.
    assert not ee.preflight("scim").ok
    # create an active admin -> green.
    security.create_user("admin@x.com", "admin", role="admin", password=FIXTURE_PW)
    pf = ee.preflight("scim")
    assert pf.ok, pf.reason
    print("  A. scim preflight: no-admin RED, active-admin GREEN OK")


def test_preflight_pii_ner(monkeypatch):
    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    monkeypatch.setattr(ee, "_spacy_model_installed", lambda m: True)
    # valid entities -> green.
    assert ee.preflight("pii_ner", proposed={"pii_ner_entities": "PERSON,LOCATION"}).ok
    # a bogus entity name -> red (offline validation, no presidio needed).
    pf = ee.preflight("pii_ner", proposed={"pii_ner_entities": "PERSON,NOPE"})
    assert not pf.ok and "NOPE" in pf.reason, pf.reason
    # model not installed locally -> red naming spacy download.
    monkeypatch.setattr(ee, "_spacy_model_installed", lambda m: False)
    pf = ee.preflight("pii_ner", proposed={"pii_ner_spacy_model": "en_core_web_lg"})
    assert not pf.ok and "spacy download" in pf.reason.lower(), pf.reason
    # libs absent -> red on the extras check.
    monkeypatch.setattr(ee, "_lib_present", _no_lib)
    assert not ee.preflight("pii_ner").ok
    print("  A. pii_ner preflight: extras + local-model + entity-name validation OK")


def test_pii_ner_model_must_be_a_pipeline(monkeypatch):
    """GATE ROUND 1, probe C (PROBE-PROVEN). `spacy.util.is_package` is true for ANY
    installed distribution, so `pii_ner_spacy_model=pip` -- a plausible paste into the
    panel's free-text field -- was preflight-GREEN, accepted without --force, and then died
    at boot inside pii._model_lang -> spacy.info ("Can't find pipeline meta.json"), which
    wasabi reports as a SystemExit that pii's own `except Exception` does not catch."""
    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    for not_a_model in ("pip", "setuptools"):
        pf = ee.preflight("pii_ner", proposed={"pii_ner_spacy_model": not_a_model})
        assert not pf.ok, f"{not_a_model!r} is installed but is NOT a spaCy pipeline"
        assert "pipeline" in pf.reason.lower(), pf.reason
    # A name that is not installed at all stays red, and the seam degrades safely.
    assert not ee.preflight("pii_ner", proposed={"pii_ner_spacy_model": "xx_nope"}).ok
    assert ee._spacy_model_is_pipeline("pip") is False
    print("  A. pii_ner: an installed NON-pipeline package (pip) is RED, not green OK")


def test_preflight_jobqueue(monkeypatch):
    # the jobqueue table exists after init_db -> green.
    assert ee.preflight("jobqueue").ok
    # table absent -> red naming alembic upgrade head.
    monkeypatch.setattr(ee, "_table_exists", lambda name: False)
    pf = ee.preflight("jobqueue")
    assert not pf.ok and "alembic upgrade head" in pf.reason, pf.reason
    print("  A. jobqueue preflight: table-present GREEN, table-absent RED (alembic) OK")


def test_preflight_s3(monkeypatch):
    # The S3 blob/export store ships in the paid add-on: RED with the honest line even when
    # the proposed bucket / credentials are otherwise valid.
    from app import config

    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "AKIA", raising=False)
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "SEKRIT", raising=False)
    pf = ee.preflight(
        "s3", proposed={"blobstore_s3_bucket": "b", "blobstore_s3_credentials": "static"}
    )
    assert not pf.ok and "paid add-on, not the open core" in pf.reason, pf.reason
    print("  A. s3 preflight: RED with the honest 'not installed (paid add-on)' line OK")


# --------------------------------------------------------------------------- #
# B/C. enable / disable
# --------------------------------------------------------------------------- #
def test_enable_gates_on_preflight(monkeypatch):
    _reset(["otel_enabled", "otel_endpoint"])
    monkeypatch.setattr(ee, "_lib_present", _no_lib)
    # red preflight -> refuse without force, and NOTHING is written.
    try:
        ee.enable("otel", "tester", proposed={"otel_endpoint": "http://c:4318/v1/traces"})
        raise AssertionError("enable should refuse on a red preflight")
    except ee.EnterpriseError:
        pass
    assert settings.get("otel_enabled") == "0", "a refused enable must write nothing"
    # --force overrides the red preflight AND writes the config field + flips the flag.
    res = ee.enable(
        "otel", "tester", proposed={"otel_endpoint": "http://c:4318/v1/traces"}, force=True
    )
    assert res.action == "enable" and res.new_value == "1" and res.needs_restart
    assert settings.get("otel_enabled") == "1"
    assert settings.get("otel_endpoint") == "http://c:4318/v1/traces"
    assert "enterprise.enable" in _audit_actions_like("enterprise.%")
    print("  B. enable: red preflight REFUSED (no write); --force writes flag + fields + audit OK")


def test_enable_green_and_secret(monkeypatch):
    _reset(
        ["saml_enabled", "saml_idp_metadata_xml", "saml_sp_x509_cert", "saml_sp_private_key_enc"]
    )
    monkeypatch.setattr(ee, "_lib_present", _all_lib)
    # Monkeypatch the parse so we get a green metadata check without a real python3-saml.
    import app.services.enterprise_enablement as mod

    def _green_saml(proposed, run_network, for_gate):
        return [ee.Check("python3-saml importable", True), ee.Check("IdP metadata parses", True)]

    monkeypatch.setitem(mod._PREFLIGHTS, "saml", _green_saml)
    res = ee.enable(
        "saml",
        "tester",
        proposed={
            "saml_idp_metadata_xml": "<md/>",
            "saml_sp_private_key": "SECRETKEYMATERIAL",
        },
    )
    assert res.action == "enable" and settings.get("saml_enabled") == "1"
    assert settings.get("saml_idp_metadata_xml") == "<md/>"
    # The private key is stored Fernet-ENCRYPTED (not a plaintext row) + never logged raw.
    assert settings.read_secret_setting("saml_sp_private_key_enc") == "SECRETKEYMATERIAL"
    assert settings.get("saml_sp_private_key_enc") != "SECRETKEYMATERIAL"
    detail = " ".join(_audit_details_like("enterprise.%"))
    assert "SECRETKEYMATERIAL" not in detail, "a secret value must never reach the audit row"
    print("  B. enable: config fields + Fernet-at-rest secret; secret never audited OK")


def _audit_details_like(prefix):
    with Session(engine) as s:
        return [
            r.detail for r in s.exec(select(AuditLog).where(AuditLog.action.like(prefix))).all()
        ]


def test_disable_never_preflights(monkeypatch):
    settings.set_many({"otel_enabled": "1"})
    # A broken config (libs absent) must NOT block a disable.
    monkeypatch.setattr(ee, "_lib_present", _no_lib)
    res = ee.disable("otel", "tester")
    assert res.action == "disable" and settings.get("otel_enabled") == "0"
    # disable of an already-off feature is a clean no-op (still audited).
    res2 = ee.disable("otel", "tester")
    assert res2.action == "noop" and settings.get("otel_enabled") == "0"
    print("  C. disable: flips off with NO preflight, even when the config is broken OK")


def test_jobqueue_enable_uses_backend_value():
    _reset(["job_queue_backend"])
    res = ee.enable("jobqueue", "tester")
    assert res.new_value == "db" and settings.get("job_queue_backend") == "db"
    ee.disable("jobqueue", "tester")
    assert settings.get("job_queue_backend") == "memory"
    print("  C. jobqueue enable/disable uses db/memory (not 1/0) OK")


# --------------------------------------------------------------------------- #
# D. operator step-up
# --------------------------------------------------------------------------- #
def test_operator_step_up():
    _reset(["operator_key_hash"])
    assert ee.operator_key_configured() is False
    assert ee.verify_operator_key("anything") is False  # unset -> always False
    settings.set_many({"operator_key_hash": hashlib.sha256(b"topsecret").hexdigest()})
    assert ee.operator_key_configured() is True
    assert ee.verify_operator_key("topsecret") is True
    assert ee.verify_operator_key("wrong") is False
    assert ee.verify_operator_key("") is False
    print("  D. operator step-up: SHA-256 verify (unset/right/wrong/blank) OK")


# --------------------------------------------------------------------------- #
# E. web panel (login wall ON)
# --------------------------------------------------------------------------- #
def _web_client():
    from app.main import app

    if not any(u.email == "root@x.com" for u in security.list_users()):
        security.create_user("root@x.com", "root", role="admin", password=FIXTURE_PW)
    settings.set_many({"security_auth_required": "1"})
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": "root@x.com", "password": FIXTURE_PW, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    csrf_for(c)
    return c


def test_web_panel_and_step_up(monkeypatch):
    _reset(["operator_key_hash", "job_queue_backend", "otel_enabled", "otel_endpoint"])
    c = _web_client()
    assert c.get("/settings/enterprise").status_code == 200

    # No operator key configured -> enable is READ-ONLY (redirect w/ error, no write).
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "jobqueue"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert settings.get("job_queue_backend") == "memory", "read-only panel must not write"

    # Configure the operator key; a WRONG key is refused.
    settings.set_many({"operator_key_hash": hashlib.sha256(b"opk").hexdigest()})
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "jobqueue", "operator_key": "WRONG"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert settings.get("job_queue_backend") == "memory"

    # RIGHT key + a green feature -> enabled.
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "jobqueue", "operator_key": "opk"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=" in r.headers["location"]
    assert settings.get("job_queue_backend") == "db"

    # A RED preflight (otel, libs absent) is refused even with the right key + no force...
    monkeypatch.setattr(ee, "_lib_present", _no_lib)
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "otel", "operator_key": "opk", "otel_endpoint": "http://c:4318/v1/traces"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert settings.get("otel_enabled") == "0"
    # ...and ALLOWED with the force checkbox.
    r = c.post(
        "/settings/enterprise/enable",
        data={
            "feature": "otel",
            "operator_key": "opk",
            "force": "1",
            "otel_endpoint": "http://c:4318/v1/traces",
        },
        follow_redirects=False,
    )
    assert settings.get("otel_enabled") == "1"

    # disable also requires the operator key.
    r = c.post(
        "/settings/enterprise/disable",
        data={"feature": "otel", "operator_key": "opk"},
        follow_redirects=False,
    )
    assert settings.get("otel_enabled") == "0"
    print("  E. web: read-only w/o key, wrong-key refused, right-key enable, red+force, disable OK")


def test_step_up_refusal_is_audited():
    """GATE ROUND 1, probe B. The operator key is the only thing between an admin session
    and turning SSO / SCIM / data-topology switches on; a refused step-up used to leave NO
    trace, so guessing at it was invisible. Refusals are audited -- and the row must never
    carry the supplied value (a stored guess is a guess to replay)."""
    _reset(["operator_key_hash", "job_queue_backend"])
    settings.set_many({"operator_key_hash": hashlib.sha256(b"opk").hexdigest()})
    c = _web_client()
    before = len(_audit_actions_like("enterprise.step_up_refused"))
    guess = "please-let-me-in"
    for path in ("/settings/enterprise/enable", "/settings/enterprise/disable"):
        c.post(path, data={"feature": "scim", "operator_key": guess}, follow_redirects=False)
    rows = _audit_actions_like("enterprise.step_up_refused")
    assert len(rows) == before + 2, f"both refusals must be audited ({before} -> {len(rows)})"
    with Session(engine) as s:
        detail = " || ".join(
            f"{r.target}|{r.detail}"
            for r in s.exec(
                select(AuditLog).where(AuditLog.action == "enterprise.step_up_refused")
            ).all()
        )
    assert guess not in detail, "the audit row must NEVER carry the supplied key"
    assert "scim" in detail
    # A successful step-up still audits the CHANGE, not a refusal.
    n = len(_audit_actions_like("enterprise.step_up_refused"))
    c.post(
        "/settings/enterprise/enable",
        data={"feature": "jobqueue", "operator_key": "opk"},
        follow_redirects=False,
    )
    assert settings.get("job_queue_backend") == "db"
    assert len(_audit_actions_like("enterprise.step_up_refused")) == n
    _reset(["job_queue_backend"])
    print("  E. web: a refused operator-key step-up is audited (never the key itself) OK")


def test_web_csrf_enforced():
    _reset(["operator_key_hash", "job_queue_backend"])
    settings.set_many({"operator_key_hash": hashlib.sha256(b"opk").hexdigest()})

    c = _web_client()
    # A WRONG CSRF token (fetch-style header) is rejected 403 by the global CSRF wall.
    c.headers["X-CSRF-Token"] = "bogus-token"
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "jobqueue", "operator_key": "opk"},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.status_code
    # A form POST with NO token at all is diverted to re-login (303), never processed.
    c.headers.pop("X-CSRF-Token", None)
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "jobqueue", "operator_key": "opk"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/auth/login" in r.headers["location"], r.status_code
    assert settings.get("job_queue_backend") == "memory"
    print("  E. web: CSRF wall refuses a wrong token (403) + a token-less form (re-login) OK")


def test_no_api_or_mcp_surface():
    from app.main import app
    from app.web._routes import walk_routes

    paths = {getattr(r, "path", "") for r in walk_routes(app)}
    leak = [p for p in paths if p.startswith("/api") and "enterprise" in p]
    assert not leak, f"enablement must NOT be on /api: {leak}"
    # The panel routes are web-only under /settings/enterprise.
    assert "/settings/enterprise" in paths
    print("  E. no /api enablement route exists (host-only panel) OK")


# --------------------------------------------------------------------------- #
# F. the MOVE
# --------------------------------------------------------------------------- #
def test_panel_fields_are_not_wire_writable():
    """GATE ROUND 1, probe A (PROBE-PROVEN). The panel gates these fields behind the
    operator-key step-up; /api must not be a second door with a weaker story.

    ``saml_idp_metadata_xml`` is the SAML TRUST ANCHOR (entityId + SSO URL + the x509 cert
    every assertion is validated against). Before the fix, an admin-scoped API key on a
    remote_admin_enabled host wrote it via POST /api/admin/config -> HTTP 200, which on a
    SAML-on install is a complete authentication bypass. ``blobstore_s3_prefix`` was a
    remote HTTP-200 that bricks the next boot (_normalize_prefix refuses it)."""
    for key in (
        "saml_idp_metadata_xml",
        "saml_sp_x509_cert",
        "saml_sp_private_key_enc",
        "blobstore_s3_prefix",
    ):
        assert key in settings.REMOTE_IMMUTABLE_KEYS, (
            f"{key!r} is operator-key-gated on /settings/enterprise but WIRE-WRITABLE over "
            "/api/admin/config -- two doors, two auth stories. Keep it REMOTE_IMMUTABLE."
        )
    # Every enable Setting the panel flips must be host-only too (the sprint invariant).
    for f in ee.FEATURES:
        assert f.setting_key in settings.REMOTE_IMMUTABLE_KEYS, f.setting_key
    print("  E. panel-gated SAML trust material + s3 prefix are REMOTE_IMMUTABLE OK")


def test_move_scim_saml_toggle_redirects():
    _reset(["scim_enabled", "saml_enabled", "security_auth_required"])
    c = _web_client()
    for slug in ("scim", "saml"):
        r = c.post(
            f"/settings/features/{slug}",
            data={"enabled": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "/settings/enterprise" in r.headers["location"], slug
        # The Feature-card toggle no longer flips the switch -- the panel is the only door.
        assert settings.get(f"{slug}_enabled") == "0", slug
    # The registry entry still EXISTS (route/CLI gating unchanged) -- the feature is real.
    from app import features

    assert features.feature_by_slug("scim") is not None
    assert features.feature_by_slug("saml") is not None
    print("  F. move: /settings/features/{scim,saml} redirect to the panel + write nothing OK")


def test_config_fields_editable_while_on():
    """GATE ROUND 1, probe D (PROBE-PROVEN regression from the MOVE). /security let an admin
    edit pii_ner_spacy_model / pii_ner_entities WHILE PII NER WAS ON; the panel took the
    field over and rendered it only while the feature was OFF -- behavior lost in the move.
    Worse for SAML: when an IdP rotates its signing cert, reaching saml_idp_metadata_xml
    would have required DISABLING SSO first, locking every federated user out.

    The write still goes through the same operator-key step-up (it is the enable verb)."""
    _reset(["operator_key_hash", "pii_ner_enabled", "pii_ner_spacy_model", "saml_enabled"])
    settings.set_many(
        {
            "operator_key_hash": hashlib.sha256(b"opk").hexdigest(),
            "pii_ner_enabled": "1",
            "saml_enabled": "1",
        }
    )
    c = _web_client()
    html = c.get("/settings/enterprise").text
    for fld in ("pii_ner_spacy_model", "pii_ner_entities", "saml_idp_metadata_xml"):
        assert f'name="{fld}"' in html, f"{fld} must stay editable while the feature is ON"
    assert "/settings/enterprise/disable" in html, "the disable form must still render"

    # ...and the save STILL needs the operator key (no step-up hole in the while-on path).
    r = c.post(
        "/settings/enterprise/enable",
        data={"feature": "pii_ner", "pii_ner_spacy_model": "sneaky", "operator_key": "WRONG"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert settings.get("pii_ner_spacy_model") != "sneaky", "while-on save must obey the step-up"
    good = {"feature": "pii_ner", "pii_ner_spacy_model": "en_core_web_sm", "operator_key": "opk"}
    if importlib.util.find_spec("spacy") is None:
        # Without spaCy (the default install) the boot-brick preflight is red; the form's
        # 'force' box is how an operator saves anyway. The step-up check above is unchanged.
        good["force"] = "1"
    r = c.post("/settings/enterprise/enable", data=good, follow_redirects=False)
    assert settings.get("pii_ner_spacy_model") == "en_core_web_sm", r.headers.get("location")
    assert settings.get("pii_ner_enabled") == "1", "a config save must not toggle the feature"
    assert "saved=" in r.headers["location"]
    _reset(["pii_ner_enabled", "pii_ner_spacy_model", "saml_enabled"])
    print("  F. move: config fields stay editable WHILE ON, still behind the step-up OK")


def test_move_security_no_longer_writes_pii_ner():
    _reset(["pii_ner_enabled", "pii_ner_entities", "security_auth_required"])
    c = _web_client()
    # POST /security with a stale pii_ner_enabled field must NOT flip it (the write moved).
    r = c.post(
        "/security",
        data={
            "security_auth_required": "1",
            "audit_enabled": "1",
            "pii_ner_enabled": "1",
            "pii_ner_entities": "PERSON,LOCATION",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert settings.get("pii_ner_enabled") == "0", "the /security form no longer writes pii_ner_*"
    assert settings.get("pii_ner_entities") == "PERSON"
    print("  F. move: POST /security no longer writes pii_ner_* (moved to the panel) OK")


def main() -> None:
    class _MP:
        """Tiny monkeypatch shim so main() can drive the test fns outside pytest."""

        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value=None, raising=True):
            if value is None and not isinstance(target, str):
                raise ValueError("use setattr(obj, 'name', value)")
            old = getattr(target, name, None)
            self._undo.append(("attr", target, name, old, hasattr(target, name)))
            setattr(target, name, value)

        def setitem(self, dic, key, value):
            had = key in dic
            old = dic.get(key)
            self._undo.append(("item", dic, key, old, had))
            dic[key] = value

        def undo(self):
            for kind, obj, key, old, had in reversed(self._undo):
                if kind == "attr":
                    if had:
                        setattr(obj, key, old)
                elif had:
                    obj[key] = old
                else:
                    obj.pop(key, None)
            self._undo.clear()

    init_db()
    tests = [
        test_preflight_otel,
        test_preflight_metrics,
        test_preflight_saml,
        test_preflight_scim,
        test_preflight_pii_ner,
        test_pii_ner_model_must_be_a_pipeline,
        test_preflight_jobqueue,
        test_preflight_s3,
        test_enable_gates_on_preflight,
        test_enable_green_and_secret,
        test_disable_never_preflights,
        test_jobqueue_enable_uses_backend_value,
        test_operator_step_up,
        test_web_panel_and_step_up,
        test_step_up_refusal_is_audited,
        test_web_csrf_enforced,
        test_no_api_or_mcp_surface,
        test_panel_fields_are_not_wire_writable,
        test_move_scim_saml_toggle_redirects,
        test_config_fields_editable_while_on,
        test_move_security_no_longer_writes_pii_ner,
    ]
    import inspect

    for fn in tests:
        if "monkeypatch" in inspect.signature(fn).parameters:
            mp = _MP()
            try:
                fn(mp)
            finally:
                mp.undo()
        else:
            fn()
    print("ENTERPRISE ENABLEMENT OK")


if __name__ == "__main__":
    main()
