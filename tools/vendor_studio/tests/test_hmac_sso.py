"""HMAC signature verify (good/bad/replay + ordering) and SSO token verify
(good/expired/bad-sig). These are the wire security invariants."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from conftest import sample_contract
from vstudio import contract as C
from vstudio import security


def _sign(raw: bytes, secret="test-signing-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_signature_good_and_bad(env):
    raw = b'{"hello":"world"}'
    assert security.verify_signature("test-signing-secret", raw, _sign(raw)) is True
    # bare hex (no prefix) is accepted too.
    bare = hmac.new(b"test-signing-secret", raw, hashlib.sha256).hexdigest()
    assert security.verify_signature("test-signing-secret", raw, bare) is True
    # wrong secret / wrong body / wrong length / non-hex / empty all fail.
    assert not security.verify_signature("test-signing-secret", raw, _sign(raw, "nope"))
    assert not security.verify_signature("test-signing-secret", b"other", _sign(raw))
    assert not security.verify_signature("test-signing-secret", raw, "sha256=deadbeef")
    assert not security.verify_signature("test-signing-secret", raw, "sha256=zz" * 16)
    assert not security.verify_signature("test-signing-secret", raw, "")
    # no secret = no valid signature (never "signature optional").
    assert not security.verify_signature("", raw, _sign(raw, ""))


def test_intake_route_verifies_before_parsing(env):
    """Ordering: a NON-JSON body with a BAD signature is a 401 (signature), never a 400
    (parse) — the parser is never reached by an unsigned caller."""
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    garbage = b"\xff\xfe not json {{{"
    r = c.post(
        "/intake/dispatch", content=garbage, headers={C.SIGNATURE_HEADER: "sha256=" + "ab" * 32}
    )
    assert r.status_code == 401, r.text
    # ...and a well-signed garbage body IS reached by the parser -> 400.
    r2 = c.post("/intake/dispatch", content=garbage, headers={C.SIGNATURE_HEADER: _sign(garbage)})
    assert r2.status_code == 400, r2.text


def test_intake_route_accepts_a_signed_dispatch(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    raw = json.dumps(sample_contract()).encode()
    r = c.post(
        "/intake/dispatch",
        content=raw,
        headers={C.SIGNATURE_HEADER: _sign(raw), C.CLAIM_HEADER: "claim-tok-123"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_replay_of_a_signed_dispatch_is_idempotent(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    raw = json.dumps(sample_contract()).encode()
    hdr = {C.SIGNATURE_HEADER: _sign(raw), C.CLAIM_HEADER: "claim-tok-123"}
    a = c.post("/intake/dispatch", content=raw, headers=hdr).json()
    b = c.post("/intake/dispatch", content=raw, headers=hdr).json()
    assert a["project_id"] == b["project_id"]  # same project, not a duplicate


# --- SSO ---------------------------------------------------------------------
def test_sso_token_good_expired_and_bad_sig(env):
    now = int(time.time())
    tok = security.mint_sso_token("alice@example.com", now=now)
    assert security.verify_sso_token(tok, now=now) == "alice@example.com"
    # expired (past the 60s TTL).
    assert security.verify_sso_token(tok, now=now + 61) is None
    # tampered signature.
    body, _, sig = tok.partition(".")
    forged = body + "." + ("0" * len(sig))
    assert security.verify_sso_token(forged, now=now) is None
    # tampered payload (signature no longer matches).
    other = security.mint_sso_token("bob", now=now)
    spliced = body + "." + other.partition(".")[2]
    assert security.verify_sso_token(spliced, now=now) is None
    # a blank SSO secret disables SSO entirely.
    assert security.verify_sso_token(tok, secret="", now=now) is None


def test_sso_route_sets_a_session_cookie(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    tok = security.mint_sso_token("carol")
    # L1: the handoff is a POST with the token in the BODY (never the query string).
    r = c.post("/sso", data={"token": tok}, follow_redirects=False)
    assert r.status_code == 303
    assert security.COOKIE_NAME in r.cookies or "set-cookie" in {k.lower() for k in r.headers}
    # a bad token is a 403 wall, not a redirect.
    bad = c.post("/sso", data={"token": "not-a-token"}, follow_redirects=False)
    assert bad.status_code == 403
    # a bare GET /sso is just the login wall (200), never a token sink.
    assert c.get("/sso", follow_redirects=False).status_code == 200


def test_unauthenticated_pages_are_a_404_wall(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    assert c.get("/", follow_redirects=False).status_code == 404
    assert c.get("/projects/1", follow_redirects=False).status_code == 404
    # health is always open.
    assert c.get("/health").status_code == 200
