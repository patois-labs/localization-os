"""Vendor Wave E — QUOTE GATE (R3) + REWORK (R4).

Pins the new wire behaviours:
  * a quote-gated dispatch stores quote_status "requested";
  * submitting a quote signs + POSTs the exact quote body/claim/signature and flips
    quote_status "submitted";
  * a signed quote-decision flips approved / cancels on declined; a bad signature is 401
    with NO state change;
  * a rework dispatch (fresh claim token) creates a NEW project flagged is_rework and stores
    per-segment feedback, and the inbox/editor render paths stay 200.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from conftest import sample_contract
from vstudio import contract as C
from vstudio import delivery, intake, security
from vstudio.models import VProject, VProjectStatus


# --- helpers -----------------------------------------------------------------
def _sign(raw: bytes, secret="test-signing-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def quote_contract(claim="claim-quote-1", **kw):
    """A CURRENT-shape dispatch marked quote-gated (top-level quote_requested)."""
    c = sample_contract(claim=claim, **kw)
    c["quote_requested"] = True
    return c


def rework_contract(claim="claim-rework-1", rnd=2):
    """A rework dispatch: fresh claim token + top-level rework block + per-segment feedback."""
    c = sample_contract(claim=claim)
    c["rework"] = {
        "round": rnd,
        "is_rework": True,
        "note": "Round-2 corrections needed.",
        "prior_run_id": 7,
        "prior_dispatch_id": 42,
    }
    c["segments"][0]["feedback"] = {
        "issues": ["tag_damage", "glossary_violation"],
        "note": "placeholder ⟦x1⟧ was dropped; use the approved glossary term.",
    }
    return c


class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _recording_poster(captured, status=200, body=None):
    def poster(payload, claim, **_kw):
        captured.append({"payload": payload, "claim": claim})
        return _FakeResp(status, body)

    return poster


# --- R3: quote gate ----------------------------------------------------------
def test_quote_requested_dispatch_is_stored_as_requested(env, session):
    proj = intake.store_dispatch(session, quote_contract())
    assert proj.quote_requested is True
    assert proj.quote_status == "requested"
    assert proj.quoted_cost_usd is None
    # A NON-gated dispatch defaults cleanly (backward compatible).
    plain = intake.store_dispatch(session, sample_contract(claim="plain-1"))
    assert plain.quote_requested is False
    assert plain.quote_status == ""


def test_submit_quote_posts_body_claim_and_flips_submitted(env, session):
    proj = intake.store_dispatch(session, quote_contract())
    captured = []
    out = delivery.submit_quote(
        session, proj, 250.0, note="rush job", poster=_recording_poster(captured)
    )
    assert out["ok"] is True
    assert len(captured) == 1
    body = captured[0]["payload"]
    assert body["type"] == "quote"
    assert body["message_version"] == "1.0"
    assert body["claim_token"] == "claim-quote-1"
    assert body["quoted_cost_usd"] == 250.0
    assert body["currency"] == "USD"
    assert body["note"] == "rush job"
    assert captured[0]["claim"] == "claim-quote-1"
    session.refresh(proj)
    assert proj.quote_status == "submitted"
    assert proj.quoted_cost_usd == 250.0


def test_submit_quote_refuses_when_not_gated_or_already_approved(env, session):
    plain = intake.store_dispatch(session, sample_contract(claim="plain-2"))
    try:
        delivery.submit_quote(session, plain, 10.0, poster=_recording_poster([]))
        assert False, "expected ValueError for a non-gated project"
    except ValueError:
        pass
    gated = intake.store_dispatch(session, quote_contract(claim="claim-quote-2"))
    gated.quote_status = "approved"
    session.add(gated)
    session.commit()
    try:
        delivery.submit_quote(session, gated, 10.0, poster=_recording_poster([]))
        assert False, "expected ValueError once approved"
    except ValueError:
        pass


def test_post_quote_signs_the_exact_raw_body(env, monkeypatch):
    """post_quote signs the EXACT bytes it sends and targets the quote webhook path."""
    captured = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return _FakeResp(200, {"ok": True})

    monkeypatch.setattr(delivery.httpx, "Client", _FakeClient)
    payload = delivery.build_quote_payload("claim-xyz", 99.5, note="hi")
    delivery.post_quote(payload, "claim-xyz")
    assert captured["url"].endswith("/webhooks/vendor-quotes")
    assert captured["headers"][C.CLAIM_HEADER] == "claim-xyz"
    expected_sig = security.compute_signature("test-signing-secret", captured["content"])
    assert captured["headers"][C.SIGNATURE_HEADER] == expected_sig
    assert json.loads(captured["content"].decode())["quoted_cost_usd"] == 99.5


def test_quote_route_end_to_end(env, client, session, monkeypatch):
    proj = intake.store_dispatch(session, quote_contract(claim="claim-quote-route"))
    captured = []
    monkeypatch.setattr(delivery, "post_quote", _recording_poster(captured))
    r = client.post(
        f"/projects/{proj.id}/quote",
        data={"quoted_cost_usd": "175.25", "note": "n"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert captured and captured[0]["payload"]["quoted_cost_usd"] == 175.25
    fresh = session.get(VProject, proj.id)
    session.refresh(fresh)
    assert fresh.quote_status == "submitted"
    assert fresh.quoted_cost_usd == 175.25


# --- R3: quote decision ------------------------------------------------------
def _decision_body(claim, decision, **extra):
    body = {
        "type": "quote_decision",
        "message_version": "1.0",
        "claim_token": claim,
        "decision": decision,
        "quote_currency": "USD",
    }
    body.update(extra)
    return json.dumps(body).encode()


def test_quote_decision_approved(env, client, session):
    proj = intake.store_dispatch(session, quote_contract(claim="claim-dec-approve"))
    raw = _decision_body("claim-dec-approve", "approved", quoted_cost_usd=300.0)
    r = client.post("/intake/quote-decision", content=raw, headers={C.SIGNATURE_HEADER: _sign(raw)})
    assert r.status_code == 200, r.text
    fresh = session.get(VProject, proj.id)
    session.refresh(fresh)
    assert fresh.quote_status == "approved"
    assert fresh.quoted_cost_usd == 300.0
    assert fresh.status != VProjectStatus.CANCELLED


def test_quote_decision_declined_cancels(env, client, session):
    proj = intake.store_dispatch(session, quote_contract(claim="claim-dec-decline"))
    raw = _decision_body("claim-dec-decline", "declined", reason="over budget")
    r = client.post("/intake/quote-decision", content=raw, headers={C.SIGNATURE_HEADER: _sign(raw)})
    assert r.status_code == 200, r.text
    fresh = session.get(VProject, proj.id)
    session.refresh(fresh)
    assert fresh.quote_status == "declined"
    assert fresh.status == VProjectStatus.CANCELLED
    assert fresh.cancel_reason == "over budget"


def test_quote_decision_idempotent_replayed_decline_after_approve(env, client, session):
    """Finding #14: once a TERMINAL quote decision is recorded, a REPLAYED / out-of-order
    decision is a no-op. A 'declined' arriving AFTER an 'approved' must NOT cancel the active
    project."""
    proj = intake.store_dispatch(session, quote_contract(claim="claim-dec-replay"))
    raw = _decision_body("claim-dec-replay", "approved", quoted_cost_usd=300.0)
    r = client.post("/intake/quote-decision", content=raw, headers={C.SIGNATURE_HEADER: _sign(raw)})
    assert r.status_code == 200, r.text
    # a replayed 'declined' (correctly signed) arrives late
    raw2 = _decision_body("claim-dec-replay", "declined", reason="stale")
    r2 = client.post(
        "/intake/quote-decision", content=raw2, headers={C.SIGNATURE_HEADER: _sign(raw2)}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json().get("applied") is False, r2.text
    fresh = session.get(VProject, proj.id)
    session.refresh(fresh)
    assert fresh.quote_status == "approved", "the terminal decision stands"
    assert fresh.status != VProjectStatus.CANCELLED, "a replayed decline must NOT cancel"


def test_quote_decision_bad_signature_is_401_no_change(env, client, session):
    proj = intake.store_dispatch(session, quote_contract(claim="claim-dec-badsig"))
    raw = _decision_body("claim-dec-badsig", "approved")
    r = client.post(
        "/intake/quote-decision", content=raw, headers={C.SIGNATURE_HEADER: "sha256=" + "ab" * 32}
    )
    assert r.status_code == 401, r.text
    fresh = session.get(VProject, proj.id)
    session.refresh(fresh)
    assert fresh.quote_status == "requested"  # unchanged
    assert fresh.status != VProjectStatus.CANCELLED


def test_quote_decision_unknown_claim_is_404(env, client):
    raw = _decision_body("no-such-claim", "approved")
    r = client.post("/intake/quote-decision", content=raw, headers={C.SIGNATURE_HEADER: _sign(raw)})
    assert r.status_code == 404, r.text


# --- R4: rework --------------------------------------------------------------
def test_rework_dispatch_creates_new_project_with_feedback(env, session):
    # An original job first, then a rework with a DISTINCT claim token.
    original = intake.store_dispatch(session, sample_contract(claim="orig-1"))
    rw = intake.store_dispatch(session, rework_contract(claim="rework-1"))
    assert rw.id != original.id
    assert rw.is_rework is True
    assert rw.rework["round"] == 2
    assert rw.rework["note"] == "Round-2 corrections needed."
    segs = delivery.project_segments(session, rw)
    assert segs[0].feedback == {
        "issues": ["tag_damage", "glossary_violation"],
        "note": "placeholder ⟦x1⟧ was dropped; use the approved glossary term.",
    }
    # Segments without feedback stay None (backward compatible).
    assert segs[1].feedback is None


def test_rework_dispatch_over_the_wire_and_renders(env, client, session):
    raw = json.dumps(rework_contract(claim="rework-wire")).encode()
    r = client.post(
        "/intake/dispatch",
        content=raw,
        headers={C.SIGNATURE_HEADER: _sign(raw), C.CLAIM_HEADER: "rework-wire"},
    )
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    # Inbox + project + editor render paths all stay 200 with the rework/feedback present.
    assert client.get("/").status_code == 200
    assert client.get(f"/projects/{pid}").status_code == 200
    assert client.get(f"/projects/{pid}/editor").status_code == 200


def test_quote_gated_project_renders(env, client, session):
    proj = intake.store_dispatch(session, quote_contract(claim="claim-render"))
    assert client.get("/").status_code == 200
    assert client.get(f"/projects/{proj.id}").status_code == 200


def test_boot_against_pre_wave_e_schema(monkeypatch, tmp_path):
    """Finding #1: a VS DB built BEFORE the Wave-E columns existed must boot. init_db's
    additive-column shim adds vproject.{quote_requested,quoted_cost_usd,quote_status,is_rework,
    rework} + vsegment.feedback on an EXISTING table, so a request reading the model does not
    500 (create_all alone never ALTERs an existing table)."""
    import sqlite3

    db = str(tmp_path / "old.db")
    monkeypatch.setenv("VSTUDIO_DB_PATH", db)
    monkeypatch.setenv("VSTUDIO_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("VSTUDIO_SSO_SECRET", "test-sso-secret")
    monkeypatch.setenv("VSTUDIO_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("VSTUDIO_LOCSTUDIO_URL", "https://loc.example.test")
    monkeypatch.setenv("VSTUDIO_ADVISOR_MODE", "mock")

    from vstudio import db as vdb
    from vstudio.config import settings

    settings.reload()
    vdb.reset_engine()

    wave_e = {
        "vproject": ["quote_requested", "quoted_cost_usd", "quote_status", "is_rework", "rework"],
        "vsegment": ["feedback"],
    }

    # 1. Build the full schema, then STRIP the Wave-E columns to simulate a pre-Wave-E DB.
    vdb.init_db()
    con = sqlite3.connect(db)
    try:
        for tbl, cols in wave_e.items():
            for c in cols:
                con.execute(f"ALTER TABLE {tbl} DROP COLUMN {c}")
        con.commit()
        before = {c[1] for c in con.execute("PRAGMA table_info(vproject)").fetchall()}
        assert "quote_requested" not in before, "precondition: the old schema lacks the column"
    finally:
        con.close()
    vdb.reset_engine()

    # 2. Boot again -> the additive-column shim must re-add every Wave-E column.
    vdb.init_db()
    con = sqlite3.connect(db)
    try:
        after_p = {c[1] for c in con.execute("PRAGMA table_info(vproject)").fetchall()}
        after_s = {c[1] for c in con.execute("PRAGMA table_info(vsegment)").fetchall()}
    finally:
        con.close()
    for c in wave_e["vproject"]:
        assert c in after_p, f"init_db must add vproject.{c} on a pre-Wave-E DB"
    assert "feedback" in after_s, "init_db must add vsegment.feedback on a pre-Wave-E DB"

    # 3. And a real request path works against the healed schema.
    from vstudio import intake
    from vstudio.db import get_session

    with get_session() as s:
        proj = intake.store_dispatch(s, quote_contract(claim="healed-schema"))
        assert proj.quote_requested is True and proj.quote_status == "requested"
    vdb.reset_engine()
