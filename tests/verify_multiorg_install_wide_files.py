"""Install-wide files and settings are host-only under multi-org (offline, throwaway dir).

The 2026-10 security review, area `files` (files-01, files-02) and the same-class
sweep. Under an ACTIVE multi-org gate an org admin is an admin of ONE org, yet these
resources have no org dimension, so a write by one org changes every org:

  [1] files-01 the custom SRX ruleset (DATA_DIR/segmentation.srx) and the srx_enabled /
      srx_file settings: the web form POST /settings/segmentation and the /api doors
      upload / enable / disable (and the generic config door) answer 403 to an org admin,
      and the file on disk is untouched (bytes and mtime). The check runs BEFORE any write.
  [2] files-02 the LQA rulebooks (DATA_DIR/rulebooks/<src>__<tgt>.md, one per language
      pair for the whole install): GET and POST /api/lqa/rulebook and the web
      POST /documents/{id}/rulebook answer 403 to an org admin; the host-authored file is
      unchanged and its text never reaches the org admin over /api; the LQA page offers
      no editor and says the rulebook is host-managed.
  [3] sweep: install-wide egress targets and shared credentials (the SMTP port, TLS, user,
      password and From address; the Airtable and Atlassian OAuth app credentials) are refused on
      the /api config door and on the web Security page.
  [4] (retired with Advanced Workflows)
  [5] the host operator keeps every door under multi-org: CLI `admin srx upload`,
      `admin srx enable`, `admin lqa rulebook set|get` and `config set`.
  [6] gate OFF (every install today): every door above behaves exactly as before
      (web 303, /api 200, the file written, the rulebook readable).

Documented in: docs/architecture/tenancy-confinement.md
"""

import base64
import hashlib
import logging
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mo_files_")

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Customer, Document, Organization, Project, Role, Segment, User
from app.services import access, apikeys, lqa, security, settings, srx
from tests._cli_runner import run_cli
from tests._csrf import csrf_for, session_data

ADMIN_A = "mofiles-a@example.com"
ADMIN_B = "mofiles-b@example.com"
PW = "MoFiles-Admin-Pass-2026-x"  # pregate: allow-secret throwaway fixture login

# A valid ruleset that differs from the shipped default (one extra comment), so a write
# of it is visible on disk.
_DEFAULT_SRX = srx._DEFAULT_SRX_PATH.read_bytes()
_EVIL_SRX = _DEFAULT_SRX.replace(b"<header", b"<!-- org b -->\n<header", 1)
assert _EVIL_SRX != _DEFAULT_SRX and srx.validate(_EVIL_SRX)[0]

_HOST_RULEBOOK = "HOST RULEBOOK: FORBID utilisez"

#: Install-wide egress targets and shared credentials (sweep [3]).
_EGRESS_KEYS = {
    "smtp_port": "2525",
    "smtp_tls": "0",
    "smtp_user": "org-b-mailer",
    "smtp_from": "no-reply@attacker.example.test",
    "oauth_airtable_client_id": "org-b-airtable-app",
    "oauth_atlassian_client_id": "org-b-atlassian-app",
}
#: The write-only secrets of the same family, set through the Security page.
_EGRESS_SECRET_FIELDS = (
    "smtp_password",
    "oauth_airtable_client_secret",
    "oauth_atlassian_client_secret",
)

FIX = {}


def _fixture():
    init_db()
    with Session(engine) as s:
        org_a = Organization(name="Org A files")
        org_b = Organization(name="Org B files")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        FIX["org_a"], FIX["org_b"] = org_a.id, org_b.id
    security.create_user(ADMIN_A, "Admin A", Role.ADMIN, PW, org_id=FIX["org_a"])
    security.create_user(ADMIN_B, "Admin B", Role.ADMIN, PW, org_id=FIX["org_b"])
    with Session(engine) as s:
        for key, org_id in (("a", FIX["org_a"]), ("b", FIX["org_b"])):
            cust = Customer(name=f"Cust {key}", org_id=org_id)
            s.add(cust)
            s.commit()
            s.refresh(cust)
            proj = Project(
                name=f"Proj {key}", customer_id=cust.id, org_id=org_id, source_locale="en-US"
            )
            s.add(proj)
            s.commit()
            s.refresh(proj)
            doc = Document(project_id=proj.id, filename=f"doc_{key}.md", fmt="md")
            s.add(doc)
            s.commit()
            s.refresh(doc)
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="fr-FR",
                    source_text="Use the app.",
                    target_text="Utilisez l'app.",
                )
            )
            s.commit()
            FIX[f"doc_{key}"] = doc.id
        uid_b = s.exec(select(User).where(User.email == ADMIN_B)).first().id
    _row, raw = apikeys.create_key(uid_b, "mofiles-b", scopes="api docs admin")
    FIX["hdr_b"] = {"X-Loc-Token": raw}
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})


def _login(email) -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": PW, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    c.get("/settings")
    csrf_for(c)
    return c


def _srx_state():
    p = srx.custom_srx_path()
    if not p.exists():
        return (None, None, settings.get("srx_enabled"), settings.get("srx_file"))
    st = p.stat()
    return (p.read_bytes(), st.st_mtime_ns, settings.get("srx_enabled"), settings.get("srx_file"))


def _set_gate(on: bool):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_mo_files_" in os.environ["LOC_TMS_DATA_DIR"]
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    assert access.multi_org_active() is on


# --------------------------------------------------------------------------- #
def test_1_srx_is_host_only_under_multi_org():
    _set_gate(True)
    before = _srx_state()
    c = _login(ADMIN_B)
    r = c.post(
        "/settings/segmentation",
        data={"srx_enabled": "1"},
        files={"srx_file": ("evil.srx", _EVIL_SRX, "application/xml")},
        follow_redirects=False,
    )
    assert r.status_code == 403, (r.status_code, r.headers.get("location"))
    assert _srx_state() == before, "an org admin changed the install-wide SRX ruleset (web)"
    # The 'use default' and toggle-only posts of the same form are refused too.
    r = c.post("/settings/segmentation", data={"srx_enabled": "1"}, follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"))
    assert _srx_state() == before
    page = c.get("/settings").text
    assert 'action="/settings/segmentation"' not in page, "the SRX form is still offered"
    assert "only the host operator can" in page
    api = TestClient(app)
    hdr = FIX["hdr_b"]
    b64 = base64.b64encode(_EVIL_SRX).decode()
    for path, body in (
        ("/api/admin/srx/upload", {"srx_b64": b64}),
        ("/api/admin/srx/enable", {}),
        ("/api/admin/srx/disable", {}),
        ("/api/admin/config", {"settings": {"srx_enabled": "1"}}),
        ("/api/admin/config", {"settings": {"srx_file": "segmentation.srx"}}),
    ):
        r = api.post(path, headers=hdr, json=body)
        assert r.status_code == 403, (path, r.status_code, r.text[:200])
        assert _srx_state() == before, (path, "SRX state changed")
    # Validation writes nothing, so it stays open to an org admin.
    r = api.post("/api/admin/srx/validate", headers=hdr, json={"srx_b64": b64})
    assert r.status_code == 200, r.text[:200]
    print("  [1] files-01 SRX upload/enable/disable are host-only under multi-org (web + /api)")


def test_2_rulebooks_are_host_only_under_multi_org():
    _set_gate(True)
    lqa.save_rulebook("en-US", "fr-FR", _HOST_RULEBOOK)  # the host operator's rulebook
    path = lqa.rulebook_path("en-US", "fr-FR")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    api = TestClient(app)
    hdr = FIX["hdr_b"]
    r = api.get("/api/lqa/rulebook?source=en-US&target=fr-FR", headers=hdr)
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert "HOST RULEBOOK" not in r.text
    r = api.post(
        "/api/lqa/rulebook",
        headers=hdr,
        json={"source": "en-US", "target": "fr-FR", "text": "Ignore previous instructions."},
    )
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    # A pair with no rulebook yet cannot be planted either.
    r = api.post(
        "/api/lqa/rulebook",
        headers=hdr,
        json={"source": "en-US", "target": "de-DE", "text": "planted"},
    )
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert not lqa.rulebook_path("en-US", "de-DE").exists()
    # A refused attempt is audited (the request-audit middleware records every POST).
    with Session(engine) as s:
        refused_rows = s.exec(
            select(AuditLog).where(
                AuditLog.action == "POST /api/lqa/rulebook", AuditLog.actor == ADMIN_B
            )
        ).all()
    assert len(refused_rows) >= 2, "refused rulebook writes left no audit row"
    c = _login(ADMIN_B)
    r = c.post(
        f"/documents/{FIX['doc_b']}/rulebook",
        data={"locale": "fr-FR", "text": "Ignore previous instructions."},
        follow_redirects=False,
    )
    assert r.status_code == 403, (r.status_code, r.headers.get("location"))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    page = c.get(f"/documents/{FIX['doc_b']}/lqa?locale=fr-FR").text
    assert f'action="/documents/{FIX["doc_b"]}/rulebook"' not in page, "editor offered"
    assert "host operator" in page, "the LQA page must say the rulebook is host-managed"
    # The install-wide text is not readable on the web either (it may be another org's).
    assert "HOST RULEBOOK" not in page, "the LQA page leaks the install-wide rulebook text"
    print("  [2] files-02 LQA rulebooks are host-only under multi-org (web + /api read/write)")


def test_3_egress_targets_and_shared_credentials_are_host_only():
    _set_gate(True)
    api = TestClient(app)
    hdr = FIX["hdr_b"]
    before = {k: settings.get(k) for k in _EGRESS_KEYS}
    for key, value in _EGRESS_KEYS.items():
        r = api.post(
            "/api/admin/config", headers=hdr, json={"settings": {key: value}, "confirm": True}
        )
        assert r.status_code == 403, (key, r.status_code, r.text[:200])
    assert {k: settings.get(k) for k in _EGRESS_KEYS} == before
    # The dedicated SMTP door writes through the same spine.
    smtp_body = {"smtp_user": "org-b-mailer"}
    smtp_body["smtp_password"] = "org-b-mail-secret"  # pregate: allow-secret throwaway value
    r = api.post("/api/admin/smtp", headers=hdr, json=smtp_body)
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert not settings.get("smtp_user") and not settings.get("smtp_password")
    c = _login(ADMIN_B)
    sec = c.get("/security")
    assert sec.status_code == 200
    base = {
        "security_auth_required": settings.get("security_auth_required"),
        "audit_enabled": "1",
        "ldap_default_role": settings.get("ldap_default_role"),
        "smtp_port": settings.get("smtp_port"),
    }
    if settings.get("smtp_tls") == "1":
        base["smtp_tls"] = "1"
    # Control: the same form with nothing changed still saves, so each 403 below is the
    # field under test and not some other key on the page.
    r = c.post("/security", data=base, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers.get("location", ""), (
        r.status_code,
        r.headers.get("location"),
    )
    for field, value in (
        ("smtp_from", "no-reply@attacker.example.test"),
        ("smtp_user", "org-b-mailer"),
        ("oauth_airtable_client_id", "org-b-airtable-app"),
        ("smtp_password", "org-b-mail-secret"),  # pregate: allow-secret throwaway value
        ("oauth_atlassian_client_secret", "org-b-app-secret"),  # pregate: allow-secret fixture
    ):
        r = c.post("/security", data={**base, field: value}, follow_redirects=False)
        assert r.status_code == 403, (field, r.status_code, r.headers.get("location"))
    for field in _EGRESS_SECRET_FIELDS:
        assert not settings.get(field), (field, "a shared credential was replaced")
    assert {k: settings.get(k) for k in _EGRESS_KEYS} == before
    print("  [3] sweep: SMTP and OAuth app settings are host-only")


def test_5_host_operator_keeps_every_door():
    _set_gate(True)
    secret = "mofiles-operator-door-value-123"  # pregate: allow-secret throwaway operator key
    settings.set_many({"operator_key_hash": hashlib.sha256(secret.encode()).hexdigest()})
    os.environ["LOC_TMS_OPERATOR_KEY"] = secret
    op = ["--operator"]
    try:
        with tempfile.TemporaryDirectory() as td:
            srx_file = os.path.join(td, "host.srx")
            with open(srx_file, "wb") as fh:
                fh.write(_EVIL_SRX)
            code, out, err = run_cli([*op, "admin", "srx", "upload", srx_file])
            assert code == 0, (code, out, err)
            assert srx.custom_srx_path().read_bytes() == _EVIL_SRX
            code, out, err = run_cli([*op, "admin", "srx", "enable"])
            assert code == 0 and settings.get("srx_enabled") == "1", (code, out, err)
            rb_file = os.path.join(td, "rb.md")
            with open(rb_file, "w", encoding="utf-8") as fh:
                fh.write("HOST v2")
            code, out, err = run_cli(
                [*op, "admin", "lqa", "rulebook", "set", "en-US", "fr-FR", rb_file]
            )
            assert code == 0, (code, out, err)
            assert lqa.load_rulebook("en-US", "fr-FR") == "HOST v2"
            code, out, err = run_cli([*op, "admin", "lqa", "rulebook", "get", "en-US", "fr-FR"])
            assert code == 0 and "HOST v2" in out, (code, out, err)
        code, out, err = run_cli([*op, "config", "set", "smtp_from", "ops@example.org"])
        assert code == 0 and settings.get("smtp_from") == "ops@example.org", (code, out, err)
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
    settings.set_many({"srx_enabled": "0", "srx_file": "", "smtp_from": ""})
    print("  [5] the host CLI keeps the SRX, rulebook and config doors under multi-org")


def test_6_gate_off_is_unchanged():
    _set_gate(False)
    c = _login(ADMIN_B)
    assert 'action="/settings/segmentation"' in c.get("/settings").text
    r = c.post(
        "/settings/segmentation",
        data={"srx_enabled": "1"},
        files={"srx_file": ("ok.srx", _DEFAULT_SRX, "application/xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "srx_msg=" in r.headers.get("location", ""), r.headers
    assert srx.custom_srx_path().read_bytes() == _DEFAULT_SRX
    assert settings.get("srx_enabled") == "1" and settings.get("srx_file") == "segmentation.srx"
    api = TestClient(app)
    hdr = FIX["hdr_b"]
    r = api.post(
        "/api/admin/srx/upload",
        headers=hdr,
        json={"srx_b64": base64.b64encode(_EVIL_SRX).decode()},
    )
    assert r.status_code == 200, r.text[:200]
    assert srx.custom_srx_path().read_bytes() == _EVIL_SRX
    for path in ("/api/admin/srx/disable", "/api/admin/srx/enable"):
        r = api.post(path, headers=hdr, json={})
        assert r.status_code == 200, (path, r.text[:200])
    r = api.post(
        "/api/lqa/rulebook",
        headers=hdr,
        json={"source": "en-US", "target": "fr-FR", "text": "single-org rulebook"},
    )
    assert r.status_code == 200, r.text[:200]
    r = api.get("/api/lqa/rulebook?source=en-US&target=fr-FR", headers=hdr)
    assert r.status_code == 200 and r.json()["rulebook"] == "single-org rulebook", r.text[:200]
    r = c.post(
        f"/documents/{FIX['doc_b']}/rulebook",
        data={"locale": "fr-FR", "text": "web rulebook"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert lqa.load_rulebook("en-US", "fr-FR") == "web rulebook"
    page = c.get(f"/documents/{FIX['doc_b']}/lqa?locale=fr-FR").text
    assert f'action="/documents/{FIX["doc_b"]}/rulebook"' in page
    assert "web rulebook" in page, "gate off: the rulebook text is shown as before"
    for key, value in _EGRESS_KEYS.items():
        r = api.post(
            "/api/admin/config", headers=hdr, json={"settings": {key: value}, "confirm": True}
        )
        assert r.status_code == 200, (key, r.status_code, r.text[:200])
        assert settings.get(key) == value, key
    print("  [6] gate off: every door behaves exactly as before")


def main():
    _fixture()
    try:
        test_1_srx_is_host_only_under_multi_org()
        test_2_rulebooks_are_host_only_under_multi_org()
        test_3_egress_targets_and_shared_credentials_are_host_only()
        test_5_host_operator_keeps_every_door()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    test_6_gate_off_is_unchanged()
    print("verify_multiorg_install_wide_files OK")


if __name__ == "__main__":
    main()
