"""Per-user API key verification (offline).

- a signed-in user mints a key from /account; the full key is shown once
  on the next /account render (session-stashed — never in a URL, where it
  would leak into access logs / browser history)
- the key authenticates the inbound API (X-Loc-Token) and attributes work:
  an async connector sync creates a Job with created_by == the user's email
- async sync dedup: a retry while a run is in flight returns 202 with the
  SAME job id and existing=true instead of stacking a second run
- revoking the key kills access; legacy shared connector tokens are refused
- admins can revoke ALL keys for a user; tampered keys are rejected
- users cannot revoke someone else's key via the service guard
"""

import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_apikeys_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app, connector_api_token
from app.models import Job, JobStatus, Organization
from app.services import apikeys, security, settings
from app.services.connectors import store as cstore
from tests._csrf import csrf_for


def main() -> None:
    init_db()
    security.seed_initial()  # admin/admin + linguist + user1, auth ON
    settings.set_many({"audit_enabled": "1"})
    c = TestClient(app)
    csrf_for(c)  # auth on: the login POST carries the session CSRF token
    r = c.post(
        "/auth/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    csrf_for(c)  # login rotated the session -> refresh for the /account/apikeys POSTs

    # --- create a key from /account; full key surfaces ONCE, never in a URL ---
    r = c.post("/account/apikeys", data={"name": "CI key"}, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text)
    loc = r.headers["location"]
    assert "lsu_" not in loc and "new_key=" not in loc, (
        f"the raw key must never ride in a URL: {loc}"
    )
    page = c.get(loc).text
    m = re.search(r'value="(lsu_[^"]+)"', page)
    assert m and "won't be shown again" in page, "one-time display missing"
    full_key = m.group(1)
    assert full_key.startswith("lsu_") and len(full_key) > 20, full_key
    # ...and only the prefix afterwards
    page2 = c.get("/account").text
    assert full_key not in page2 and full_key[:12] in page2, "full key must not persist on the page"
    print("  create: POST /account/apikeys -> lsu_ key shown once (no URL), prefix afterwards")

    # --- the per-user key authenticates the inbound API ---
    assert c.get("/api/connectors").status_code == 401, "no token must 401"
    r = c.get("/api/connectors", headers={"X-Loc-Token": full_key})
    assert r.status_code == 200 and r.json()["ok"], r.text
    print("  auth: GET /api/connectors with the user key -> 200")

    # --- attribution: async sync creates a Job credited to the user ---
    conn = cstore.create(
        "airtable", "Dummy", {"source_field": "Notes"}, {"api_key": "patFAKE"}, actor="admin"
    )
    real_translate_now = cstore.translate_now  # never hit the network in tests
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (True, "ok", {"translated": 1}),
    )
    try:
        r = c.post(
            f"/api/connectors/{conn.id}/sync",
            json={"async": True},
            headers={"X-Loc-Token": full_key},
        )
        assert r.status_code == 202, (r.status_code, r.text)
        jid = r.json()["job_id"]
        with Session(engine) as s:
            job = s.get(Job, jid)
            assert job.created_by == "admin", f"job.created_by={job.created_by!r}"
            assert job.payload.get("actor") == "admin", job.payload
        # let the worker consume the patched stub before restoring the real one
        deadline = time.time() + 15
        while time.time() < deadline:
            d = c.get(f"/api/jobs/{jid}", headers={"X-Loc-Token": full_key}).json()
            if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            time.sleep(0.05)
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_translate_now)
    print("  attribution: async sync via user key -> Job.created_by == 'admin'")

    # --- async sync dedup: a retry while a run is in flight must return the
    #     LIVE job (202, same id, existing=true), never stack a second run.
    #     The fake QUEUED row is never enqueued, so no handler runs. ---
    with Session(engine) as s:
        live = Job(
            kind="connector_translate",
            status=JobStatus.QUEUED,
            payload={"connection_id": conn.id, "actor": "x"},
        )
        s.add(live)
        s.commit()
        s.refresh(live)
        live_id = live.id
    r = c.post(
        f"/api/connectors/{conn.id}/sync", json={"async": True}, headers={"X-Loc-Token": full_key}
    )
    assert r.status_code == 202, (r.status_code, r.text)
    d = r.json()
    assert d["job_id"] == live_id, f"dedup must return the live job, got {d}"
    assert d.get("existing") is True, f"dup response must carry existing=true: {d}"
    assert d["status_url"] == f"/api/jobs/{live_id}", d
    with Session(engine) as s:
        j = s.get(Job, live_id)
        j.status = JobStatus.COMPLETED
        s.add(j)
        s.commit()
    print("  dedup: async retry during a queued run -> 202, same job id, existing=true")

    # --- revoke from /account: the key stops working immediately ---
    admin_user = security.get_by_email("admin")
    kid = [k for k in apikeys.list_keys(admin_user.id) if k.active][0].id  # noqa: RUF015
    r = c.post(f"/account/apikeys/{kid}/revoke", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert c.get("/api/connectors", headers={"X-Loc-Token": full_key}).status_code == 401, (
        "revoked key must be rejected"
    )
    print("  revoke: own key revoked -> same call now 401")

    # Legacy shared credentials no longer carry connector authority.
    token = connector_api_token()
    assert c.get("/api/connectors", headers={"X-Loc-Token": token}).status_code == 403, (
        "legacy connector token must be refused"
    )
    print("  legacy: shared token cannot list connections")

    # --- ownership guard: a user cannot revoke someone else's key ---
    u1 = security.get_by_email("user1")
    u1key, u1full = apikeys.create_key(u1.id, "user1 key")
    assert apikeys.revoke(u1key.id, user_id=admin_user.id) is False, (
        "revoke must refuse a key owned by another user"
    )
    assert apikeys.verify_key(u1full) is not None, "guarded revoke must not deactivate"
    print("  guard: revoke(key, user_id=other) refused; key still valid")

    # --- admin revoke-all for a user ---
    apikeys.create_key(u1.id, "user1 second key")
    r = c.post(f"/users/{u1.id}/apikeys/revoke", follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text)
    assert apikeys.verify_key(u1full) is None, "admin revoke-all must kill the key"
    assert all(not k.active for k in apikeys.list_keys(u1.id)), "all keys must be inactive"
    users_page = c.get("/users").text
    assert "API keys" in users_page, "users page must show the API keys column"
    print("  admin: POST /users/{uid}/apikeys/revoke deactivates every key")

    # --- tampered / unknown keys are rejected ---
    _k, good = apikeys.create_key(admin_user.id, "tamper test")
    tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
    assert apikeys.verify_key(good) is not None
    assert apikeys.verify_key(tampered) is None, "tampered key must be rejected"
    assert apikeys.verify_key("lsu_totally-made-up") is None
    assert apikeys.verify_key("") is None
    print("  verify_key: tampered/unknown/empty keys rejected")

    # --- system-wide enumeration: SAFE metadata (never the key material/hash) ------
    from datetime import datetime as _dt

    _km, _kf = apikeys.create_key(admin_user.id, "meta test key")
    with Session(engine) as s:
        row = s.get(type(_km), _km.id)
        row.last_used = _dt(2026, 1, 2, 3, 4, 5)
        s.add(row)
        s.commit()
        s.refresh(row)
        md = apikeys.key_metadata_dict(row, "admin")
    # The metadata NEVER carries the key material or the sha256 hash. The `prefix`
    # is the 12-char PUBLIC display prefix (already shown on /account), not a secret.
    assert "key_hash" not in md and "api_key" not in md, md
    assert len(md["prefix"]) <= 12, md
    assert row.key_hash not in md.values(), "the sha256 hash must never surface"
    assert md["last_used"] == "2026-01-02T03:04:05", md
    assert md["id"] == _km.id and md["user"] == "admin" and md["active"] is True, md
    assert set(md) == {
        "id",
        "user",
        "user_id",
        "name",
        "prefix",
        "scopes",
        "active",
        "created_at",
        "last_used",
        "expires_at",
        "expired",
    }, md
    # no-expiry key (the pre-existing shape): expires_at None, never expired
    assert md["expires_at"] is None and md["expired"] is False, md
    print(
        "  metadata: key_metadata_dict carries id/user/name/scopes/last_used/expires, "
        "NEVER key_hash or the key value"
    )

    # --- list_keys_scoped: an org admin's org filter EXCLUDES another org's keys ----
    # (the project's most reliably-found defect class -- probed directly at the seam.)
    with Session(engine) as s:
        oa = Organization(name="AK Org A")
        ob = Organization(name="AK Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    ua = security.create_user("ak_a@x.com", "AK A", "admin", "akpass-a-12345")
    ub = security.create_user("ak_b@x.com", "AK B", "admin", "akpass-b-12345")
    with Session(engine) as s:
        s.get(type(ua), ua.id).org_id = oa_id
        s.get(type(ub), ub.id).org_id = ob_id
        s.add(s.get(type(ua), ua.id))
        s.add(s.get(type(ub), ub.id))
        s.commit()
    apikeys.create_key(ua.id, "A-only key")
    apikeys.create_key(ub.id, "B-only key")
    with Session(engine) as s:
        a_keys = apikeys.list_keys_scoped(s, org_id=oa_id)
        b_keys = apikeys.list_keys_scoped(s, org_id=ob_id)
        all_keys = apikeys.list_keys_scoped(s, org_id=None)
    a_users = {k["user"] for k in a_keys}
    b_users = {k["user"] for k in b_keys}
    assert "ak_a@x.com" in a_users and "ak_b@x.com" not in a_users, a_users
    assert "ak_b@x.com" in b_users and "ak_a@x.com" not in b_users, b_users
    # The unscoped (trusted operator) view sees BOTH orgs' keys.
    all_users = {k["user"] for k in all_keys}
    assert {"ak_a@x.com", "ak_b@x.com"} <= all_users, all_users
    # per-owner narrowing
    one = _list_owner(oa_id, ua.id)
    assert one and all(k["user_id"] == ua.id for k in one), one
    print(
        "  isolation: list_keys_scoped(org_id=A) excludes org B keys; "
        "unscoped sees both; user_id narrows to one owner"
    )

    # --- CLI `apikey list` shows the last_used column and NEVER the key value ------
    settings.set_many({"multi_org_enabled": "0"})
    settings.invalidate_cache()
    code, out = _run_cli(["apikey", "list", "--user", "admin"])
    assert code == 0, out
    assert "last_used" in out, "CLI apikey list must include a last_used column"
    # The table shows only the 12-char public prefix (lsu_XXXXXXXX), never a full key.
    import re as _re2

    assert not _re2.search(r"lsu_[A-Za-z0-9_-]{13,}", out), (
        "CLI apikey list must NEVER print full key material (only the 12-char prefix)"
    )
    code, jout = _run_cli(["--json", "apikey", "list", "--user", "admin"])
    assert code == 0, jout
    import json as _json

    body = _json.loads(jout.strip().splitlines()[-1])
    assert body["ok"] and body["keys"], body
    assert all("key_hash" not in k and "last_used" in k for k in body["keys"]), body
    print("  CLI: `apikey list` human+json carry last_used, never the key/hash")

    # --- Q1: unknown scopes are REFUSED at every mint surface ---------------------
    # The service seam: create_key raises InvalidScopeError naming the vocabulary and
    # the offending token; a key that would authorize nothing is never minted.
    raised = None
    try:
        apikeys.create_key(admin_user.id, "q1 bad", scopes="all")
    except apikeys.InvalidScopeError as exc:
        raised = str(exc)
    assert raised is not None, "create_key must REFUSE scopes='all' (Q1)"
    assert "all" in raised and "docs" in raised and "keys" in raised and "admin" in raised, (
        f"the refusal must name the offending token AND the valid vocabulary: {raised!r}"
    )
    # A garbage token mixed with a valid one is still refused, naming only the bad one.
    try:
        apikeys.create_key(admin_user.id, "q1 bad2", scopes="docs bogus")
        assert False, "a mix of valid+unknown scopes must still be refused"
    except apikeys.InvalidScopeError as exc:
        assert "bogus" in str(exc), str(exc)
    # Valid combos still mint, and 'api' is always implied.
    for req, want in (
        ("docs", {"api", "docs"}),
        ("docs,keys", {"api", "docs", "keys"}),
        ("admin", {"api", "admin"}),
        ("", {"api"}),
    ):
        k, _f = apikeys.create_key(admin_user.id, "q1 ok", scopes=req)
        assert apikeys.scope_set(k.scopes) == want, (req, k.scopes)
    print(
        "  Q1 service: create_key refuses unknown scopes (names vocabulary), valid "
        "combos mint, 'api' implied"
    )

    # The CLI `apikey issue --scopes all`: nonzero exit, error names the vocabulary,
    # and NO key is minted.
    n_before = len(apikeys.list_keys(admin_user.id))
    code, out = _run_cli(["apikey", "issue", "--user", "admin", "--scopes", "all"])
    assert code != 0, f"apikey issue --scopes all must fail: {out}"
    assert "unknown scope" in out.lower() and "all" in out.lower(), out
    assert len(apikeys.list_keys(admin_user.id)) == n_before, "a refused mint must not create a key"
    # a valid CLI mint still works
    code2, out2 = _run_cli(["apikey", "issue", "--user", "admin", "--scopes", "docs,keys"])
    assert code2 == 0, out2
    print("  Q1 CLI: `apikey issue --scopes all` refused (no key minted); valid mint OK")

    # The /api mint route returns a clean 400 (not a 500) for an unknown scope.
    _q1m, q1key = apikeys.create_key(admin_user.id, "q1 api caller", scopes="api docs")
    r = c.post("/api/apikeys/issue", json={"scopes": "all"}, headers={"X-Loc-Token": q1key})
    assert r.status_code == 400, (r.status_code, r.text)
    body = r.json()
    assert body.get("ok") is False and "unknown scope" in body.get("error", "").lower(), body
    print("  Q1 /api: POST /api/apikeys/issue with scopes='all' -> clean 400")

    settings.set_many({"security_auth_required": "0"})
    print("APIKEYS OK")


def _list_owner(org_id, user_id):
    with Session(engine) as s:
        return apikeys.list_keys_scoped(s, org_id=org_id, user_id=user_id)


def _run_cli(argv):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as _cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = _cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue() + err.getvalue()


if __name__ == "__main__":
    main()
