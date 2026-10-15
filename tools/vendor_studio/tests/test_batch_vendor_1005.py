"""Issues #516 (key-bound roles), #522 (quote gate, delivered-segment lock, duplicate ids)
and #521 (confirm survives the blur save)."""

from __future__ import annotations

import pytest
from conftest import sample_contract
from fastapi.testclient import TestClient
from vstudio import delivery, editor, intake, security
from vstudio.models import VSegStatus


# --- helpers -----------------------------------------------------------------
def _cookie_client(role: str, **extra) -> TestClient:
    from vstudio.app import app

    c = TestClient(app)
    c.cookies.set(
        security.COOKIE_NAME,
        security.encode_session({"authed": True, "user": "u", "name": "u", "role": role, **extra}),
    )
    return c


def _stub_account(monkeypatch, status=200, body=None):
    """Stub the wire call to Localization OS. Records the URL + headers."""
    import vstudio.app as vapp

    seen = {}

    class _Resp:
        status_code = status

        def json(self):
            if body is None:
                raise ValueError("no json")
            return body

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            return _Resp()

    monkeypatch.setattr(vapp.httpx, "Client", _Client)
    vapp._APIKEY_BUCKET.clear()
    return seen


def _login(monkeypatch, body, status=200):
    from vstudio.app import app

    _stub_account(monkeypatch, status=status, body=body)
    c = TestClient(app)
    r = c.post("/login/apikey", data={"api_key": "lsu_k"}, follow_redirects=False)
    return c, r


def _session_role(c):
    vals = [ck.value for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]
    sess = security.decode_session(vals[-1]) if vals else None
    return (sess or {}).get("role")


# --- #516 -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("main_role", "studio_role"),
    [
        ("admin", "PM"),
        ("manager", "PM"),
        ("reviewer", "Reviewer"),
        ("linguist", "Reviewer"),
        ("translator", "Translator"),
    ],
)
def test_apikey_login_binds_role_to_the_keys_real_role(env, monkeypatch, main_role, studio_role):
    c, r = _login(monkeypatch, {"ok": True, "role": main_role})
    assert r.status_code == 303
    assert _session_role(c) == studio_role


@pytest.mark.parametrize(
    "body",
    [
        {"ok": True, "role": "viewer"},
        {"ok": True, "role": "vendor"},
        {"ok": True},
        {"ok": True, "role": ""},
        {"ok": True, "role": "ADMINISTRATOR"},
        ["x"],
        None,
    ],
)
def test_apikey_login_fails_closed_for_unknown_or_unmapped_role(env, monkeypatch, body):
    c, r = _login(monkeypatch, body)
    assert r.status_code == 403
    assert not [ck for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_apikey_login_fails_closed_on_non_2xx_account_answer(env, monkeypatch, status):
    c, r = _login(monkeypatch, {"ok": True, "role": "admin"}, status=status)
    assert r.status_code == 403
    assert not [ck for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]


def test_apikey_login_fails_closed_on_ok_false_body(env, monkeypatch):
    c, r = _login(monkeypatch, {"ok": False, "role": "admin"})
    assert r.status_code == 403
    assert not [ck for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]


def test_apikey_login_fails_closed_on_timeout(env, monkeypatch):
    import httpx
    import vstudio.app as vapp
    from vstudio.app import app

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(vapp.httpx, "Client", _Client)
    vapp._APIKEY_BUCKET.clear()
    c = TestClient(app)
    r = c.post("/login/apikey", data={"api_key": "lsu_k"}, follow_redirects=False)
    assert r.status_code == 403
    assert not [ck for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]


def test_apikey_login_asks_the_account_endpoint_not_languages(env, monkeypatch):
    seen = _stub_account(monkeypatch, body={"ok": True, "role": "admin"})
    from vstudio.app import app

    TestClient(app).post("/login/apikey", data={"api_key": "lsu_k"}, follow_redirects=False)
    assert seen["url"].endswith("/api/account"), seen
    assert seen["headers"].get("X-Loc-Token") == "lsu_k"


@pytest.mark.parametrize("role", ["Translator", "Reviewer"])
def test_vendor_scoped_session_cannot_reach_pm_routes(env, session, role):
    proj = intake.store_dispatch(session, sample_contract(claim="c-pm"))
    c = _cookie_client(role)
    for method, path, kw in [
        ("post", f"/projects/{proj.id}/assign", {"data": {"assignee": "x"}}),
        ("post", f"/projects/{proj.id}/cost", {"data": {"actual_cost_usd": "1"}}),
        ("post", f"/projects/{proj.id}/quote", {"data": {"quoted_cost_usd": "1"}}),
        ("post", f"/projects/{proj.id}/cancel", {"data": {"reason": "x"}}),
        ("post", f"/projects/{proj.id}/deliver", {"json": {}}),
        ("post", f"/projects/{proj.id}/deliver-partial", {"json": {}}),
        ("post", f"/projects/{proj.id}/complete", {"json": {}}),
        ("post", "/settings/qa", {"data": {}}),
    ]:
        r = getattr(c, method)(path, follow_redirects=False, **kw)
        assert r.status_code == 403, (path, r.status_code)
    from vstudio.models import VProject

    session.expire_all()
    fresh = session.get(VProject, proj.id)
    assert fresh.status != "CANCELLED" and not fresh.assigned_to


def test_translator_session_cannot_approve_or_reject(env, session):
    proj = intake.store_dispatch(session, sample_contract(claim="c-ar"))
    seg = delivery.project_segments(session, proj)[1]
    c = _cookie_client("Translator")
    assert c.post(f"/projects/{proj.id}/segments/{seg.id}/approve").status_code == 403
    assert (
        c.post(f"/projects/{proj.id}/segments/{seg.id}/reject", json={"issue_tag": "x"}).status_code
        == 403
    )


def test_reviewer_session_can_approve(env, session):
    proj = intake.store_dispatch(session, sample_contract(claim="c-ar2"))
    seg = delivery.project_segments(session, proj)[1]
    c = _cookie_client("Reviewer")
    assert c.post(f"/projects/{proj.id}/segments/{seg.id}/approve").status_code == 200


@pytest.mark.parametrize("granted", ["Translator", "Reviewer"])
def test_self_assigned_role_in_persona_request_is_ignored(env, granted):
    c = _cookie_client(granted)
    for want in ("PM", "Reviewer", "Translator"):
        c.post("/persona", data={"role": want, "name": "evil"}, follow_redirects=False)
        assert _session_role(c) == granted, (granted, want, _session_role(c))


def test_apikey_translator_cannot_persona_up_to_pm(env, monkeypatch):
    c, r = _login(monkeypatch, {"ok": True, "role": "translator"})
    assert r.status_code == 303
    c.post("/persona", data={"role": "PM"}, follow_redirects=False)
    assert _session_role(c) == "Translator"
    # ...and the PM route stays shut.
    assert c.post("/settings/qa", data={}).status_code == 403


def test_pm_session_may_still_switch_persona_down_and_back(env):
    c = _cookie_client("PM")
    c.post("/persona", data={"role": "Reviewer"}, follow_redirects=False)
    assert _session_role(c) == "Reviewer"
    c.post("/persona", data={"role": "PM"}, follow_redirects=False)
    assert _session_role(c) == "PM"


def test_persona_redirect_ignores_offsite_referer(env):
    c = _cookie_client("PM")
    r = c.post(
        "/persona",
        data={"role": "PM"},
        headers={"referer": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


# --- #522 -------------------------------------------------------------------
def _quote_project(session, claim="c-q"):
    c = sample_contract(claim=claim)
    c["quote_requested"] = True
    return intake.store_dispatch(session, c)


@pytest.mark.parametrize("status", ["requested", "submitted", "declined"])
def test_quote_gate_blocks_claim_deliver_complete_until_approved(env, session, client, status):
    proj = _quote_project(session, claim=f"c-q-{status}")
    proj.quote_status = status
    session.add(proj)
    session.commit()
    assert client.post(f"/projects/{proj.id}/claim", follow_redirects=False).status_code == 409
    assert client.post(f"/projects/{proj.id}/deliver", json={}).status_code == 409
    assert client.post(f"/projects/{proj.id}/deliver-partial", json={}).status_code == 409
    assert client.post(f"/projects/{proj.id}/complete", json={}).status_code == 409
    session.expire_all()
    session.refresh(proj)
    assert proj.status == "NEW" and not proj.claimed_by


def test_quote_gate_service_level_deliver_refuses(env, session):
    proj = _quote_project(session, claim="c-q-svc")
    with pytest.raises(ValueError, match="quote"):
        delivery.deliver(session, proj, mode="full", poster=lambda *a, **k: None)


def test_quote_gate_open_once_approved_and_when_not_gated(env, session, client):
    proj = _quote_project(session, claim="c-q-ok")
    proj.quote_status = "approved"
    session.add(proj)
    session.commit()
    assert client.post(f"/projects/{proj.id}/claim", follow_redirects=False).status_code == 303
    plain = intake.store_dispatch(session, sample_contract(claim="c-plain"))
    assert client.post(f"/projects/{plain.id}/claim", follow_redirects=False).status_code == 303


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


def test_delivered_segment_is_locked_after_partial_delivery(env, session):
    proj = intake.store_dispatch(session, sample_contract(claim="c-lock"))
    segs = delivery.project_segments(session, proj)
    editor.patch_target(session, proj, segs[1], "Hola", actor="t")
    editor.confirm_segment(session, proj, segs[1], actor="t")
    out = delivery.deliver(session, proj, mode="partial", poster=lambda *a, **k: _Resp())
    assert out["ok"] and segs[1].contract_id in out["delivered"]
    with pytest.raises(editor.EditError):
        editor.patch_target(session, proj, segs[1], "otro", actor="t")
    with pytest.raises(editor.EditError):
        editor.reviewer_reject(session, proj, segs[1], "x", actor="r")
    with pytest.raises(editor.EditError):
        editor.confirm_segment(session, proj, segs[1], actor="t")
    # an undelivered row stays editable
    editor.patch_target(session, proj, segs[2], "Tres", actor="t")


def test_duplicate_segment_ids_refused_at_intake(env, session):
    for ids in ([5, 5], ["5", "5"], [5, "5"], [5, " 5 "]):
        c = sample_contract(claim=f"c-dup-{ids}")
        c["segments"] = [
            {"id": i, "source": f"s{n}", "target": "", "state": "needs-translation"}
            for n, i in enumerate(ids)
        ]
        with pytest.raises(intake.IntakeError, match="duplicate"):
            intake.store_dispatch(session, c)


# --- #521 -------------------------------------------------------------------
def test_blur_save_with_unchanged_text_keeps_confirmation(env, session, client):
    proj = intake.store_dispatch(session, sample_contract(claim="c-blur"))
    seg = delivery.project_segments(session, proj)[1]
    base = f"/projects/{proj.id}/segments/{seg.id}"
    assert client.patch(base, json={"target": "Hola"}).status_code == 200
    assert client.post(base + "/confirm").json()["status"] == VSegStatus.CONFIRMED
    # the blur PATCH that follows focus moving on, same text
    r = client.patch(base, json={"target": "Hola"})
    assert r.status_code == 200
    assert r.json()["status"] == VSegStatus.CONFIRMED
    assert r.json()["confirmed_by"]
    session.expire_all()
    assert editor.progress(session, proj)["done"] == 1


def test_unchanged_patch_keeps_approval_and_reject_tag(env, session):
    proj = intake.store_dispatch(session, sample_contract(claim="c-blur2"))
    seg = delivery.project_segments(session, proj)[1]
    editor.patch_target(session, proj, seg, "Hola", actor="t")
    editor.reviewer_approve(session, proj, seg, actor="r")
    editor.patch_target(session, proj, seg, "Hola", actor="r")
    assert seg.status == VSegStatus.APPROVED
    editor.reviewer_reject(session, proj, seg, "bad", actor="r")
    editor.patch_target(session, proj, seg, "Hola", actor="t")
    assert seg.status == VSegStatus.EDITED and seg.issue_tag == "bad"


def test_changed_text_still_resets_confirmation(env, session):
    proj = intake.store_dispatch(session, sample_contract(claim="c-blur3"))
    seg = delivery.project_segments(session, proj)[1]
    editor.patch_target(session, proj, seg, "Hola", actor="t")
    editor.confirm_segment(session, proj, seg, actor="t")
    editor.patch_target(session, proj, seg, "Hola!", actor="t")
    assert seg.status == VSegStatus.EDITED and seg.confirmed_by == ""


def test_editor_js_saves_on_blur_only_when_changed_and_keeps_shortcut():
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "static", "editor.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert "data-saved" in js or "savedValue" in js
    assert 'e.ctrlKey && e.key === "Enter"' in js


def test_editor_template_preserves_leading_newline(env, session, client):
    c = sample_contract(claim="c-nl")
    c["segments"][1]["target"] = "\nlinea"
    proj = intake.store_dispatch(session, c)
    html = client.get(f"/projects/{proj.id}/editor").text
    assert '<textarea data-target rows="2">\n\nlinea</textarea>' in html


# --- Review additions --------------------------------------------------------
def test_pre_fix_session_cookie_no_longer_verifies(env):
    """A cookie minted before #516 (old salt, self-assigned PM) must not survive the upgrade."""
    from itsdangerous import URLSafeTimedSerializer
    from vstudio.app import app
    from vstudio.config import settings

    old = URLSafeTimedSerializer(settings.session_secret, salt="vstudio-session").dumps(
        {"authed": True, "user": "u", "name": "u", "role": "PM"}
    )
    assert security.decode_session(old) is None
    c = TestClient(app)
    c.cookies.set(security.COOKIE_NAME, old)
    assert c.get("/api/wirelog").status_code == 404
    assert c.post("/settings/qa", data={"check_key": "x"}).status_code == 404


def test_non_string_target_is_refused_not_500(env, session, client):
    proj = intake.store_dispatch(session, sample_contract(claim="c-nonstr"))
    seg = delivery.project_segments(session, proj)[1]
    for bad in (5, ["x"], {"a": 1}):
        r = client.patch(f"/projects/{proj.id}/segments/{seg.id}", json={"target": bad})
        assert r.status_code == 409


def test_poll_does_not_serve_a_completed_project_with_unapproved_quote(env, session):
    from vstudio import contract as C
    from vstudio.app import app
    from vstudio.models import VProjectStatus

    proj = _quote_project(session, claim="c-q-poll")
    proj.status = VProjectStatus.COMPLETED
    proj.quote_status = "submitted"
    session.add(proj)
    session.commit()
    c = TestClient(app)
    assert c.get("/intake/poll", headers={C.CLAIM_HEADER: "c-q-poll"}).status_code == 204
    session.refresh(proj)
    assert proj.status == VProjectStatus.COMPLETED
    proj.quote_status = "approved"
    session.add(proj)
    session.commit()
    assert c.get("/intake/poll", headers={C.CLAIM_HEADER: "c-q-poll"}).status_code == 200


def test_editor_js_confirm_waits_for_an_in_flight_blur_save():
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "static", "editor.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert "ta._pendingSave = p" in js
    assert "isDirty(ta) ? saveTarget(ta) : (ta._pendingSave ||" in js
