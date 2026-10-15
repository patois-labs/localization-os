"""The vendor-RETURN surfaces over real HTTP (external-processing Wave B): the signed
public webhook, the /api verbs, and the web panel's manual re-upload form.

Hermetic + offline: the outbound dispatch that creates a parked leg rides an injected
httpx.MockTransport, and the resume is stubbed so no background workflow thread races the
assertions. Drives the real ASGI app through fastapi.testclient.

  [A] POST /webhooks/vendor-returns -- UNAUTHENTICATED at the session/API layer BY
      DESIGN and therefore SIGNED. A valid signature admits; a bad one, a missing one and
      an unknown claim token are all the SAME generic 403 (no enumeration oracle); an
      oversized body is 413 BEFORE the HMAC is ever computed; a late return is a loud 409;
      a bad contract id is a 400 that writes nothing.
  [B] /api/vendors/dispatches | /returns | /poll -- admin-only (H1, mirroring the rest of
      the vendor surface), route ordering proven against the sibling /api/vendors/{vid},
      and no response body ever carries the claim token or the signing secret.
  [C] the web panel's manual re-upload: admin-gated, CSRF-protected, and PINNED to the leg
      in the URL so a paste of the wrong vendor's file is refused rather than applied.
"""

import hashlib
import hmac
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_vendorretweb_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    Role,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import (
    apikeys,
    orchestrator,
    security,
    settings,
    vendor_dispatch,
    vendor_return,
    vendors,
)
from app.services import workflows as wf
from tests._csrf import csrf_for

init_db()

SIGNING = "web-signing-secret"
SOURCES = ["Hello world.", "The cat sat on the mat."]
GOOD = ["Hola mundo.", "El gato se sento en la alfombra."]
_SEQ = [0]

# The resume is stubbed DURING these tests: they exercise the HTTP surfaces, and a real
# background resume thread would race every assertion. The resume itself is proven
# end-to-end (sync AND exactly-once) in tests/verify_vendor_return.py.
#
# MEDIUM-7: the stub is installed by an AUTOUSE FIXTURE WITH TEARDOWN (not a module-level
# reassignment). A bare module-level monkeypatch leaked into every OTHER suite collected in
# the same pytest process -- turning the real resume into a no-op and silently breaking
# sibling suites' resume assertions (verify_vendor_return's happy path, verify_tm_match_step's
# lock-survives-resume), which is exactly why a same-process multi-file run was NOT green.
# The subprocess harness masked it because each suite runs in its own interpreter.
_RESUMED: list = []


def _install_resume_stub():
    """Replace the resume seams with recording no-ops; return the originals so a caller can
    restore them. Used by the autouse fixture (pytest) and the __main__ script path."""
    real = (orchestrator.resume_run_async, orchestrator.resume_workflow)
    set_pieces(
        "app.services.orchestrator",
        "resume_run_async",
        lambda s, rid, loc, txt, actor="", **kw: _RESUMED.append(rid),
    )
    set_pieces(
        "app.services.orchestrator",
        "resume_workflow",
        lambda s, rid, loc, txt, **kw: _RESUMED.append(rid),
    )
    return real


import pytest


@pytest.fixture(autouse=True)
def _resume_stubbed():
    """Per-test stub install + guaranteed restore, so nothing leaks past this module."""
    real = _install_resume_stub()
    try:
        yield
    finally:
        orchestrator.resume_run_async, orchestrator.resume_workflow = real


def _app():
    from app.main import app

    return app


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _admin_headers() -> dict:
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    n = _seq()
    u = security.create_user(f"vr-admin-{n}@x.com", "VR Admin", Role.ADMIN, "vr-admin-password-123")
    _row, raw = apikeys.create_key(u.id, f"vr-key-{n}", scopes="api docs")
    return {"X-Loc-Token": raw}


def _role_headers(role, email) -> dict:
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    u = security.create_user(email, email, role, "vr-role-password-123")
    _row, raw = apikeys.create_key(u.id, f"vr-key-{email}", scopes="api docs")
    return {"X-Loc-Token": raw}


def _mock_transport():
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def _seed_parked_leg():
    """A customer + project + segmented doc + linked vendor, run to a PARKED external
    leg. Returns the ids the return path needs."""
    with Session(engine) as s:
        cust = Customer(name=f"C-{_seq()}")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(
            name=f"P-{_seq()}", source_locale="en-US", default_provider="mock", customer_id=cust.id
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(SOURCES):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        v = Vendor(name=f"V-{_seq()}", endpoint_url="https://vendor.example.com/intake")
        v.secret_enc = vendors._enc_secret({"signing_secret": SIGNING})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust.id)
        w = Workflow(
            name=f"W-{_seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="Vendor review", step_type=StepType.EXTERNAL, vendor_id=v.id)
        wid, did, vid = w.id, d.id, v.id
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wid, did, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        segs = orchestrator._load_segments(s, did, "es-ES")
        return {
            "doc_id": did,
            "vendor_id": vid,
            "run_id": run.id,
            "leg_id": leg.id,
            "claim": leg.claim_token,
            "seg_ids": [sg.id for sg in segs],
        }


def _body(env, targets=None, *, ids=None, cost=None) -> bytes:
    payload = {
        "contract_version": "1.0",
        "claim_token": env["claim"],
        "target_locale": "es-ES",
        "segments": [
            {"id": i, "target": t}
            for i, t in zip(
                ids if ids is not None else env["seg_ids"],
                targets if targets is not None else GOOD,
                strict=False,
            )
        ],
    }
    if cost is not None:
        payload["actual_cost_usd"] = cost
    return json.dumps(payload).encode()


def _sig(raw: bytes, secret=SIGNING) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _targets(doc_id):
    with Session(engine) as s:
        return [sg.target_text or "" for sg in orchestrator._load_segments(s, doc_id, "es-ES")]


# ============================================================================
# [A] the signed public webhook
# ============================================================================
def test_webhook_admits_a_correctly_signed_return():
    env = _seed_parked_leg()
    _RESUMED.clear()
    api = TestClient(_app())
    raw = _body(env, cost=77.5)
    r = api.post(
        "/webhooks/vendor-returns",
        content=raw,
        headers={
            vendor_return.CLAIM_HEADER: env["claim"],
            vendor_return.SIGNATURE_HEADER: _sig(raw),
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200, (r.status_code, r.text)
    out = r.json()
    assert out["complete"] is True and len(out["admitted"]) == 2, out
    assert env["claim"] not in r.text and SIGNING not in r.text, "secret/token echoed"
    assert _targets(env["doc_id"]) == GOOD
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        assert leg.status == VendorDispatchStatus.RETURNED
        assert leg.actual_cost_usd == 77.5
    assert [env["run_id"]] == _RESUMED, "the completing return resumes the run once"
    print("  [A] a correctly signed callback admits, reconciles and resumes: OK")


def test_webhook_rejects_bad_signatures_uniformly():
    env = _seed_parked_leg()
    api = TestClient(_app())
    raw = _body(env)
    good_hdr = {vendor_return.CLAIM_HEADER: env["claim"], "content-type": "application/json"}
    cases = {
        "no signature header": {},
        "empty": {vendor_return.SIGNATURE_HEADER: ""},
        "too short": {vendor_return.SIGNATURE_HEADER: "sha256=deadbeef"},
        "non-hex": {vendor_return.SIGNATURE_HEADER: "sha256=" + "zz" * 32},
        "wrong secret": {vendor_return.SIGNATURE_HEADER: _sig(raw, "not-the-secret")},
        "right secret, other body": {vendor_return.SIGNATURE_HEADER: _sig(b'{"segments":[]}')},
    }
    bodies = set()
    for label, extra in cases.items():
        r = api.post("/webhooks/vendor-returns", content=raw, headers={**good_hdr, **extra})
        assert r.status_code == 403, (label, r.status_code, r.text)
        bodies.add(r.text)
    # An unknown claim token answers IDENTICALLY -- no existence oracle.
    r = api.post(
        "/webhooks/vendor-returns",
        content=raw,
        headers={
            vendor_return.CLAIM_HEADER: "not-a-real-token",
            vendor_return.SIGNATURE_HEADER: _sig(raw),
            "content-type": "application/json",
        },
    )
    assert r.status_code == 403
    bodies.add(r.text)
    # ...and no claim token at all.
    r = api.post(
        "/webhooks/vendor-returns", content=raw, headers={"content-type": "application/json"}
    )
    assert r.status_code == 403
    bodies.add(r.text)
    assert len(bodies) == 1, ("every auth failure must be byte-identical", bodies)
    assert _targets(env["doc_id"]) != GOOD, "a refused callback must write nothing"
    with Session(engine) as s:
        assert s.get(VendorDispatch, env["leg_id"]).status == VendorDispatchStatus.DISPATCHED
    print("  [A] every callback auth failure is ONE byte-identical 403; nothing written: OK")


def test_webhook_checks_the_signature_before_parsing_the_body():
    """A body that is not JSON at all, with a bad signature, must fail as a SIGNATURE
    error -- if the parser ran first, an unauthenticated caller would reach it."""
    env = _seed_parked_leg()
    api = TestClient(_app())
    garbage = b"{{{ not json \xff\xfe"
    r = api.post(
        "/webhooks/vendor-returns",
        content=garbage,
        headers={
            vendor_return.CLAIM_HEADER: env["claim"],
            vendor_return.SIGNATURE_HEADER: "sha256=" + "ab" * 32,
        },
    )
    assert r.status_code == 403, (r.status_code, r.text)
    assert "JSON" not in r.text, "the body was parsed before the signature was checked"
    # The SAME garbage, correctly signed, is a clean 400 contract error.
    r = api.post(
        "/webhooks/vendor-returns",
        content=garbage,
        headers={
            vendor_return.CLAIM_HEADER: env["claim"],
            vendor_return.SIGNATURE_HEADER: _sig(garbage),
        },
    )
    assert r.status_code == 400 and r.json()["applied"] is False, r.text
    print("  [A] the signature is verified BEFORE the body is deserialized: OK")


def test_webhook_caps_the_body_before_hashing_it():
    """The route is unauthenticated at the session layer, so an unbounded read would be a
    memory-exhaustion vector reachable with no credentials -- the cap fires first."""
    from app import config

    env = _seed_parked_leg()
    api = TestClient(_app())
    huge = b"x" * (config.WEBHOOK_MAX_BODY_BYTES + 1024)
    r = api.post(
        "/webhooks/vendor-returns",
        content=huge,
        headers={
            vendor_return.CLAIM_HEADER: env["claim"],
            vendor_return.SIGNATURE_HEADER: _sig(huge),
        },
    )
    assert r.status_code == 413, (r.status_code, r.text)
    print("  [A] an oversized callback body is 413 before the HMAC is computed: OK")


def test_webhook_drops_a_late_return_loudly_and_a_bad_id_cleanly():
    env = _seed_parked_leg()
    api = TestClient(_app())
    # (1) a bad contract id -> 400, nothing written, leg still open.
    raw = _body(env, ids=[env["seg_ids"][0], 987654])
    r = api.post(
        "/webhooks/vendor-returns",
        content=raw,
        headers={
            vendor_return.CLAIM_HEADER: env["claim"],
            vendor_return.SIGNATURE_HEADER: _sig(raw),
        },
    )
    assert r.status_code == 400 and r.json()["applied"] is False, r.text
    assert _targets(env["doc_id"])[0] != GOOD[0], "an unknown id admitted a sibling"
    # (2) cancel the leg, then a perfectly valid signed return -> 409, LOUD.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        leg.status = VendorDispatchStatus.CANCELLED
        s.add(leg)
        s.commit()
    raw = _body(env)
    r = api.post(
        "/webhooks/vendor-returns",
        content=raw,
        headers={
            vendor_return.CLAIM_HEADER: env["claim"],
            vendor_return.SIGNATURE_HEADER: _sig(raw),
        },
    )
    assert r.status_code == 409, (r.status_code, r.text)
    # L2: the webhook is vendor-facing (authenticated but UNTRUSTED), so the 409 body is
    # GENERIC -- it must NOT leak internal ids or the loud "DROPPED" detail (that stays in
    # the server log). applied is False; nothing was written.
    assert r.json()["applied"] is False, r.text
    assert "DROPPED" not in r.text and str(env["leg_id"]) not in r.text, (
        "L2: the webhook error response must not leak internal detail",
        r.text,
    )
    assert _targets(env["doc_id"]) != GOOD
    print(
        "  [A] a bad id is a clean 400 and a late return a generic 409; both write "
        "nothing (no internal detail leaked): OK"
    )


# ============================================================================
# [B] the /api verbs
# ============================================================================
def test_api_return_routes_are_admin_only_and_route_correctly():
    env = _seed_parked_leg()
    api = TestClient(_app())
    admin = _admin_headers()
    viewer = _role_headers(Role.VIEWER, f"vr-view-{_seq()}@x.com")
    translator = _role_headers(Role.TRANSLATOR, f"vr-trans-{_seq()}@x.com")
    for hdr, who in ((viewer, "viewer"), (translator, "translator")):
        assert api.get("/api/vendors/dispatches", headers=hdr).status_code == 403, who
        assert (
            api.post(
                "/api/vendors/returns", headers=hdr, json={"claim_token": env["claim"]}
            ).status_code
            == 403
        ), who
        assert api.post("/api/vendors/poll", headers=hdr, json={}).status_code == 403, who
    assert api.get("/api/vendors/dispatches").status_code == 401, "no token = 401"

    # ROUTE ORDERING: the literal /api/vendors/dispatches must not be swallowed by the
    # sibling /api/vendors/{vid} (which would 422 on a non-integer id).
    r = api.get("/api/vendors/dispatches", headers=admin)
    assert r.status_code == 200, (r.status_code, r.text)
    rows = r.json()["dispatches"]
    assert any(d["id"] == env["leg_id"] for d in rows), rows
    row = [d for d in rows if d["id"] == env["leg_id"]][0]  # noqa: RUF015
    assert row["outstanding_ids"] == 2 and row["endpoint_host"] == "vendor.example.com"
    assert env["claim"] not in r.text and SIGNING not in r.text, "secret/token echoed"
    print(
        "  [B] /api dispatch + return + poll are admin-only and route past /api/vendors/{vid}: OK"
    )


def test_api_manual_return_admits_through_the_same_guards():
    env = _seed_parked_leg()
    _RESUMED.clear()
    api = TestClient(_app())
    admin = _admin_headers()
    # A source echo is STILL a no-op on this surface -- the transport never changes the
    # judgement (segment 0 echoes the source; segment 1 is a real translation).
    r = api.post(
        "/api/vendors/returns",
        headers=admin,
        json={
            "claim_token": env["claim"],
            "target_locale": "es-ES",
            "segments": [
                {"id": env["seg_ids"][0], "target": SOURCES[0]},
                {"id": env["seg_ids"][1], "target": GOOD[1]},
            ],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["no_op"] == [str(env["seg_ids"][0])], out
    assert out["admitted"] == [str(env["seg_ids"][1])], out
    assert _targets(env["doc_id"])[0] != SOURCES[0], (
        "a source echo was written through the /api surface"
    )
    assert [env["run_id"]] == _RESUMED
    # A second POST against the now-closed leg is a loud 409.
    r = api.post(
        "/api/vendors/returns",
        headers=admin,
        json={
            "claim_token": env["claim"],
            "segments": [{"id": env["seg_ids"][0], "target": GOOD[0]}],
        },
    )
    assert r.status_code == 409 and "DROPPED" in r.text, r.text
    # An unknown token is a 404 with no detail.
    r = api.post(
        "/api/vendors/returns",
        headers=admin,
        json={"claim_token": "nope", "segments": [{"id": 1, "target": "x"}]},
    )
    assert r.status_code == 404, r.text
    print(
        "  [B] /api/vendors/returns applies the identical guards (source echo still a "
        "no-op) and drops a late repost: OK"
    )


def test_api_poll_refuses_a_vendor_without_a_poll_url():
    env = _seed_parked_leg()
    api = TestClient(_app())
    admin = _admin_headers()
    r = api.post("/api/vendors/poll", headers=admin, json={"dispatch_id": env["leg_id"]})
    assert r.status_code == 400 and "poll_url" in r.text, r.text
    # The bulk sweep simply finds nothing pollable (never an error).
    r = api.post("/api/vendors/poll", headers=admin, json={"vendor_id": env["vendor_id"]})
    assert r.status_code == 200 and r.json()["polled"] == 0, r.text
    print("  [B] /api/vendors/poll refuses an un-opted-in vendor and sweeps nothing: OK")


# ============================================================================
# [C] the web panel form
# ============================================================================
def _web_client_for(role):
    """A logged-in web (cookie) client for `role` (login wall ON, multi-org OFF)."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    n = _seq()
    email = f"vr-web-{n}@x.com"
    security.create_user(email, "VR Web", role, "vr-web-password-123")
    c = TestClient(_app())
    # Auth is on: the login POST itself carries a CSRF token, and a successful login
    # ROTATES the session, so the client's token has to be refreshed afterwards.
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": "vr-web-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    csrf_for(c)
    return c


def _web_admin_client():
    return _web_client_for(Role.ADMIN)


def _to_retry_wait(env):
    """Flip the parked DISPATCHED leg to RETRY_WAIT (a transient dispatch failure being
    retried), keeping the run PAUSED at the external step."""
    from datetime import datetime, timedelta

    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        leg.status = VendorDispatchStatus.RETRY_WAIT
        leg.attempt_count = 1
        leg.next_attempt_at = datetime.utcnow() + timedelta(minutes=5)
        leg.last_error = "dispatch to vendor.example.com returned HTTP 503 (retryable)."
        leg.retry_contract = {"claim_token": leg.claim_token, "segments": []}
        s.add(leg)
        s.commit()


def _to_dead_letter(env):
    """Flip the parked leg to DEAD_LETTER (escalated to a human), keeping the run PAUSED at
    the external step -- the resume IS legitimate here, but the surface must give context."""
    from datetime import datetime

    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        leg.status = VendorDispatchStatus.DEAD_LETTER
        leg.escalated_at = datetime.utcnow()
        leg.attempt_count = 6
        leg.retry_contract = None
        leg.next_attempt_at = None
        s.add(leg)
        s.commit()


def test_web_manual_return_is_pinned_to_the_leg_in_the_url():
    env = _seed_parked_leg()
    other = _seed_parked_leg()
    _RESUMED.clear()
    c = _web_admin_client()
    # The vendor detail page lists the open leg + its re-upload form.
    page = c.get(f"/vendors/{env['vendor_id']}")
    assert page.status_code == 200
    assert "Open dispatches" in page.text
    assert f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return" in page.text
    assert env["claim"] not in page.text, "the claim token must never render in the page"

    # A paste of ANOTHER leg's return against this leg's URL is refused, not applied.
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _body(other).decode()},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers
    assert _targets(other["doc_id"]) != GOOD, "the wrong-leg paste was applied"

    # The right file against the right URL admits.
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _body(env).decode()},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    assert _targets(env["doc_id"]) == GOOD
    assert [env["run_id"]] == _RESUMED
    print(
        "  [C] the web re-upload form is pinned to the leg in its URL and admits "
        "through the same pipeline: OK"
    )


def test_web_return_form_is_admin_gated_and_csrf_protected():
    env = _seed_parked_leg()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    n = _seq()
    email = f"vr-webtrans-{n}@x.com"
    security.create_user(email, "VR Trans", Role.TRANSLATOR, "vr-web-password-123")
    c = TestClient(_app())
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": "vr-web-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    csrf_for(c)
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _body(env).decode()},
        follow_redirects=False,
    )
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert _targets(env["doc_id"]) != GOOD, "a translator admitted a vendor return"
    # No CSRF token at all (admin session): the middleware sends the form post back to
    # login rather than executing it.
    admin = _web_admin_client()
    admin.headers.pop("X-CSRF-Token", None)
    r = admin.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _body(env).decode()},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/auth/login" in r.headers["location"], r.headers
    assert _targets(env["doc_id"]) != GOOD, "a CSRF-less POST admitted a vendor return"
    # A leg belonging to ANOTHER vendor cannot be pinned against this vendor's URL.
    other = _seed_parked_leg()
    ok = _web_admin_client()
    r = ok.post(
        f"/vendors/{env['vendor_id']}/dispatches/{other['leg_id']}/return",
        data={"payload": _body(other).decode()},
        follow_redirects=False,
    )
    assert r.status_code == 404, r.status_code
    assert _targets(other["doc_id"]) != GOOD, "a cross-vendor leg id was applied"
    print("  [C] the web re-upload form is admin-gated and CSRF-protected: OK")


def test_human_resume_refused_on_a_vendor_pause():
    """HIGH-1: POST /runs/{id}/resume must refuse (409) while the run is parked at an
    external vendor step -- a human cannot 'approve' a vendor-parked leg (that would advance
    past the external step without the vendor's content and orphan the open leg)."""
    env = _seed_parked_leg()
    c = _web_admin_client()
    r = c.post(
        f"/runs/{env['run_id']}/resume",
        data={"locale": "es-ES", "edited_text": ""},
        follow_redirects=False,
    )
    assert r.status_code == 409, (r.status_code, r.text[:200])
    with Session(engine) as s:
        assert s.get(VendorDispatch, env["leg_id"]).status == VendorDispatchStatus.DISPATCHED
        assert s.get(WorkflowRun, env["run_id"]).status == RunStatus.PAUSED
    print("  [HIGH-1] a human resume is refused (409) while parked at a vendor step: OK")


def _wedge(env):
    """Drive the leg to the MEDIUM-4 WEDGE shape: admit + close the leg (RETURNED) but do
    NOT resume, so the run stays PAUSED at the external step with vendor content committed."""
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        vendor_return.admit_return(s, leg, _body(env).decode(), transport="manual", resume="none")
    with Session(engine) as s:
        assert s.get(VendorDispatch, env["leg_id"]).status == VendorDispatchStatus.RETURNED
        assert s.get(WorkflowRun, env["run_id"]).status == RunStatus.PAUSED
    assert _targets(env["doc_id"]) == GOOD, "vendor content must be committed by the wedge"


def test_web_human_resume_refused_on_a_wedged_returned_leg():
    """HIGH (round-3): the web resume route must refuse (409) a wedged RETURNED leg -- on the
    pre-fix tip the guard keyed on DISPATCHED only, so a wedged leg slipped through and a
    human 'Approve & resume' clobbered the vendor content with source."""
    env = _seed_parked_leg()
    _wedge(env)
    c = _web_admin_client()
    r = c.post(
        f"/runs/{env['run_id']}/resume",
        data={"locale": "es-ES", "edited_text": ""},
        follow_redirects=False,
    )
    assert r.status_code == 409, (r.status_code, r.text[:200])
    assert _targets(env["doc_id"]) == GOOD, "the vendor content must survive the refusal"
    print("  [HIGH-r3] web resume of a wedged RETURNED leg is refused (409): OK")


def test_api_human_resume_refused_on_a_wedged_returned_leg():
    """HIGH (round-3): the /api resume verb must also refuse (409) a wedged RETURNED leg."""
    env = _seed_parked_leg()
    _wedge(env)
    api = TestClient(_app())
    admin = _admin_headers()
    r = api.post(
        f"/api/runs/{env['run_id']}/resume",
        headers=admin,
        json={"locale": "es-ES", "edited_text": ""},
    )
    assert r.status_code == 409, (r.status_code, r.text[:200])
    assert "redrive" in r.text.lower(), r.text
    assert _targets(env["doc_id"]) == GOOD
    print("  [HIGH-r3] /api resume of a wedged RETURNED leg is refused (409): OK")


def test_run_view_shows_awaiting_redrive_for_a_wedged_leg():
    """HIGH (round-3) (b): run_view renders the non-actionable 'awaiting redrive' state, NOT
    the 'Approve & resume' card, for a wedged RETURNED leg."""
    env = _seed_parked_leg()
    _wedge(env)
    c = _web_admin_client()
    r = c.get(f"/runs/{env['run_id']}")
    assert r.status_code == 200
    assert "awaiting redrive" in r.text.lower(), r.text[:600]
    assert "Approve" not in r.text, "the Approve & resume card must NOT render for a wedge"
    print("  [HIGH-r3 b] run_view shows 'awaiting redrive', not Approve & resume: OK")


def test_api_dispatch_detail_route():
    """Wave C GET /api/vendors/dispatches/{did}: admin-only, carries the band breakdown +
    full estimate, 404 on a missing leg, and never echoes the claim token / signing secret
    (host only)."""
    env = _seed_parked_leg()
    api = TestClient(_app())
    admin = _admin_headers()
    viewer = _role_headers(Role.VIEWER, f"vd-view-{_seq()}@x.com")
    did = env["leg_id"]
    assert api.get(f"/api/vendors/dispatches/{did}").status_code == 401, "no token = 401"
    assert api.get(f"/api/vendors/dispatches/{did}", headers=viewer).status_code == 403, (
        "viewer forbidden"
    )
    r = api.get(f"/api/vendors/dispatches/{did}", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()["dispatch"]
    assert body["id"] == did
    assert isinstance(body.get("band_rows"), list) and body["band_rows"], "band_rows present"
    assert {row["band"] for row in body["band_rows"]} >= {"new", "exact"}, body["band_rows"]
    assert "estimate" in body
    assert body["endpoint_host"] == "vendor.example.com"
    assert env["claim"] not in r.text and SIGNING not in r.text, "secret/token echoed"
    assert api.get("/api/vendors/dispatches/99999999", headers=admin).status_code == 404, (
        "missing leg = 404"
    )
    print(
        "  [B] GET /api/vendors/dispatches/{did}: admin-only, band_rows+estimate, "
        "404 on missing, no secret echoed: OK"
    )


def test_api_dispatch_detail_cross_tenant_404():
    """Wave C cross-tenant wall on the new detail route: org B's admin gets an IDENTICAL
    404 for org A's leg (never enumerable), while org A's admin reads it 200."""
    from app.models import Organization, User

    env = _seed_parked_leg()
    with Session(engine) as s:
        oa = Organization(name=f"WCA-{_seq()}")
        ob = Organization(name=f"WCB-{_seq()}")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        org_a, org_b = oa.id, ob.id
        leg = s.get(VendorDispatch, env["leg_id"])
        leg.org_id = org_a
        v = s.get(Vendor, env["vendor_id"])
        v.org_id = org_a
        s.add(leg)
        s.add(v)
        s.commit()
        ua = security.create_user(f"wc-a-{_seq()}@x.com", "A", Role.ADMIN, "wc-password-1234")
        ub = security.create_user(f"wc-b-{_seq()}@x.com", "B", Role.ADMIN, "wc-password-1234")
        ra = s.get(User, ua.id)
        ra.org_id = org_a
        s.add(ra)
        rb = s.get(User, ub.id)
        rb.org_id = org_b
        s.add(rb)
        s.commit()
        _r, raw_a = apikeys.create_key(ua.id, f"wc-a-{_seq()}", scopes="api docs")
        _r, raw_b = apikeys.create_key(ub.id, f"wc-b-{_seq()}", scopes="api docs")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        api = TestClient(_app())
        did = env["leg_id"]
        assert (
            api.get(f"/api/vendors/dispatches/{did}", headers={"X-Loc-Token": raw_a}).status_code
            == 200
        ), "org A owns it"
        assert (
            api.get(f"/api/vendors/dispatches/{did}", headers={"X-Loc-Token": raw_b}).status_code
            == 404
        ), "org B is walled off with a 404"
    finally:
        settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  [B] dispatch-detail cross-tenant wall: org A 200, org B 404: OK")


def test_web_and_api_resume_refused_on_a_retry_wait_leg():
    """BLOCKER (fix round): a run parked at a RETRY_WAIT leg (a transient dispatch failure
    being retried) must REFUSE a human resume on BOTH the web and /api surfaces -- pre-fix
    vendor_pause_state matched only (DISPATCHED, RETURNED), so a RETRY_WAIT leg fell through
    to the generic 'Approve & resume' and let a human advance past the parked external step."""
    env = _seed_parked_leg()
    _to_retry_wait(env)
    # web
    c = _web_admin_client()
    r = c.post(
        f"/runs/{env['run_id']}/resume",
        data={"locale": "es-ES", "edited_text": ""},
        follow_redirects=False,
    )
    assert r.status_code == 409, (r.status_code, r.text[:200])
    # api
    api = TestClient(_app())
    admin = _admin_headers()
    r = api.post(
        f"/api/runs/{env['run_id']}/resume",
        headers=admin,
        json={"locale": "es-ES", "edited_text": ""},
    )
    assert r.status_code == 409, (r.status_code, r.text[:200])
    assert "retr" in r.text.lower(), r.text
    with Session(engine) as s:
        assert s.get(VendorDispatch, env["leg_id"]).status == VendorDispatchStatus.RETRY_WAIT
        assert s.get(WorkflowRun, env["run_id"]).status == RunStatus.PAUSED
    # the run page shows the 'retrying' banner, NOT the Approve & resume card.
    page = c.get(f"/runs/{env['run_id']}")
    assert page.status_code == 200
    assert "retrying" in page.text.lower() or "being retried" in page.text.lower(), page.text[:600]
    assert "Approve" not in page.text, "no Approve & resume card for a retry_wait leg"
    assert "dispatch to vendor.example.com returned HTTP 503 (retryable)." in page.text
    assert ">Failed</span>" not in page.text, "retrying is not a failed vendor leg"
    assert 'class="error-inline"' not in page.text, "retry notes must remain neutral"
    print(
        "  [BLOCKER] web + /api resume refused (409) on a RETRY_WAIT leg; banner shows "
        "'retrying', not Approve: OK"
    )


def test_run_view_vendor_panel_is_admin_only():
    """HIGH-C (fix round): the run-view vendor-leg panel + the Human half of the spend line
    render ONLY for an admin. A non-admin with project visibility sees the plain external
    banner (status only) -- never the vendor name, MSA-derived cost, or the band table."""
    env = _seed_parked_leg()
    with Session(engine) as s:
        vname = s.get(Vendor, env["vendor_id"]).name
    admin = _web_admin_client()
    ra = admin.get(f"/runs/{env['run_id']}")
    assert ra.status_code == 200
    assert vname in ra.text, "admin must see the vendor identity in the leg panel"
    assert "TM-leverage" in ra.text, "admin must see the band breakdown"
    # A non-admin who can still VIEW the run (MANAGER) must NOT receive vendor data.
    mgr = _web_client_for(Role.MANAGER)
    rm = mgr.get(f"/runs/{env['run_id']}")
    assert rm.status_code == 200, (rm.status_code, rm.text[:200])
    assert vname not in rm.text, "a non-admin must NOT see the vendor name"
    assert "TM-leverage" not in rm.text, "a non-admin must NOT see the band table"
    assert "Vendor dispatch legs" not in rm.text, "a non-admin must NOT see the leg panel"
    # ...but the plain external-pause banner (status only) still renders so the run isn't
    # silently 'stuck'.
    assert "external vendor" in rm.text.lower(), rm.text[:600]
    print(
        "  [HIGH-C] run-view vendor panel + Human spend are admin-only; non-admin sees "
        "only the plain banner: OK"
    )


def test_dead_letter_pause_shows_context_to_non_admin():
    """MED-2 (final round): a DEAD_LETTER external pause is human-resumable, so it falls to
    the human-review card -- but that card alone gives ZERO context. A state-only banner
    (rendered for EVERYONE) must name the state + consequence, carrying NO vendor identity /
    host / cost. Admin additionally sees the leg panel."""
    env = _seed_parked_leg()
    _to_dead_letter(env)
    with Session(engine) as s:
        vname = s.get(Vendor, env["vendor_id"]).name
    # Non-admin (MANAGER can view the run) sees the context + the human card, NOT vendor data.
    mgr = _web_client_for(Role.MANAGER)
    rm = mgr.get(f"/runs/{env['run_id']}")
    assert rm.status_code == 200, (rm.status_code, rm.text[:200])
    low = rm.text.lower()
    assert "escalated to a human" in low or "machine output only" in low, (
        "the dead-letter context line must render for a non-admin",
        rm.text[:800],
    )
    assert vname not in rm.text, "the context line must NOT carry the vendor identity"
    assert "TM-leverage" not in rm.text and "Vendor dispatch legs" not in rm.text, (
        "a non-admin must not see the admin leg panel"
    )
    assert "Approve" in rm.text, "the run is human-resumable, so the resume card must render"
    # Admin sees the same context PLUS the leg panel (vendor identity).
    admin = _web_admin_client()
    ra = admin.get(f"/runs/{env['run_id']}")
    assert "machine output only" in ra.text.lower() or "escalated to a human" in ra.text.lower()
    assert vname in ra.text, "admin still sees the vendor identity in the leg panel"
    print(
        "  [MED-2] dead-letter pause renders state-only context to a non-admin (no vendor "
        "identity), with the resume card: OK"
    )


if __name__ == "__main__":
    _install_resume_stub()  # script path: the autouse fixture only runs under pytest
    test_webhook_admits_a_correctly_signed_return()
    test_webhook_rejects_bad_signatures_uniformly()
    test_webhook_checks_the_signature_before_parsing_the_body()
    test_webhook_caps_the_body_before_hashing_it()
    test_webhook_drops_a_late_return_loudly_and_a_bad_id_cleanly()
    test_api_return_routes_are_admin_only_and_route_correctly()
    test_api_dispatch_detail_route()
    test_api_dispatch_detail_cross_tenant_404()
    test_api_manual_return_admits_through_the_same_guards()
    test_api_poll_refuses_a_vendor_without_a_poll_url()
    test_web_manual_return_is_pinned_to_the_leg_in_the_url()
    test_web_return_form_is_admin_gated_and_csrf_protected()
    test_human_resume_refused_on_a_vendor_pause()
    test_web_human_resume_refused_on_a_wedged_returned_leg()
    test_api_human_resume_refused_on_a_wedged_returned_leg()
    test_run_view_shows_awaiting_redrive_for_a_wedged_leg()
    test_web_and_api_resume_refused_on_a_retry_wait_leg()
    test_run_view_vendor_panel_is_admin_only()
    test_dead_letter_pause_shows_context_to_non_admin()
    print("verify_vendor_return_web: all OK")
