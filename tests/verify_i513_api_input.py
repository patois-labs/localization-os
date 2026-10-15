"""Issue #513 (API input handling), the items another queue is not covering.

  (1) (memory-note project scoping was an LLMemory check; that paid feature is not in
      the open core, so the case is gone.)
  (2) POST /api/admin/{ldap,oidc,smtp} refuse a null / list / object value with 400
      (the /api/admin/config answer) instead of storing the strings "None" / "['a']".
      Booleans and numbers still save.
  (4) A failure while RESOLVING the identity is audited as an unidentified request,
      never as "system" (which pinned an anonymous request on the app itself).

(3), non-object JSON bodies on the connector and document routes, is another queue's.
Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII-only. Final line: "I513 API INPUT OK".
"""

import os
import sys
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_i513_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Customer, Project, Role
from app.services import apikeys, security, settings

PROBE_IP = "203.0.113.9"
_N = [0]


def _n() -> int:
    _N[0] += 1
    return _N[0]


def _h(raw):
    return {"X-Loc-Token": raw}


def _setup():
    init_db()
    settings.set_many(
        {
            "security_auth_required": "0",
            "multi_org_enabled": "0",
            "remote_admin_enabled": "1",
        }
    )
    settings.invalidate_cache()
    admin = security.create_user("i513admin@x.com", "Admin", Role.ADMIN, "i513-password-123456")
    _row, raw = apikeys.create_key(admin.id, "i513", scopes="api docs admin")
    with Session(engine) as s:
        ca, cb = Customer(name="I513 A"), Customer(name="I513 B")
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
        pa = Project(name="I513 PA", source_locale="en-US", customer_id=ca.id)
        pb = Project(name="I513 PB", source_locale="en-US", customer_id=cb.id)
        s.add(pa)
        s.add(pb)
        s.commit()
        s.refresh(pa)
        s.refresh(pb)
        return raw, ca.id, cb.id, pa.id, pb.id


# ---------------------------------------------------------------- (2) identity routes
def test_identity_routes_refuse_nonscalar(raw):
    c = TestClient(app)
    bad = [
        ("/api/admin/smtp", {"smtp_port": None}),
        ("/api/admin/smtp", {"smtp_host": ["a", "b"]}),
        ("/api/admin/ldap", {"ldap_host": {"x": 1}, "confirm": True}),
        ("/api/admin/oidc", {"sso_issuer": None, "confirm": True}),
    ]
    for path, body in bad:
        before = {k: settings.get(k) for k in body if k != "confirm"}
        r = c.post(path, json=body, headers=_h(raw))
        assert r.status_code == 400, (path, body, r.status_code, r.text)
        assert "scalar" in r.json()["error"], r.text
        after = {k: settings.get(k) for k in body if k != "confirm"}
        assert before == after, (path, before, after)
        assert "None" not in str(after.values()), after
    # scalars still save
    r = c.post(
        "/api/admin/smtp",
        json={
            "smtp_host": "mail.example.test",
            "smtp_port": 2525,
            "smtp_tls": True,
            "confirm": True,
        },
        headers=_h(raw),
    )
    assert r.status_code == 200, (r.status_code, r.text)
    assert settings.get("smtp_host") == "mail.example.test"
    assert str(settings.get("smtp_port")) == "2525"
    print("  (2) identity routes refuse null/list/object with 400; scalars unchanged path")


# ---------------------------------------------------------------- (4) identity failure
def test_identity_failure_is_not_system():
    c = TestClient(app, client=(PROBE_IP, 50123))
    with Session(engine) as s:
        mark = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        mark = mark.id if mark else 0

    import app.main as app_main

    real = app_main.api_identity

    def _boom(request):
        # Fail only inside the audit writer; the auth gates resolve identity normally.
        if sys._getframe(1).f_code.co_name == "_audit_mutating_request":
            raise RuntimeError("pool timeout while resolving the identity")
        return real(request)

    with mock.patch("app.main.api_identity", _boom):
        r = c.post("/api/graphql", json={"query": "{__typename}"})
    assert r.status_code >= 400, r.status_code
    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog).where(AuditLog.id > mark, AuditLog.action == "POST /api/graphql")
        ).all()
    assert len(rows) == 1, [(x.actor, x.action, x.detail) for x in rows]
    row = rows[0]
    assert row.actor == f"unauthenticated {PROBE_IP}", row.actor
    assert row.actor != "system", row.actor
    assert row.detail.startswith("refused "), row.detail
    print("  (4) identity-resolution failure audits as unauthenticated <ip> + refused <status>")


def test_accepted_request_row_never_sampled(raw, ca):
    """Gate: an ACCEPTED mutation whose identity fails to resolve in the audit writer is a
    real change; the anonymous-refusal sampler (60/min) must never drop its row."""
    c = TestClient(app, client=(PROBE_IP, 50124))
    with Session(engine) as s:
        mark = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        mark = mark.id if mark else 0

    import app.main as app_main

    real = app_main.api_identity

    def _boom(request):
        if sys._getframe(1).f_code.co_name == "_audit_mutating_request":
            raise RuntimeError("pool timeout while resolving the identity")
        return real(request)

    with (
        mock.patch("app.main.api_identity", _boom),
        mock.patch("app.main.audit.allow_refusal_record", lambda: False),
    ):
        r = c.post(
            "/api/admin/smtp",
            json={"smtp_host": "accepted.example.test", "confirm": True},
            headers=_h(raw),
        )
    assert r.status_code == 200, (r.status_code, r.text)
    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog).where(AuditLog.id > mark, AuditLog.action == "POST /api/admin/smtp")
        ).all()
    assert len(rows) == 1, "an accepted mutation lost its audit row to the refusal sampler"
    assert rows[0].actor == f"unauthenticated {PROBE_IP}", rows[0].actor
    assert rows[0].detail.startswith("status 200"), rows[0].detail
    print("  (4b) accepted request with an identity failure keeps its audit row")


def test_non_object_bodies_are_400(raw):
    """Gate: the touched routes answer 400 for a list / string / non-JSON body, never a
    silent no-op 200 or a misleading 'customer' error."""
    c = TestClient(app)
    for path in ("/api/admin/smtp", "/api/admin/ldap", "/api/admin/oidc"):
        for kwargs in ({"json": []}, {"json": "x"}, {"content": b"not json"}):
            r = c.post(path, headers=_h(raw), **kwargs)
            assert r.status_code == 400, (path, kwargs, r.status_code, r.text)
            assert "JSON" in r.json()["error"], (path, r.text)
    print("  (5) non-object bodies answer 400 on the identity routes")


def main():
    raw, ca, _cb, _pa, _pb = _setup()
    test_identity_routes_refuse_nonscalar(raw)
    test_identity_failure_is_not_system()
    test_accepted_request_row_never_sampled(raw, ca)
    test_non_object_bodies_are_400(raw)
    print("I513 API INPUT OK")


if __name__ == "__main__":
    main()
