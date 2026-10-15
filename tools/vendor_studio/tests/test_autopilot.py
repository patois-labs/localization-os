"""Autopilot scenarios build the correct vendor-side payloads (Localization OS's reaction is
asserted by the roundtrip suite; here we assert what leaves the vendor)."""

from __future__ import annotations

import json

from conftest import sample_contract
from vstudio import autopilot, delivery, intake


class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True, "complete": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _recording_poster(captured, status=200, body=None):
    def poster(payload, claim, *, sign=True, secret=None, base_url=None, timeout=20.0):
        captured.append({"payload": payload, "claim": claim, "sign": sign})
        return _FakeResp(status, body)

    return poster


def _seed(session, claim="claim-ap"):
    return intake.store_dispatch(session, sample_contract(claim=claim))


def test_happy_path_fills_confirms_and_delivers_all(session):
    proj = _seed(session)
    cap = []
    out = autopilot.run_scenario(
        session, "happy_path", project_id=proj.id, poster=_recording_poster(cap)
    )
    assert out["delivered"] is True
    # every dispatched id was returned, exactly once, none fabricated.
    sent_ids = [s["id"] for s in cap[0]["payload"]["segments"]]
    assert sorted(sent_ids) == ["1", "2", "3"]
    assert cap[0]["payload"]["claim_token"] == proj.claim_token
    assert cap[0]["sign"] is True


def test_partial_2of3_sends_two_tranches(session):
    proj = _seed(session)
    cap = []
    poster = _recording_poster(cap, body={"ok": True, "complete": False})
    autopilot.run_scenario(session, "partial_2of3", project_id=proj.id, poster=poster)
    assert len(cap) == 2
    assert [s["id"] for s in cap[0]["payload"]["segments"]] == ["1", "2"]
    assert [s["id"] for s in cap[1]["payload"]["segments"]] == ["3"]


def test_bad_id_extra_and_foreign_inject_an_undispatched_id(session):
    for scenario, needle in [("bad_id_extra", "999999999"), ("bad_id_foreign", "foreign:leg:0")]:
        proj = _seed(session, claim="c-" + scenario)
        cap = []
        out = autopilot.run_scenario(
            session, scenario, project_id=proj.id, poster=_recording_poster(cap)
        )
        ids = [s["id"] for s in cap[0]["payload"]["segments"]]
        assert needle in ids and out["corrupt_id"] == needle


def test_source_echo_and_blank_targets(session):
    proj = _seed(session, claim="c-echo")
    cap = []
    autopilot.run_scenario(
        session, "source_echo", project_id=proj.id, poster=_recording_poster(cap)
    )
    segs = delivery.project_segments(session, proj)
    sent = {s["id"]: s["target"] for s in cap[0]["payload"]["segments"]}
    assert sent["2"] == segs[1].source  # echoed the source back

    proj2 = _seed(session, claim="c-blank")
    cap2 = []
    autopilot.run_scenario(
        session, "blank_targets", project_id=proj2.id, poster=_recording_poster(cap2)
    )
    assert all(s["target"] == "" for s in cap2[0]["payload"]["segments"])


def test_silence_sends_nothing(session):
    proj = _seed(session, claim="c-silence")
    cap = []
    out = autopilot.run_scenario(
        session, "silence", project_id=proj.id, poster=_recording_poster(cap)
    )
    assert out["delivered"] is False and cap == []


def test_wrong_claim_and_bad_signature(session):
    proj = _seed(session, claim="c-wrong")
    cap = []
    autopilot.run_scenario(
        session, "wrong_claim", project_id=proj.id, poster=_recording_poster(cap)
    )
    assert cap[0]["claim"] == "not-a-real-claim-token"
    assert cap[0]["payload"]["claim_token"] == "not-a-real-claim-token"

    proj2 = _seed(session, claim="c-badsig")
    cap2 = []
    autopilot.run_scenario(
        session, "bad_signature", project_id=proj2.id, poster=_recording_poster(cap2)
    )
    assert cap2[0]["sign"] is False


def test_replay_sends_the_same_payload_twice(session):
    proj = _seed(session, claim="c-replay")
    cap = []
    autopilot.run_scenario(session, "replay", project_id=proj.id, poster=_recording_poster(cap))
    assert len(cap) == 2
    assert cap[0]["payload"]["segments"] == cap[1]["payload"]["segments"]


def test_late_after_cancel_cancels_then_forces_a_post(session):
    proj = _seed(session, claim="c-late")
    cap = []
    autopilot.run_scenario(
        session, "late_after_cancel", project_id=proj.id, poster=_recording_poster(cap)
    )
    from vstudio.models import VProjectStatus

    session.refresh(proj)
    assert proj.status == VProjectStatus.CANCELLED
    assert len(cap) == 1  # it posted anyway, bypassing the UI guard
