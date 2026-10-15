"""Jira jql and Confluence space_key are admin only on a credentialed connection (#419
residual, the maintainer's B-scope decision 2026-10-11).

The Jira JQL and the Confluence space key are the declared scope every caller request is
ANDed against; blank means "anything the stored token can read". Before this change a
LINGUIST could edit either on a connection holding an admin's saved credential and so
widen what that credential reaches. They now ride `store_common.scope_admin_error`
(`SCOPE_ADMIN_FIELDS`) exactly like ServiceNow knowledge_base and Notion parent_page_id:

  1. seam: a non-admin change (or blanking) is refused with an audit row; an admin, an
     unchanged value and a credential-less connection are allowed; the compare is
     case-sensitive for these two keys (a JQL literal, a ~accountid personal space).
  2. /api update: LINGUIST 403 + config untouched; other fields and the same value pass;
     an admin may change it.
  3. web edit form: LINGUIST refused (ok=0, unchanged); re-saving the same value works.
  4. /api create without a credential is unaffected; the help text names the rule.

Offline + hermetic: throwaway LOC_TMS_DATA_DIR, no connector call is made.

Final line: "SCOPE ADMIN JIRA CONFLUENCE OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_scope_jc_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Connection, Role, User
from app.services import apikeys, security, settings
from app.services import connectors as reg
from app.services.connectors import store as cstore
from tests._csrf import csrf_for

_LOGIN_SECRET = "scope-jc-pass-123456"  # pregate: allow-secret throwaway fixture login
_FAKE_TOKEN = "atlassian-fake-token"  # pregate: allow-secret fake fixture credential
_SITE = "https://scope-qa.atlassian.net"
_JQL = "project = LOC AND summary ~ 'Loc'"
_SPACE = "HELP"

CASES = (
    # kind, scope key, in-scope value, widened value, case-only variant
    ("jira", "jql", _JQL, "project = HR", "project = LOC AND summary ~ 'LOC'"),
    ("confluence", "space_key", _SPACE, "SECRET", "help"),
    # gate 2026-10-11: a cql-only scope is the whole scope, so cql is admin only too
    (
        "confluence",
        "cql",
        "space = HELP AND label = 'loc'",
        "type = page",
        "space = HELP AND label = 'LOC'",
    ),
)


def _base_cfg(kind, key, value):
    cfg = {"base_url": _SITE, key: value, "source_locale": "en-US", "target_locales": "fr-FR"}
    if kind == "jira":
        cfg["source_field"] = "summary"
    return cfg


def _mk(kind, key, value, *, credential=True):
    secret = {"email": "admin@x.test", "api_token": _FAKE_TOKEN} if credential else {}
    return cstore.create(
        kind, f"{kind}-scope-{value[:6]}", _base_cfg(kind, key, value), secret, actor="admin"
    )


def _row(cid):
    with Session(engine) as s:
        return s.get(Connection, cid)


def _cfg(cid):
    return dict(cstore.get(cid).config or {})


def _refusals(cid):
    with Session(engine) as s:
        return [
            a
            for a in s.query(AuditLog).all()
            if a.action == "connector.scope_change_refused" and a.target == f"connection:{cid}"
        ]


def _user(email, role):
    with Session(engine) as s:
        u = User(email=email, name=email, role=role, active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_seam():
    for kind, key, val, wide, case_variant in CASES:
        conn = _mk(kind, key, val)
        row = _row(conn.id)
        for new in (wide, "", case_variant):
            err = cstore.scope_admin_error(
                kind, row, {key: new}, caller_is_admin=False, actor="ling@x.test"
            )
            assert "Only an admin" in err and sp_name(kind) in err, (kind, new, err)
        assert len(_refusals(conn.id)) == 3, _refusals(conn.id)
        assert not cstore.scope_admin_error(kind, row, {key: wide}, caller_is_admin=True)
        # unchanged (outer whitespace ignored) and other-field edits stay open
        assert not cstore.scope_admin_error(kind, row, {key: f"  {val} "}, caller_is_admin=False)
        assert not cstore.scope_admin_error(
            kind, row, {"target_locales": "de-DE"}, caller_is_admin=False
        )
        # a connection with no saved credential: the editor brings their own
        bare = _mk(kind, key, val, credential=False)
        assert not cstore.scope_admin_error(kind, _row(bare.id), {key: wide}, caller_is_admin=False)
        # create (conn=None) is unaffected: the creator supplies the credential
        assert not cstore.scope_admin_error(kind, None, {key: wide}, caller_is_admin=False)
    print("  1. seam: non-admin change/blank/case change refused + audited; admin, same value ok")


def sp_name(kind):
    return reg.spec(kind).name


def test_api_and_web():
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        api = TestClient(app)
        ling = _user("ling-scope-jc@x.test", Role.LINGUIST)
        _r, ltok = apikeys.create_key(ling, "scope-jc-ling")
        admin = _user("admin-scope-jc@x.test", Role.ADMIN)
        _r, atok = apikeys.create_key(admin, "scope-jc-admin")
        security.create_user(
            "ling-scope-jc-web@x.test", "ling-scope-jc-web@x.test", Role.LINGUIST, _LOGIN_SECRET
        )
        web = TestClient(app)
        csrf_for(web)
        r = web.post(
            "/auth/login",
            data={"email": "ling-scope-jc-web@x.test", "password": _LOGIN_SECRET, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(web)

        for kind, key, val, wide, _case in CASES:
            conn = _mk(kind, key, val)
            url = f"/api/connectors/{conn.id}/update"
            for new in (wide, ""):
                r = api.post(url, headers={"X-Loc-Token": ltok}, json={"config": {key: new}})
                assert r.status_code == 403 and "Only an admin" in r.text, (
                    kind,
                    r.status_code,
                    r.text,
                )
                assert _cfg(conn.id)[key] == val, _cfg(conn.id)
            r = api.post(
                url,
                headers={"X-Loc-Token": ltok},
                json={"config": {key: val, "target_locales": "de-DE"}},
            )
            assert r.status_code == 200, (kind, r.status_code, r.text[:300])
            assert _cfg(conn.id)["target_locales"] == "de-DE" and _cfg(conn.id)[key] == val

            # web edit form: the full form re-posts every spec field
            form = {"name": conn.name, **_base_cfg(kind, key, val)}
            form["target_locales"] = "de-DE"
            form[key] = wide
            r = web.post(f"/connectors/connection/{conn.id}", data=form, follow_redirects=False)
            assert r.status_code == 303 and "ok=0" in r.headers["location"], (kind, r.headers)
            assert "Only%20an%20admin" in r.headers["location"], r.headers["location"]
            assert _cfg(conn.id)[key] == val, _cfg(conn.id)
            form[key] = val
            form["target_locales"] = "es-MX"
            r = web.post(f"/connectors/connection/{conn.id}", data=form, follow_redirects=False)
            assert r.status_code == 303 and "ok=1" in r.headers["location"], (kind, r.headers)
            assert _cfg(conn.id)["target_locales"] == "es-MX" and _cfg(conn.id)[key] == val
            assert len(_refusals(conn.id)) == 3, (kind, len(_refusals(conn.id)))

            # an admin may move the scope
            r = api.post(url, headers={"X-Loc-Token": atok}, json={"config": {key: wide}})
            assert r.status_code == 200 and _cfg(conn.id)[key] == wide, (kind, r.text[:300])

            # /api create (never carries a credential) is unaffected
            r = api.post(
                "/api/connectors",
                headers={"X-Loc-Token": ltok},
                json={
                    "kind": kind,
                    "name": f"{kind}-ling-new",
                    "config": _base_cfg(kind, key, wide),
                },
            )
            assert r.status_code in (200, 201), (kind, r.status_code, r.text[:300])
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  2-3. /api 403 + web ok=0 for a LINGUIST; same value, other fields, admin pass")


def test_explained():
    for kind, key, *_ in CASES:
        f = next(x for x in reg.spec(kind).config_fields if x.name == key)
        assert "Only an admin can change it" in f.help, (kind, f.help)
    from app.services.connectors import store_common

    for kind, key, *_ in CASES:
        assert key in store_common.SCOPE_ADMIN_FIELDS.get(kind, ()), kind
    print("  4. field help on the web form names the admin-only rule")


def main():
    init_db()
    test_seam()
    test_api_and_web()
    test_explained()
    print("SCOPE ADMIN JIRA CONFLUENCE OK")


if __name__ == "__main__":
    main()
