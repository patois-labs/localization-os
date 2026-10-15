"""Issue #501 item 2 (connectors_common / connectors redirects), offline.

  * connector_kind_toggle: a browser's ABSOLUTE same-host Referer returns to that page
    (it used to fall to "/" because _safe_local_path refuses absolute URLs and "/" is
    truthy); a foreign or missing Referer lands on /connectors/manage.
  * connector_token_regenerate: the redirect goes through the same local-path check
    (a foreign Referer is never echoed as a Location).
  * OAuth callback success: the "Connected via OAuth." message is attested, so the page
    guard does not strip it from the connection page the user lands on.

Item 1 of #501 (urlencode in the 13 action redirects) is done by another queue.
Hermetic: throwaway data dir, no network, auth off.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rest501_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.services import connectors as reg
from app.services import settings
from app.services.connectors import oauth as conn_oauth_mod
from app.services.connectors import store as cstore

init_db()
settings.set_many({"security_auth_required": "0"})
settings.invalidate_cache()


def _loc(r):
    return r.headers.get("location")


def test_toggle_referer_fallback():
    c = TestClient(app)
    ref = "http://testserver/connectors/catalog?x=1"
    r = c.post(
        "/connectors/manage/jira",
        data={"enabled": "1"},
        headers={"Referer": ref},
        follow_redirects=False,
    )
    assert r.status_code == 303 and _loc(r) == "/connectors/catalog?x=1", _loc(r)
    r = c.post("/connectors/manage/jira", data={"enabled": "1"}, follow_redirects=False)
    assert _loc(r) == "/connectors/manage", _loc(r)
    print("  toggle: same-host absolute Referer honored, missing -> /connectors/manage OK")


def test_referer_helper_refuses_foreign_and_malformed():
    # A cross-origin POST is stopped earlier by the CSRF middleware, so the helper's
    # refusals are exercised directly (the token route also relies on them).
    from types import SimpleNamespace

    from app.web.form_errors import referer_local_path

    def req(ref, host="app.example"):
        headers = {"host": host}
        if ref is not None:
            headers["referer"] = ref
        return SimpleNamespace(headers=headers, url=SimpleNamespace(netloc=host))

    d = "/fallback"
    assert referer_local_path(req(None), d) == d
    assert referer_local_path(req("https://evil.example/x"), d) == d
    assert referer_local_path(req("//evil.example/x"), d) == d
    assert referer_local_path(req("javascript:alert(1)"), d) == d
    assert referer_local_path(req("https://app.example/a\b"), d) == d  # control character
    # review: a real backslash (browsers treat "\" as "/", so "/\evil" is "//evil"), userinfo,
    # a different port, a path that starts "//" and a scheme-relative / empty-host form
    assert referer_local_path(req("https://app.example/a\\b"), d) == d
    assert referer_local_path(req("https://app.example/\\evil.example"), d) == d
    assert referer_local_path(req("/\\evil.example"), d) == d
    assert referer_local_path(req("https://app.example@evil.example/x"), d) == d
    assert referer_local_path(req("https://evil.example@app.example/x"), d) == d
    assert referer_local_path(req("https://app.example:8443/x"), d) == d
    assert referer_local_path(req("https://app.example//evil.example/x"), d) == d
    assert referer_local_path(req("https:///evil.example/x"), d) == d
    assert referer_local_path(req("https:/evil.example/x"), d) == d
    assert referer_local_path(req("ftp://app.example/x"), d) == d
    assert referer_local_path(req("https://app.example.evil.example/x"), d) == d
    assert referer_local_path(req("https://evil.example/https://app.example/x"), d) == d
    # host case folds (DNS names are case-insensitive) and the redirect stays a local path
    assert referer_local_path(req("HTTPS://APP.Example/Case?q=1#frag"), d) == "/Case?q=1"
    assert referer_local_path(req("https://app.example/a?b=1"), d) == "/a?b=1"
    assert referer_local_path(req("/connectors/x"), d) == "/connectors/x"
    print("  referer helper: foreign / protocol-relative / malformed -> default OK")


def test_token_regenerate_redirect_is_local():
    c = TestClient(app)
    r = c.post(
        "/connectors/token/regenerate",
        headers={"Referer": "http://testserver/connectors/manage"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and _loc(r) == "/connectors/manage", _loc(r)
    r = c.post("/connectors/token/regenerate", follow_redirects=False)
    assert _loc(r) == "/connectors", _loc(r)
    print("  token regenerate: local redirect OK")


def test_oauth_success_message_survives_page_guard():
    reg.set_kind_enabled("jira", True)
    settings.set_many(
        {"oauth_atlassian_client_id": "atl-client", "oauth_atlassian_client_secret": "atl-secret"}
    )
    c = TestClient(app)
    made = cstore.create(
        "jira", "Callback target", {"base_url": "https://team.atlassian.net"}, {}, actor="admin"
    )
    c.post("/connectors/jira/oauth/start", data={"cid": made.id}, follow_redirects=False)
    orig = conn_oauth_mod.complete_authorize
    try:
        conn_oauth_mod.complete_authorize = lambda request, code, state: (
            (
                made.id,
                {"access_token": "AT", "refresh_token": "RT", "cloudid": "c", "expires_at": 0},
            ),
            None,
        )
        r = c.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
        assert r.status_code == 303, r.status_code
        page = c.get(_loc(r))
        assert page.status_code == 200, page.status_code
        banner = '<p class="ok">Connected via OAuth.</p>'
        assert banner in page.text, "success message was stripped by the page guard"
    finally:
        conn_oauth_mod.complete_authorize = orig
    print("  oauth callback success: message attested and shown OK")


if __name__ == "__main__":
    test_toggle_referer_fallback()
    test_referer_helper_refuses_foreign_and_malformed()
    test_token_regenerate_redirect_is_local()
    test_oauth_success_message_survives_page_guard()
    print("REST501 OK")
