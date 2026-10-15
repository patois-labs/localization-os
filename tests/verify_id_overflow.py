"""Phase-2 hardening F1 + F2 — an oversized id/param is a clean 4xx, never a 500.

An id wider than a signed 64-bit int (10**19, 2**63) survives pydantic's `int()`
(no 422 — the string parses) but only fails LATER, at the SQLite bind inside
`session.get(...)` / a DB filter, as `OverflowError: Python int too large to
convert to SQLite INTEGER` — which the app's catch-all turned into a 500 on 149
by-id path routes (F1) and two numeric query-param vectors (F2). The fix is a
single shared boundary: an `OverflowError` exception handler (app/main.py) scoped
to that exact SQLite-bind signature, returning the same 422 pydantic already
returns for a non-numeric id on these routes — so ALL by-id path routes AND the
query-param routes get one clean refusal without touching 149 handler signatures.

This suite proves:
  [A] a REPRESENTATIVE sample of by-id routes across methods+families (GET, POST,
      PATCH, PUT, DELETE) returns 4xx-not-500 on both oversized values.
  [B] the two F2 query-param vectors (changes?since=, context/resolve
      ?project=&customer=) return 4xx-not-500 on both oversized values.
  [C] the already-correct sibling cases are UNCHANGED (valid id -> 200,
      negative/zero/in-range-missing -> 404, non-numeric -> 422), so the boundary
      changed nothing but the crash case.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"ID OVERFLOW OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_idovf_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Project, ProjectType, Role
from app.services import apikeys, content, security, settings
from app.services import keys as keys_svc

PW = "idovf-pw-123456"
# The two shapes that only fail at the SQLite bind: 10**19 > 2**63 (the audit's
# forged-POST value) and 2**63 exactly (the first value past signed-64-bit).
BIG_VALUES = (10**19, 2**63)
# raise_server_exceptions=False so a REGRESSION surfaces as a 500 STATUS CODE
# (assertion fails cleanly) instead of re-raising the OverflowError out of the client.
CLIENT = TestClient(app, raise_server_exceptions=False)


def _token(email, scopes="api docs keys admin", role=Role.ADMIN):
    user = security.create_user(email, email, role, PW)
    _row, raw = apikeys.create_key(user.id, "idovf", scopes=scopes)
    return raw


def _hdr(tok):
    return {"X-Loc-Token": tok}


def _customer():
    with Session(engine) as s:
        c = Customer(name="IdOvf Cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _doc_project(cust_id):
    with Session(engine) as s:
        p = content.project_create(
            s, "IdOvf Doc", source_locale="en-US", target_locales="de-DE", customer_id=str(cust_id)
        )
        return p.id


def _key_project(cust_id):
    with Session(engine) as s:
        p = content.project_create(
            s,
            "IdOvf Keys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        keys_svc.create_key(
            s, s.get(Project, p.id), "ovf::probe", base_value="Hello", actor="idovf-fixture"
        )
        return p.id


def _assert_4xx_not_500(r, label):
    assert r.status_code != 500, (
        f"{label} must NOT 500 on an oversized id (F1/F2 regression): got 500 {r.text!r}"
    )
    assert 400 <= r.status_code < 500, "%s must be a clean 4xx on an oversized id: got %d %r" % (  # noqa: UP031
        label,
        r.status_code,
        r.text,
    )


def test_by_id_path_routes_are_4xx_not_500():
    """[A] representative by-id routes across every HTTP method + several families."""
    tok = _token("idovf-path@x.com")
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    h = _hdr(tok)
    saw_422 = 0
    for big in BIG_VALUES:
        # (method, path, json-body-or-None) — one per family/verb from Appendix A.
        cases = [
            ("GET", "/api/projects/%d" % big, None),  # GET  # noqa: UP031
            ("POST", "/api/projects/%d/rename" % big, {"name": "x"}),  # POST  # noqa: UP031
            ("POST", "/api/workflows/%d/run" % big, {}),  # POST (other family)  # noqa: UP031
            ("GET", "/api/glossaries/%d" % big, None),  # GET (other family)  # noqa: UP031
            (
                "POST",
                "/api/customers/%d/rename" % big,  # noqa: UP031
                {"name": "x"},
            ),  # POST (other family)  # noqa: E501, RUF100, UP031
            ("POST", "/api/connectors/%d/test" % big, {}),  # POST (connectors)  # noqa: UP031
            ("GET", "/api/tm/%d" % big, None),  # GET (tm)  # noqa: UP031
            ("PATCH", "/api/keys/%d" % big, {"name": "x"}),  # PATCH  # noqa: UP031
            ("PUT", "/api/keys/%d/values/en-US" % big, {"value": "x"}),  # PUT  # noqa: UP031
            ("DELETE", "/api/keys/%d" % big, None),  # DELETE  # noqa: UP031
        ]
        for method, path, body in cases:
            label = f"{method} {path}"
            if method == "GET":
                r = CLIENT.get(path, headers=h)
            elif method == "DELETE":
                r = CLIENT.delete(path, headers=h)
            elif method == "PATCH":
                r = CLIENT.patch(path, json=body, headers=h)
            elif method == "PUT":
                r = CLIENT.put(path, json=body, headers=h)
            else:
                r = CLIENT.post(path, json=body, headers=h)
            _assert_4xx_not_500(r, label)
            if r.status_code == 422:
                saw_422 += 1
    # The boundary handler fired (422) on at least some pure session.get routes —
    # proof the fix engaged, not that every route happens to coerce to a 404.
    assert saw_422 > 0, "expected the OverflowError->422 boundary to fire on some route"
    print(
        "  [A] %d by-id path routes x %d oversized values -> all 4xx-not-500 "  # noqa: UP031
        "(%d resolved via the 422 boundary) OK" % (10, len(BIG_VALUES), saw_422)
    )


def test_query_param_vectors_are_4xx_not_500():
    """[B] F2 — the two numeric QUERY-param overflow vectors a path-id gate misses."""
    tok = _token("idovf-query@x.com")
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    h = _hdr(tok)
    cust = _customer()
    key_pid = _key_project(cust)
    for big in BIG_VALUES:
        r = CLIENT.get("/api/projects/%d/changes" % key_pid, params={"since": big}, headers=h)  # noqa: UP031
        _assert_4xx_not_500(r, "GET /changes?since=%d" % big)  # noqa: UP031
        r = CLIENT.get("/api/context/resolve", params={"project": big}, headers=h)
        _assert_4xx_not_500(r, "GET /context/resolve?project=%d" % big)  # noqa: UP031
        r = CLIENT.get("/api/context/resolve", params={"customer": big}, headers=h)
        _assert_4xx_not_500(r, "GET /context/resolve?customer=%d" % big)  # noqa: UP031
    print(
        "  [B] changes?since= and context/resolve?project=&customer= "
        "-> 4xx-not-500 on both oversized values OK"
    )


def test_sibling_cases_unchanged():
    """[C] the boundary must NOT change any already-correct behaviour."""
    tok = _token("idovf-ctrl@x.com")
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    h = _hdr(tok)
    cust = _customer()
    pid = _doc_project(cust)
    key_pid = _key_project(cust)

    # valid id still resolves
    r = CLIENT.get("/api/projects/%d" % pid, headers=h)  # noqa: UP031
    assert r.status_code == 200, ("valid id must still 200", r.status_code, r.text)
    # negative / zero -> the existing 404 (in-range, not-found)
    for bad in (-5, 0):
        r = CLIENT.get("/api/projects/%d" % bad, headers=h)  # noqa: UP031
        assert r.status_code == 404, ("id %d must 404" % bad, r.status_code, r.text)  # noqa: UP031
    # non-numeric -> pydantic 422 (unchanged)
    r = CLIENT.get("/api/projects/abc", headers=h)
    assert r.status_code == 422, ("non-numeric id must 422", r.status_code, r.text)
    # in-range-but-missing -> 404 (unchanged)
    r = CLIENT.get("/api/projects/999999", headers=h)
    assert r.status_code == 404, ("in-range missing id must 404", r.status_code, r.text)
    # an in-range `since` on a real key project is still a clean 200 (the overflow
    # fix must not touch the valid-cursor path the Delta API depends on)
    r = CLIENT.get("/api/projects/%d/changes" % key_pid, params={"since": 2**63 - 1}, headers=h)  # noqa: UP031
    assert r.status_code == 200, ("in-range since must 200", r.status_code, r.text)
    print(
        "  [C] valid->200, negative/zero/missing->404, non-numeric->422, "
        "in-range since->200 (all unchanged) OK"
    )


def main():
    init_db()
    test_by_id_path_routes_are_4xx_not_500()
    test_query_param_vectors_are_4xx_not_500()
    test_sibling_cases_unchanged()
    print("ID OVERFLOW OK")


if __name__ == "__main__":
    main()
