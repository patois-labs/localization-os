"""Fix-round regressions: the BLOCKER + H1/H2 + M1-M9 + L2/L4 items. Each pins an
invariant so the corresponding review mutation now fails."""

from __future__ import annotations

import json
import os
import time

import pytest
from conftest import sample_contract
from vstudio import contract as C
from vstudio import security
from vstudio.models import VProjectStatus, VSegment


def _mk_client(env, role="Reviewer"):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    c.cookies.set(
        security.COOKIE_NAME,
        security.encode_session({"authed": True, "user": "t", "name": "t", "role": role}),
    )
    return c


def _intake(session, claim="c1"):
    from vstudio import intake

    return intake.store_dispatch(session, sample_contract(claim=claim))


# ============================================================================
# ADVISORY QA (decided 2026-08-08) — findings NEVER block delivery; the snapshot rides it
# ============================================================================
class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True, "complete": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _recording_poster(captured, status=200, body=None):
    def poster(payload, claim, *, sign=True, secret=None, base_url=None, timeout=20.0):
        captured.append({"payload": payload, "claim": claim})
        return _FakeResp(status, body)

    return poster


def test_deliver_succeeds_despite_findings_and_snapshot_rides(env, session):
    """FLIPPED from the old blocking-gate test: a confirmed-but-placeholder-broken segment
    no longer 409s. Delivery SUCCEEDS, and the advisory findings snapshot rides the delivery
    record (`qa_snapshot`). Enforcement moved to Localization OS's admission seam — proven
    end-to-end in tests/verify_vendor_studio.py's placeholder-advisory scenario."""
    from vstudio import delivery, editor, qa

    proj = _intake(session)
    segs = session.exec(
        __import__("sqlmodel")
        .select(VSegment)
        .where(VSegment.project_id == proj.id)
        .order_by(VSegment.order_index)
    ).all()
    # Drop ⟦x1⟧ from seg 1 (a placeholder_mismatch finding) and confirm it.
    editor.patch_target(session, proj, segs[0], "Hola mundo sin etiqueta", actor="t")
    editor.confirm_segment(session, proj, segs[0], actor="t")
    # Fill + confirm the rest cleanly so the full tranche has a target for every id.
    for s in segs[1:]:
        editor.patch_target(session, proj, s, f"[{s.source}]", actor="t")
        editor.confirm_segment(session, proj, s, actor="t")
    cap = []
    out = delivery.deliver(session, proj, mode="full", poster=_recording_poster(cap), cost=10.0)
    # It DELIVERED (no gate refusal) — every id posted exactly once.
    assert out["ok"] and out["status_code"] == 200 and out["complete"], out
    assert sorted(out["delivered"]) == ["1", "2", "3"]
    assert len(cap) == 1 and len(cap[0]["payload"]["segments"]) == 3
    # ...and the advisory findings snapshot rides the delivery record.
    snap = out["qa_snapshot"]
    assert snap["open"] >= 1, snap
    assert any(f["check"] == qa.PLACEHOLDER_MISMATCH for f in snap["findings"]), snap


def test_partial_ignores_caller_supplied_ids(env, session):
    """The reviewer shipped empty/broken content by choosing ids. The route must ignore a
    caller `ids` field entirely (server computes the confirmed subset)."""
    proj = _intake(session)
    segs = session.exec(
        __import__("sqlmodel")
        .select(VSegment)
        .where(VSegment.project_id == proj.id)
        .order_by(VSegment.order_index)
    ).all()
    c = _mk_client(env, "PM")
    # No segment is confirmed. Even if the caller names ids, there is nothing to deliver.
    r = c.post(
        f"/projects/{proj.id}/deliver-partial",
        json={"ids": [segs[0].contract_id, segs[1].contract_id], "cost": 999},
    )
    assert r.status_code == 409, ("caller ids must not force an empty-target delivery", r.text)
    # #516 negative: the same route is closed to a non-PM session (was the PM client's job).
    rv = _mk_client(env, "Reviewer").post(
        f"/projects/{proj.id}/deliver-partial",
        json={"ids": [segs[0].contract_id], "cost": 999},
    )
    assert rv.status_code == 403, rv.text


# ============================================================================
# H1 — no innerHTML sink for wire-derived strings in the advisor card path
# ============================================================================
def test_editor_js_has_no_innerhtml_sink():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "editor.js"
    )
    js = open(path, encoding="utf-8").read()  # noqa: SIM115
    assert ".innerHTML" not in js, "editor.js must not use innerHTML (XSS sink)"
    assert "note.textContent = s.rationale" in js


def test_advisor_renders_wire_glossary_term_as_data(env, session):
    """A package glossary term carrying an XSS payload comes back as a plain STRING in the
    advice JSON (the front-end renders it via textContent)."""
    from vstudio import advisor

    payload = "<img src=x onerror=alert(1)>"
    gl = [{"term": "cat", "target": payload}]
    out = advisor.advise(1, "the cat sat", "el felino", gl, "es-ES")
    assert payload in out["rationale"] and isinstance(out["rationale"], str)


# ============================================================================
# H2 — WireLog flood control
# ============================================================================
def test_bad_signature_flood_is_coalesced(env):
    from fastapi.testclient import TestClient
    from sqlmodel import func, select
    from vstudio import intake
    from vstudio.app import app
    from vstudio.db import get_session
    from vstudio.models import WireLog

    intake._UNAUTH_BUCKET.clear()  # deterministic: don't inherit prior-test bucket state
    c = TestClient(app)
    posts = 400
    for _ in range(posts):
        c.post("/intake/dispatch", content=b"{}", headers={C.SIGNATURE_HEADER: "sha256=bad"})
    with get_session() as s:
        n = s.exec(select(func.count()).select_from(WireLog)).one()
        n = n[0] if isinstance(n, tuple) else n
    # The point: coalesced to a tiny fraction of `posts` (per-minute cap; tolerate one
    # minute-boundary crossing on a slow CI box), never one row per request.
    assert n <= 2 * intake.UNAUTH_MAX_PER_MINUTE + 2, ("flood not coalesced", n)
    assert n < posts // 4, ("flood must be heavily coalesced", n)


def test_poll_204_noop_is_not_logged(env, session):
    from fastapi.testclient import TestClient
    from sqlmodel import func, select
    from vstudio.app import app
    from vstudio.db import get_session
    from vstudio.models import WireLog

    _intake(session, claim="c-poll")  # project exists but not COMPLETED
    c = TestClient(app)
    for _ in range(20):
        assert c.get("/intake/poll", headers={C.CLAIM_HEADER: "c-poll"}).status_code == 204
    with get_session() as s:
        n = s.exec(select(func.count()).select_from(WireLog)).one()
        n = n[0] if isinstance(n, tuple) else n
    assert n == 0, ("a 204 poll no-op must not write a WireLog row", n)


# ============================================================================
# M1 — session cookie flags + expiry + startup guard
# ============================================================================
def test_cookie_flags_secure_httponly_samesite(monkeypatch, tmp_path):
    # Default (no insecure opt-out) -> Secure cookie.
    monkeypatch.delenv("VSTUDIO_COOKIE_INSECURE", raising=False)
    monkeypatch.setenv("VSTUDIO_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("VSTUDIO_SESSION_SECRET", "s")
    monkeypatch.setenv("VSTUDIO_SSO_SECRET", "z")
    from vstudio import db as vdb
    from vstudio import security as sec
    from vstudio.config import settings

    settings.reload()
    sec.reset_serializer()
    vdb.reset_engine()
    vdb.init_db()
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    r = c.post("/sso", data={"token": sec.mint_sso_token("carol")}, follow_redirects=False)
    setc = r.headers.get("set-cookie", "")
    low = setc.lower()
    assert "secure" in low and "httponly" in low and "samesite=lax" in low, setc
    assert "max-age" in low, setc


def test_expired_session_cookie_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("VSTUDIO_DB_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("VSTUDIO_SESSION_SECRET", "s")
    monkeypatch.setenv("VSTUDIO_SESSION_MAX_AGE", "1")
    monkeypatch.setenv("VSTUDIO_COOKIE_INSECURE", "1")
    from vstudio import security as sec
    from vstudio.config import settings

    settings.reload()
    sec.reset_serializer()
    tok = sec.encode_session({"authed": True, "user": "x"})
    assert sec.decode_session(tok) is not None
    # itsdangerous compares INTEGER-second ages (age > max_age), so sleep well past the
    # 1s window to make the integer age reliably >= 2 (avoids the age==1 boundary).
    time.sleep(2.2)
    assert sec.decode_session(tok) is None, "an expired session cookie must be rejected"


def test_startup_hard_fails_nonloopback_without_secret(monkeypatch):
    monkeypatch.delenv("VSTUDIO_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VSTUDIO_HOST", "0.0.0.0")
    monkeypatch.setenv("VSTUDIO_LOCSTUDIO_URL", "https://loc.example")
    from vstudio.config import settings

    settings.reload()
    with pytest.raises(RuntimeError):
        settings.assert_production_ready()


# ============================================================================
# M2 — no API docs
# ============================================================================
def test_docs_and_openapi_are_404(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path


# ============================================================================
# M3 — reviewer actions are session-gated + writability-guarded; autopilot session-gated
# ============================================================================
def test_reviewer_actions_blocked_on_delivered_or_cancelled(env, session):
    from vstudio import editor

    for st in (VProjectStatus.DELIVERED, VProjectStatus.CANCELLED):
        proj = _intake(session, claim="c-" + st)
        seg = session.exec(
            __import__("sqlmodel").select(VSegment).where(VSegment.project_id == proj.id)
        ).first()
        proj.status = st
        session.add(proj)
        session.commit()
        with pytest.raises(editor.EditError):
            editor.reviewer_approve(session, proj, seg, actor="rev")
        with pytest.raises(editor.EditError):
            editor.reviewer_reject(session, proj, seg, "x", actor="rev")


def test_autopilot_run_requires_a_session(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)  # no session cookie
    r = c.post("/autopilot/run", json={"scenario": "happy_path"})
    assert r.status_code == 404, "autopilot must be session-gated"


def test_autopilot_route_gated_by_master_switch(env, session, monkeypatch):
    """FINAL-1: the adversarial /autopilot/run route is 404 unless VSTUDIO_AUTOPILOT_ENABLED
    is set — even for a valid session (the switch is the gate, not the session)."""
    proj = _intake(session, claim="c-ap")
    c = _mk_client(env, "PM")
    # Flag unset (env default) -> 404 even with a session.
    r = c.post("/autopilot/run", json={"scenario": "silence", "project_id": proj.id})
    assert r.status_code == 404, "autopilot route must be OFF by default"
    # Flip the switch -> the route works ('silence' delivers nothing, so no Localization OS needed).
    monkeypatch.setenv("VSTUDIO_AUTOPILOT_ENABLED", "1")
    from vstudio.config import settings

    settings.reload()
    r2 = c.post("/autopilot/run", json={"scenario": "silence", "project_id": proj.id})
    assert r2.status_code == 200, ("flag ON + session must reach the route", r2.text)
    assert r2.json()["delivered"] is False
    # #516 negative: with the switch ON, a non-PM session is still refused (403, not 200).
    r3 = _mk_client(env, "Translator").post(
        "/autopilot/run", json={"scenario": "silence", "project_id": proj.id}
    )
    assert r3.status_code == 403, r3.text


# ============================================================================
# FINAL-2 — a QA dismissal does not survive an arbitrary rewrite (accurate bookkeeping)
# ============================================================================
def test_dismissal_cleared_on_rewrite_no_stale_bookkeeping(env, session):
    """QA is advisory now, but a dismissal ("I reviewed this finding") is stale once the text
    is rewritten. It must clear so the finding re-surfaces as open for a fresh look — no
    orphan VQAIgnore rows survive the rewrite."""
    from sqlmodel import select as _select
    from vstudio import editor, qa, qa_engine

    proj = _intake(session)
    seg = session.exec(
        _select(VSegment).where(VSegment.project_id == proj.id).order_by(VSegment.order_index)
    ).all()[1]
    # A dismissable double-space finding.
    editor.patch_target(session, proj, seg, "hola  mundo", actor="t")
    editor.confirm_segment(session, proj, seg, actor="t")
    f = next(
        x for x in qa_engine.run_findings(session, proj)[seg.id] if x["check"] == qa.DOUBLE_SPACE
    )
    qa_engine.ignore_finding(session, proj, seg.id, qa.DOUBLE_SPACE, f["detail_sig"], "ok")
    assert (
        next(
            x
            for x in qa_engine.run_findings(session, proj)[seg.id]
            if x["check"] == qa.DOUBLE_SPACE
        )["ignored"]
        is True
    )
    # Rewrite the target (still carries a double space, but it is clearly a new edit). The
    # dismissal must NOT carry over.
    editor.patch_target(session, proj, seg, "otra  cosa  distinta", actor="t")
    still = [
        x for x in qa_engine.run_findings(session, proj)[seg.id] if x["check"] == qa.DOUBLE_SPACE
    ]
    assert still and still[0]["ignored"] is False, (
        "a QA dismissal must not survive a rewrite (stale bookkeeping)"
    )
    # And there are no orphan VQAIgnore rows for this segment.
    from vstudio.models import VQAIgnore

    assert session.exec(_select(VQAIgnore).where(VQAIgnore.segment_id == seg.id)).all() == []


# ============================================================================
# M4 — body cap on ALL JSON routes
# ============================================================================
def test_json_body_cap_413(env, session):
    proj = _intake(session)
    c = _mk_client(env)
    big = {"reason": "x" * (300 * 1024)}
    r = c.post(f"/projects/{proj.id}/qa/ignore", json=big)
    assert r.status_code == 413, r.status_code


# ============================================================================
# M5 — qa/ignore input validation (422 not 500)
# ============================================================================
def test_qa_ignore_validates_segment_id(env, session):
    proj = _intake(session)
    c = _mk_client(env)
    # missing segment_id
    assert (
        c.post(f"/projects/{proj.id}/qa/ignore", json={"check_key": "double_space"}).status_code
        == 422
    )
    # non-integer
    assert c.post(f"/projects/{proj.id}/qa/ignore", json={"segment_id": "abc"}).status_code == 422
    # foreign segment id
    assert (
        c.post(
            f"/projects/{proj.id}/qa/ignore",
            json={"segment_id": 999999, "check_key": "double_space"},
        ).status_code
        == 422
    )


# ============================================================================
# M6 — poll: unknown claim -> 204 (no oracle)
# ============================================================================
def test_poll_unknown_claim_is_204(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    assert c.get("/intake/poll", headers={C.CLAIM_HEADER: "no-such-claim"}).status_code == 204


# ============================================================================
# M7 — apikey rate limit + https startup guard
# ============================================================================
def test_apikey_endpoint_is_rate_limited(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    codes = [
        c.post("/login/apikey", data={"api_key": "nope"}, follow_redirects=False).status_code
        for _ in range(8)
    ]
    assert 429 in codes, ("the apikey endpoint must rate-limit", codes)


def test_apikey_verify_uses_x_loc_token_header(env, monkeypatch):
    """The fallback verifies a Localization OS key by calling /api/account. Localization OS's
    /api authenticates ONLY via the X-Loc-Token header (app/web/deps.py) — a Bearer
    Authorization header is ignored, so the verify MUST send X-Loc-Token or it can never
    succeed. Stub the wire call and assert the header the fallback actually sends."""
    import vstudio.app as vapp
    from fastapi.testclient import TestClient

    # The per-IP rate-limit bucket is module-level and persists across tests in this
    # process; the rate-limit test above can leave it exhausted for the loopback IP.
    # Clear it so this test exercises the verify path, not the 429 branch.
    vapp._APIKEY_BUCKET.clear()

    seen = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "role": "admin"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            return _FakeResp()

    monkeypatch.setattr(vapp.httpx, "Client", _FakeClient)
    c = TestClient(vapp.app)
    r = c.post("/login/apikey", data={"api_key": "lsu_secret_key"}, follow_redirects=False)
    # A 200 from the stubbed verify means the login succeeded (303 redirect to /).
    assert r.status_code == 303, ("verify should have succeeded", r.status_code)
    assert seen["headers"].get("X-Loc-Token") == "lsu_secret_key", seen["headers"]
    assert "Authorization" not in seen["headers"], (
        "the key must not be sent as a Bearer Authorization header",
        seen["headers"],
    )


def test_startup_requires_https_locstudio_when_nonloopback(monkeypatch):
    monkeypatch.setenv("VSTUDIO_HOST", "0.0.0.0")
    monkeypatch.setenv("VSTUDIO_SESSION_SECRET", "s")
    monkeypatch.setenv("VSTUDIO_LOCSTUDIO_URL", "http://loc.example")  # not https
    from vstudio.config import settings

    settings.reload()
    with pytest.raises(RuntimeError):
        settings.assert_production_ready()


# ============================================================================
# M8 — security headers on every response
# ============================================================================
def test_security_headers_present(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    r = c.get("/health")
    assert "Content-Security-Policy" in r.headers
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "no-referrer"


# ============================================================================
# M9 — advice fan-out cap
# ============================================================================
def test_advice_batch_is_capped(env, session):
    from vstudio.models import VSegment

    proj = _intake(session, claim="c-batch")
    # Add 30 more segments so the batch exceeds the cap of 25.
    for i in range(30):
        session.add(
            VSegment(
                project_id=proj.id,
                contract_id=f"x{i}",
                order_index=10 + i,
                source=f"word {i}",
                target=f"palabra {i}",
            )
        )
    session.commit()
    c = _mk_client(env)
    out = c.post(f"/projects/{proj.id}/qa/advice", json={}).json()
    assert len(out["suggestions"]) <= 25, len(out["suggestions"])
    assert out["capped"] is True and out["remaining"] >= 1


# ============================================================================
# L2 — logout is POST; GET shows a confirmation page
# ============================================================================
def test_logout_is_post(env):
    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)
    assert c.get("/logout").status_code == 200  # confirmation page
    assert c.post("/logout", follow_redirects=False).status_code == 303


# ============================================================================
# L4 — re-intake same claim, different payload -> 409
# ============================================================================
def test_reintake_conflict_on_changed_payload(env):
    import hashlib as _h
    import hmac as _hm

    from fastapi.testclient import TestClient
    from vstudio.app import app

    c = TestClient(app)

    def sign(raw):
        return "sha256=" + _hm.new(b"test-signing-secret", raw, _h.sha256).hexdigest()

    a = json.dumps(sample_contract(claim="dup")).encode()
    assert (
        c.post("/intake/dispatch", content=a, headers={C.SIGNATURE_HEADER: sign(a)}).status_code
        == 200
    )
    # same payload again -> idempotent 200
    assert (
        c.post("/intake/dispatch", content=a, headers={C.SIGNATURE_HEADER: sign(a)}).status_code
        == 200
    )
    # different payload, same claim -> 409
    changed = sample_contract(claim="dup")
    changed["segments"][0]["source"] = "A totally different source."
    b = json.dumps(changed).encode()
    assert (
        c.post("/intake/dispatch", content=b, headers={C.SIGNATURE_HEADER: sign(b)}).status_code
        == 409
    )


# ============================================================================
# #331 - uvicorn proxy headers are OFF by default: X-Forwarded-For from a
# same-host peer must not rotate the per-IP /login/apikey rate-limit key.
# ============================================================================
def _load_vstudio_run():
    """Load tools/vendor_studio/run.py BY PATH (the repo root also has a run.py; a plain
    `import run` could resolve the main app's entry point instead)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
    spec = importlib.util.spec_from_file_location("vstudio_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_uvicorn_kwargs(monkeypatch):
    """The EXACT kwargs the real entry point hands to uvicorn.run(): load run.py and call
    main() with uvicorn.run stubbed out (the stub only CAPTURES; nothing is skipped)."""
    import uvicorn

    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    _load_vstudio_run().main()
    assert captured.pop("app") == "vstudio.app:app"
    return captured


def _start_live_vstudio(uvicorn_kwargs):
    """Boot the app on a real uvicorn server with run.py's proxy kwargs (the bind is the
    loopback port under test, so host/port/reload are not reused). Returns
    (port, server, thread)."""
    import socket
    import threading

    import uvicorn

    proxy_kwargs = {k: v for k, v in uvicorn_kwargs.items() if k not in ("host", "port", "reload")}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            "vstudio.app:app",
            host="127.0.0.1",
            port=port,
            log_level="critical",
            **proxy_kwargs,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not come up"
    return port, server, thread


def _stop_live(server, thread):
    server.should_exit = True
    thread.join(timeout=30)
    assert not thread.is_alive(), "uvicorn server did not stop"


def _rotate_apikey_login(port, attempts=8):
    """POST /login/apikey `attempts` times from the loopback peer, each time with a fresh
    X-Forwarded-For (the #331 spoof). Returns the status codes. With no Localization OS URL
    configured every non-limited attempt is a 403 that makes no wire call."""
    import httpx
    import vstudio.app as vapp

    vapp._APIKEY_BUCKET.clear()
    codes = []
    with httpx.Client(timeout=10.0) as h:
        for i in range(attempts):
            r = h.post(
                f"http://127.0.0.1:{port}/login/apikey",
                data={"api_key": "nope"},
                headers={"X-Forwarded-For": f"198.51.100.{i}"},
            )
            codes.append(r.status_code)
    return codes


def test_run_serves_with_proxy_headers_off_by_default(env, monkeypatch):
    """Default (no trusted-proxy setting): run.py must hand uvicorn proxy_headers=False.
    uvicorn's own default trusts X-Forwarded-For from loopback peers - that is the #331
    spoof - so "absent" is not an acceptable value here."""
    kwargs = _run_uvicorn_kwargs(monkeypatch)
    assert kwargs.get("proxy_headers") is False, kwargs
    assert "forwarded_allow_ips" not in kwargs, kwargs


def test_run_trusts_proxy_headers_only_for_the_configured_proxy(env, monkeypatch):
    """The ONE way proxy trust turns on: the explicit trusted-proxy setting, whose VALIDATED
    value is passed through as forwarded_allow_ips (loopback is never implicitly trusted)."""
    from vstudio.config import settings

    monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", "192.0.2.10")
    settings.reload()
    try:
        kwargs = _run_uvicorn_kwargs(monkeypatch)
        assert kwargs.get("proxy_headers") is True, kwargs
        assert kwargs.get("forwarded_allow_ips") == "192.0.2.10/32", kwargs
    finally:
        monkeypatch.delenv("VSTUDIO_TRUSTED_PROXY_IPS", raising=False)
        settings.reload()


@pytest.mark.parametrize("off", ["", "   ", "\t"])
def test_run_keeps_proxy_headers_off_for_an_empty_setting(env, monkeypatch, off):
    """Empty / whitespace-only stays the OFF setting: proxy_headers=False and no
    forwarded_allow_ips at all (the socket peer IS the client IP)."""
    from vstudio.config import settings

    monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", off)
    settings.reload()
    try:
        kwargs = _run_uvicorn_kwargs(monkeypatch)
        assert kwargs.get("proxy_headers") is False, kwargs
        assert "forwarded_allow_ips" not in kwargs, kwargs
    finally:
        monkeypatch.delenv("VSTUDIO_TRUSTED_PROXY_IPS", raising=False)
        settings.reload()


@pytest.mark.parametrize("bad", ["*", " * ", "0.0.0.0/0", "::/0", "10.0.0.0/7", "nope"])
def test_run_refuses_a_broad_or_unparseable_trusted_proxy(env, monkeypatch, bad):
    """None of these may reach uvicorn (issue #331 fix round 1): `*` is uvicorn's
    trust-EVERY-peer switch (`0.0.0.0/0` / `::/0` are the same per family, `10.0.0.0/7` is a
    broad slice of it) and garbage would look configured while trusting nothing. The main
    app's `security.proxy_networks` rule refuses them all, so the entry point refuses to
    start, naming the bad entry."""
    from vstudio.config import settings

    monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", bad)
    settings.reload()
    try:
        with pytest.raises(ValueError) as excinfo:
            _run_uvicorn_kwargs(monkeypatch)
        assert repr(bad.strip()) in str(excinfo.value), (
            "the refusal must name the bad entry",
            str(excinfo.value),
        )
    finally:
        monkeypatch.delenv("VSTUDIO_TRUSTED_PROXY_IPS", raising=False)
        settings.reload()


def test_run_passes_the_normalized_trusted_proxy_list(env, monkeypatch):
    """A valid list (the Fly proxy peers; a bare IP and a CIDR mixed) is accepted and handed
    to uvicorn NORMALIZED, never raw: each entry parsed and joined as network strings."""
    from vstudio.config import settings

    monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", "172.16.7.146/32, 172.19.7.145")
    settings.reload()
    try:
        kwargs = _run_uvicorn_kwargs(monkeypatch)
        assert kwargs.get("proxy_headers") is True, kwargs
        assert kwargs.get("forwarded_allow_ips") == "172.16.7.146/32,172.19.7.145/32", kwargs
    finally:
        monkeypatch.delenv("VSTUDIO_TRUSTED_PROXY_IPS", raising=False)
        settings.reload()


# ============================================================================
# #416 - behind a platform proxy every socket peer is the proxy: an empty trusted-proxy
# setting is a loud boot error.
# ============================================================================
def test_run_on_fly_without_trusted_proxy_logs_a_boot_error(env, monkeypatch, capsys):
    from vstudio.config import settings

    monkeypatch.setenv("FLY_APP_NAME", "vendor-studio")
    monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", "")
    settings.reload()
    try:
        _run_uvicorn_kwargs(monkeypatch)
        assert "VSTUDIO_TRUSTED_PROXY_IPS unset" in capsys.readouterr().err
        monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", "172.16.7.146/32")
        settings.reload()
        _run_uvicorn_kwargs(monkeypatch)
        assert "VSTUDIO_TRUSTED_PROXY_IPS unset" not in capsys.readouterr().err
        monkeypatch.delenv("FLY_APP_NAME")
        monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", "")
        settings.reload()
        _run_uvicorn_kwargs(monkeypatch)
        assert "ERROR" not in capsys.readouterr().err, "off Fly an empty setting is normal"
    finally:
        monkeypatch.delenv("VSTUDIO_TRUSTED_PROXY_IPS", raising=False)
        settings.reload()


def test_xff_from_a_local_peer_cannot_rotate_the_rate_limit(env, monkeypatch):
    """A REAL uvicorn server started with run.py's exact kwargs: 8 same-peer
    /login/apikey POSTs with a rotating X-Forwarded-For must still hit the per-IP limit
    (429) because the socket peer, not the header, is the rate-limit key by default."""
    from vstudio.config import settings

    monkeypatch.delenv("VSTUDIO_LOCSTUDIO_URL", raising=False)
    settings.reload()
    port, server, thread = _start_live_vstudio(_run_uvicorn_kwargs(monkeypatch))
    try:
        codes = _rotate_apikey_login(port)
        assert 429 in codes, ("XFF rotation defeated the per-IP rate limit", codes)
    finally:
        _stop_live(server, thread)


@pytest.mark.parametrize("on_fly", [False, True])
def test_xff_sets_the_rate_limit_key_when_a_trusted_proxy_is_configured(env, monkeypatch, on_fly):
    """With VSTUDIO_TRUSTED_PROXY_IPS naming the socket peer (127.0.0.1 here, standing
    in for a real fronting proxy) proxy headers go ON and the forwarded IP becomes the
    rate-limit key: the same rotating-XFF flood is no longer coalesced onto one bucket."""
    from vstudio.config import settings

    if on_fly:
        monkeypatch.setenv("FLY_APP_NAME", "vendor-studio")
    monkeypatch.delenv("VSTUDIO_LOCSTUDIO_URL", raising=False)
    monkeypatch.setenv("VSTUDIO_TRUSTED_PROXY_IPS", "127.0.0.1")
    settings.reload()
    port, server, thread = _start_live_vstudio(_run_uvicorn_kwargs(monkeypatch))
    try:
        codes = _rotate_apikey_login(port)
        assert codes == [403] * 8, ("trusted-peer XFF must set the rate-limit key", codes)
    finally:
        monkeypatch.delenv("VSTUDIO_TRUSTED_PROXY_IPS", raising=False)
        settings.reload()
        _stop_live(server, thread)
