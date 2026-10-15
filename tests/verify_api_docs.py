"""API docs surfaces (issue #286), offline: no network, throwaway data dir.

The reviewer's reproduction: with the login wall ON, an unauthenticated GET /docs returned
FastAPI's Swagger page, which loads `swagger-ui-dist@5` (a floating major version, no
Subresource Integrity) from cdn.jsdelivr.net and runs it on the app origin; /redoc did the
same with redoc; and an anonymous GET /openapi.json returned the full route map.

The decision: the repo does not vendor either bundle, so an exact version with an SRI hash
cannot be verified offline. The CDN-loaded pages are switched OFF instead, and the FULL
schema needs a signed-in session or a valid API key once the wall is on. This suite proves:

  * the app registers no /docs, /docs/oauth2-redirect or /redoc route, and FastAPI's own
    docs/redoc/openapi URLs are all None (our handler serves /openapi.json);
  * no response from /docs, /redoc or /openapi.json names a CDN host;
  * wall OFF: /openapi.json answers 200 as before, /docs and /redoc are 404;
  * wall ON: anonymous /openapi.json (GET and HEAD) and a wrong key get 401 with a plain
    message; a valid API key and a signed-in session get the same schema as before;
    /docs and /redoc get the sign-in redirect, never a page; /utcp (the read-only public
    subset) is unchanged;
  * the help page no longer links Swagger or ReDoc.

ASCII-only prints.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_api_docs_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import _OPENAPI_DENIED, app
from app.models import Role
from app.services import apikeys, security, settings
from app.web._routes import walk_routes
from tests._csrf import csrf_for

PW = "Api-Docs-Passw0rd!x"  # pregate: allow-secret throwaway test-only account password
_CDN_MARKERS = ("cdn.jsdelivr.net", "swagger-ui-dist", "unpkg.com", "redoc.standalone")


_KEY_HEADER = "X-Loc-Token"  # the API key header name (not a value)


def _paths():
    return {getattr(r, "path", None) for r in walk_routes(app)}


def test_no_cdn_pages_are_registered():
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    paths = _paths()
    for gone in ("/docs", "/docs/oauth2-redirect", "/redoc"):
        assert gone not in paths, f"{gone} is still registered"
    assert "/openapi.json" in paths, "the schema route must still exist"
    print("  no /docs, /docs/oauth2-redirect or /redoc route; /openapi.json is ours")


def _no_cdn(resp, label):
    body = resp.text if resp.content else ""
    for marker in _CDN_MARKERS:
        assert marker not in body, f"{label} still names {marker}"


def test_wall_off_behaves_as_before(c):
    schema = c.get("/openapi.json")
    assert schema.status_code == 200, schema.status_code
    assert "/api/projects" in schema.json()["paths"]
    _no_cdn(schema, "/openapi.json")
    for path in ("/docs", "/redoc"):
        r = c.get(path)
        assert r.status_code == 404, (path, r.status_code)
        _no_cdn(r, path)
    print("  wall off: /openapi.json 200, /docs + /redoc 404, no CDN reference anywhere")
    return schema.json()


def test_wall_on(c, schema_off):
    admin = security.create_user("apidocs-admin@example.com", "Admin", Role.ADMIN, PW)
    viewer = security.create_user("apidocs-viewer@example.com", "Viewer", Role.VIEWER, PW)
    _row, raw_key = apikeys.create_key(viewer.id, "api-docs", scopes="api")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        anon = TestClient(app)
        for method in ("get", "head"):
            r = getattr(anon, method)("/openapi.json", follow_redirects=False)
            assert r.status_code == 401, (method, r.status_code)
        r = anon.get("/openapi.json", follow_redirects=False)
        assert r.json() == {"ok": False, "error": _OPENAPI_DENIED}, r.text
        assert "paths" not in r.text
        bad = anon.get("/openapi.json", headers=[(_KEY_HEADER, "lsu_bad")])
        assert bad.status_code == 401, bad.status_code
        for path in ("/docs", "/redoc"):
            r = anon.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/auth/login"), (
                path,
                r.status_code,
                r.headers.get("location"),
            )
            _no_cdn(r, path)
        print("  wall on, anonymous: /openapi.json 401 (GET + HEAD, wrong key too);")
        print("      /docs + /redoc -> sign-in redirect, never a page")

        keyed = anon.get("/openapi.json", headers=[(_KEY_HEADER, raw_key)])
        assert keyed.status_code == 200, keyed.status_code
        assert keyed.json()["paths"] == schema_off["paths"], "a key must see the same schema"

        user = TestClient(app)
        csrf_for(user)
        r = user.post(
            "/auth/login",
            data={"email": admin.email, "password": PW, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        signed_in = user.get("/openapi.json")
        assert signed_in.status_code == 200, signed_in.status_code
        assert signed_in.json()["paths"] == schema_off["paths"]
        assert user.get("/docs").status_code == 404, "a signed-in user gets no Swagger page"
        print("  wall on: an API key and a signed-in session both read the full schema")

        utcp = anon.get("/utcp")
        assert utcp.status_code == 200, "the public read-only /utcp subset is unchanged"
        print("  /utcp (read-only public subset) still answers anonymously")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def test_help_page_links(c):
    page = c.get("/help")
    assert page.status_code == 200
    assert 'href="/docs"' not in page.text and 'href="/redoc"' not in page.text
    assert 'href="/openapi.json"' in page.text and "X-Loc-Token" in page.text
    print("  /help links the schema (and says who may read it), not Swagger or ReDoc")


def main():
    init_db()
    c = TestClient(app)
    test_no_cdn_pages_are_registered()
    schema_off = test_wall_off_behaves_as_before(c)
    test_help_page_links(c)
    test_wall_on(c, schema_off)
    print("API DOCS OK")


if __name__ == "__main__":
    main()
