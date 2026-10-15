"""Vendor Wave E: the QUOTE-APPROVAL GATE (R3) + REJECT/REWORK (R4).

Hermetic + offline: every dispatch rides an injected httpx.MockTransport
(vendor_dispatch.set_test_transport), so no byte reaches a real network, and the whole
suite runs on a throwaway data dir. This suite attacks the two lifecycle completions:

  R3 quote gate
    [R3-1] flag OFF is BYTE-IDENTICAL: a non-quote-gated dispatch parks DISPATCHED, the
           contract carries no `quote_requested`, and every quote/rework field is 0/NULL.
    [R3-2] flag ON parks QUOTED, the contract carries `quote_requested`, the pause BLOCKS a
           plain human resume, and a RETURN is REFUSED before approval.
    [R3-3] record a quote (manual) -> approve -> the leg flips DISPATCHED, the estimate is
           PRESERVED alongside the quote, and a return then admits + reconciles the actual.
    [R3-4] the SIGNED quote wire message: authenticate_callback verifies the HMAC + claim
           header; a WRONG signing key is refused; a body whose embedded claim binds to a
           DIFFERENT leg is refused (intra-vendor cross-leg replay).
    [R3-5] decline escalates the parked run to a human_review pause (reuses decision 3).
    [R3-6] a QUOTED leg past its wait EXPIRES -> the sweep escalates it to a human.

  R4 reject/rework
    [R4-1] the FULL NATIVE ROUNDTRIP: dispatch -> return -> reject -> rework re-dispatch
           (NEW claim token, same rendition, `rework` marker + per-segment feedback) ->
           rework return admitted through the SINGLE guarded seam -> run COMPLETED.
    [R4-2] rework-cap exhaustion: rejecting a leg already at the cap escalates to a human,
           with NO further vendor round.
    [R4-3] id accounting on the rework return: an unknown id fails the WHOLE rework return.
    [R4-4] run-cancel composes: cancelling a run closes a QUOTED leg and an open rework leg.

Every test covers NEW behaviour (Wave E did not exist before this branch), so none is a
fail-before proof of a pre-existing defect.
"""

import hashlib
import hmac
import json
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_vendore_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
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
    orchestrator,
    vendor_dispatch,
    vendor_quote,
    vendor_return,
    vendor_rework,
    vendors,
)
from app.services import workflows as wf

init_db()

_SEQ = [0]
SIGNING = "topsecret-signing-e"
SOURCES = ["Hello ⟦x1⟧ world.", "The cat sat on the mat.", "Please sign in now."]
GOOD = ["Hola ⟦x1⟧ mundo.", "El gato se sento en la alfombra.", "Por favor inicie sesion ahora."]
BETTER = [
    "Hola ⟦x1⟧ mundo mejorado.",
    "El gato descansa en la alfombra.",
    "Por favor, inicie sesion ahora mismo.",
]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _mock_transport(status=200, capture=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append({"url": str(req.url), "headers": dict(req.headers), "body": req.content})
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler)


def _seed(*, quote_gated=False, signing=SIGNING):
    """A customer + project + doc + vendor (optionally quote-gated) linked to the customer
    + a Translate -> External(vendor) workflow. Returns the id bundle."""
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
        cfg = {"requires_quote_approval": True} if quote_gated else None
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url="https://vendor.example.com/intake",
            config=cfg,
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": signing})
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
        wf.add_step(
            s,
            w.id,
            name="Vendor review",
            step_type=StepType.EXTERNAL,
            vendor_id=v.id,
            external_wait_hours=72,
        )
        return {
            "customer_id": cust.id,
            "project_id": p.id,
            "doc_id": d.id,
            "vendor_id": v.id,
            "wf_id": w.id,
        }


def _dispatch(env, locale="es-ES"):
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], locale))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == env["wf_id"])
            .order_by(WorkflowRun.id.desc())
        ).first()
        leg = s.exec(
            select(VendorDispatch)
            .where(VendorDispatch.workflow_run_id == run.id)
            .order_by(VendorDispatch.id.desc())
        ).first()
        return run.id, leg.id, leg.claim_token


def _seg_ids(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, locale)]


def _targets(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [sg.target_text or "" for sg in orchestrator._load_segments(s, doc_id, locale)]


def _payload(ids, targets, *, claim=None, locale="es-ES", cost=None):
    body = {
        "contract_version": "1.0",
        "target_locale": locale,
        "segments": [{"id": i, "target": t} for i, t in zip(ids, targets, strict=False)],
    }
    if claim is not None:
        body["claim_token"] = claim
    if cost is not None:
        body["actual_cost_usd"] = cost
    return body


def _sign(raw: bytes, secret=SIGNING) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _admit(claim, ids, targets, cost=None):
    with Session(engine) as s:
        return vendor_return.admit_manual(
            s, _payload(ids, targets, claim=claim, cost=cost), resume="sync", actor="op"
        )


# ============================================================================
# R3 -- the quote-approval gate
# ============================================================================
def test_flag_off_is_byte_identical():
    env = _seed(quote_gated=False)
    run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED
        assert leg.requires_quote is False
        assert leg.quoted_cost_usd is None and leg.quote_approved_at is None
        assert leg.rework_round == 0 and leg.rework_of is None
    # The dispatched contract carries NO quote_requested key when the flag is off.
    with Session(engine) as s:
        v = s.get(Vendor, env["vendor_id"])
        run = s.get(WorkflowRun, run_id)
        step = s.exec(
            select(orchestrator.WorkflowStep).where(
                orchestrator.WorkflowStep.workflow_id == env["wf_id"],
                orchestrator.WorkflowStep.step_type == StepType.EXTERNAL,
            )
        ).first()
        contract = vendor_dispatch.build_contract(
            run=run,
            step=step,
            segments=[{"id": 1, "source": "x"}],
            source_locale="en-US",
            target_locale="es-ES",
            claim_token="t",
            vendor=v,
        )
        assert "quote_requested" not in contract and "rework" not in contract
    print("  [R3-1] flag OFF: DISPATCHED, no quote_requested, no quote/rework fields: OK")


def test_quote_gated_parks_quoted_and_blocks():
    env = _seed(quote_gated=True)
    run_id, leg_id, claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.QUOTED, leg.status
        assert leg.requires_quote is True
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED
        _lg, state = vendor_dispatch.vendor_pause_state(s, run_id)
        assert state == "quoted" and vendor_dispatch.blocks_human_resume(state)
        # the contract carried quote_requested
        v = s.get(Vendor, env["vendor_id"])
        step = s.exec(
            select(orchestrator.WorkflowStep).where(
                orchestrator.WorkflowStep.workflow_id == env["wf_id"],
                orchestrator.WorkflowStep.step_type == StepType.EXTERNAL,
            )
        ).first()
        contract = vendor_dispatch.build_contract(
            run=run,
            step=step,
            segments=[{"id": 1, "source": "x"}],
            source_locale="en-US",
            target_locale="es-ES",
            claim_token="t",
            vendor=v,
        )
        assert contract.get("quote_requested") is True
    # a RETURN before approval is REFUSED (the leg is not DISPATCHED)
    ids = _seg_ids(env["doc_id"])
    try:
        _admit(claim, ids, GOOD)
        assert False, "a return before approval must be refused"
    except vendor_return.ReturnStateError:
        pass
    print(
        "  [R3-2] flag ON: QUOTED, quote_requested on the wire, blocks resume, "
        "return refused pre-approval: OK"
    )


def test_record_approve_then_return_reconciles():
    env = _seed(quote_gated=True)
    run_id, leg_id, claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        est = leg.estimate_cost_usd
        vendor_quote.record_quote(s, leg, quoted_cost_usd=250.0, currency="USD", source="manual")
        s.refresh(leg)
        assert leg.quoted_cost_usd == 250.0 and leg.status == VendorDispatchStatus.QUOTED
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_quote.approve_quote(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.DISPATCHED
        assert leg.quote_approved_at is not None
        assert leg.quoted_cost_usd == 250.0
        assert leg.estimate_cost_usd == est, "the scoping estimate is PRESERVED, not overwritten"
    ids = _seg_ids(env["doc_id"])
    out = _admit(claim, ids, GOOD, cost=260.0)
    assert out["complete"] and out["resumed"]
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
        assert leg.actual_cost_usd == 260.0, "estimate -> quote -> actual chain"
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.COMPLETED
    for txt, want in zip(_targets(env["doc_id"]), GOOD, strict=False):
        assert txt == want
    print(
        "  [R3-3] record -> approve (estimate preserved) -> return admits + reconciles "
        "actual, run COMPLETED: OK"
    )


def test_signed_quote_wire_message_and_negatives():
    env = _seed(quote_gated=True)
    _run_id, leg_id, claim = _dispatch(env)
    # a correctly-signed quote message authenticates + records
    body = json.dumps(
        {
            "type": "quote",
            "message_version": "1.0",
            "claim_token": claim,
            "quoted_cost_usd": 88.0,
            "currency": "USD",
        }
    ).encode()
    headers = {"x-locstudio-claim": claim, "x-locstudio-signature": _sign(body)}
    with Session(engine) as s:
        leg = vendor_return.authenticate_callback(s, headers, body)
        assert leg.id == leg_id
        out = vendor_quote.apply_quote_message(s, leg, body)
        assert out["quoted_cost_usd"] == 88.0
    # WRONG signing key -> ReturnAuthError (uniform)
    bad_headers = {"x-locstudio-claim": claim, "x-locstudio-signature": _sign(body, "nope")}
    with Session(engine) as s:
        try:
            vendor_return.authenticate_callback(s, bad_headers, body)
            assert False, "a wrong signature must be refused"
        except vendor_return.ReturnAuthError:
            pass
    # REPLAY into a DIFFERENT leg of the SAME vendor: a body carrying leg-A's claim, applied
    # against leg-B (same vendor secret), is refused by the claim binding.
    env2 = _seed(quote_gated=True, signing=SIGNING)
    _r2, leg2_id, _claim2 = _dispatch(env2)
    with Session(engine) as s:
        # authenticate as if it arrived for leg A is fine; applying A's body to leg B is not.
        legB = s.get(VendorDispatch, leg2_id)
        try:
            vendor_quote.apply_quote_message(s, legB, body)  # body.claim_token == claim (A)
            assert False, "a quote body bound to another leg must be refused"
        except vendor_quote.QuoteContractError:
            pass
    print(
        "  [R3-4] signed quote wire: good verifies + records; wrong key refused; "
        "cross-leg replay refused: OK"
    )


def test_decline_escalates_to_human():
    env = _seed(quote_gated=True)
    run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_quote.decline_quote(s, leg, reason="too expensive", actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        assert out["declined"] and out["escalated"]
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.ESCALATED
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED
    print("  [R3-5] decline -> ESCALATED, run parked for a human: OK")


def test_quoted_leg_expiry_escalates():
    env = _seed(quote_gated=True)
    _run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        leg.expires_at = datetime.utcnow() - timedelta(hours=1)  # force expiry
        s.add(leg)
        s.commit()
        n = vendor_dispatch.sweep_expired(s)
        assert n == 1, n
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.ESCALATED
    print("  [R3-6] a QUOTED leg past its wait is escalated by the expiry sweep: OK")


# ============================================================================
# R4 -- reject / rework
# ============================================================================
def _deliver(env, quote_gated=False):
    """Dispatch, (approve if gated,) admit a return -> a RETURNED leg + completed run."""
    run_id, leg_id, claim = _dispatch(env)
    if quote_gated:
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            vendor_dispatch.set_test_transport(_mock_transport())
            try:
                vendor_quote.approve_quote(s, leg, quoted_cost_usd=100.0, actor="op")
            finally:
                vendor_dispatch.set_test_transport(None)
    _admit(claim, _seg_ids(env["doc_id"]), GOOD, cost=120.0)
    return run_id, leg_id


def test_full_native_roundtrip_with_rework():
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    with Session(engine) as s:
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    # reject -> rework re-dispatch (real dispatch, new claim token, feedback + marker)
    cap_body = []
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport(capture=cap_body))
        try:
            out = vendor_rework.reject_and_rework(
                s,
                leg,
                feedback=[
                    {
                        "id": _seg_ids(env["doc_id"])[0],
                        "issues": ["tag_damage"],
                        "note": "fix the tag",
                    }
                ],
                note="quality too low",
                actor="op",
            )
        finally:
            vendor_dispatch.set_test_transport(None)
        assert out["reworked"] and out["rework_round"] == 1
        new_leg_id = out["new_dispatch_id"]
    # the wire carried the rework marker + per-segment feedback
    sent = json.loads(cap_body[-1]["body"])
    assert sent.get("rework", {}).get("is_rework") is True and sent["rework"]["round"] == 1
    assert any(seg.get("feedback", {}).get("issues") for seg in sent["segments"])
    with Session(engine) as s:
        old = s.get(VendorDispatch, leg_id)
        new = s.get(VendorDispatch, new_leg_id)
        assert old.status == VendorDispatchStatus.REJECTED
        assert new.status == VendorDispatchStatus.DISPATCHED
        assert new.claim_token != old.claim_token and new.rework_of == old.id
        assert new.rework_round == 1 and new.vendor_id == old.vendor_id
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED, "the run is re-parked for the rework return"
        new_claim = new.claim_token
    # the rework return funnels through the SAME single admission seam
    out = _admit(new_claim, _seg_ids(env["doc_id"]), BETTER)
    assert out["complete"] and out["resumed"]
    with Session(engine) as s:
        assert s.get(VendorDispatch, new_leg_id).status == VendorDispatchStatus.RETURNED
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    for txt, want in zip(_targets(env["doc_id"]), BETTER, strict=False):
        assert txt == want, (txt, want)
    print(
        "  [R4-1] full roundtrip: dispatch->return->reject->rework->return->admit, "
        "run COMPLETED, feedback on the wire: OK"
    )


def test_rework_cap_escalates():
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    # round 1 (under the default cap of 1)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        new_leg_id = out["new_dispatch_id"]
    # deliver the rework, then reject AGAIN -> round 2 > cap -> escalate, no new leg
    with Session(engine) as s:
        new = s.get(VendorDispatch, new_leg_id)
        _admit(new.claim_token, _seg_ids(env["doc_id"]), GOOD)
    with Session(engine) as s:
        new = s.get(VendorDispatch, new_leg_id)
        out = vendor_rework.reject_and_rework(s, new, actor="op")
        assert out["reworked"] is False and out["escalated"] is True, out
        s.refresh(new)
        assert new.status == VendorDispatchStatus.REJECTED
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)).all()
        assert len(legs) == 2, "no THIRD vendor leg is created past the cap"
    print("  [R4-2] rework cap reached -> escalate to human, no further vendor round: OK")


def _run_cli(argv):
    """Drive the real CLI in-process; returns (rc, combined output)."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:  # argparse / CliError exits
            rc = exc.code
    return rc, buf.getvalue()


def _reject(leg_id):
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            return vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)


def test_rework_max_rounds_setting_honored():
    """#632: vendor_rework_max_rounds is a REGISTERED setting. `config set` accepts it (it used
    to refuse it as an unknown key, so the loop always ran on the hard-coded 1) and the rework
    loop honours the stored value: with 2, a leg takes TWO rework rounds and only the third
    rejection escalates."""
    from app.services import settings

    assert settings.DEFAULTS.get("vendor_rework_max_rounds") == "1", "registered, default 1"
    rc, out = _run_cli(["config", "set", "vendor_rework_max_rounds", "2"])
    assert rc == 0, (rc, out)
    settings.invalidate_cache()
    rc, out = _run_cli(["--json", "config", "get", "vendor_rework_max_rounds"])
    assert rc == 0 and json.loads(out)["value"] == "2", (rc, out)
    try:
        assert vendor_rework.rework_cap() == 2
        env = _seed(quote_gated=False)
        run_id, leg_id = _deliver(env)
        out1 = _reject(leg_id)
        assert out1["reworked"] is True, out1
        with Session(engine) as s:
            _admit(
                s.get(VendorDispatch, out1["new_dispatch_id"]).claim_token,
                _seg_ids(env["doc_id"]),
                GOOD,
            )
        out2 = _reject(out1["new_dispatch_id"])
        assert out2["reworked"] is True and out2.get("escalated") is not True, (
            "round 2 must re-dispatch under a cap of 2 (the hard-coded 1 escalated here)",
            out2,
        )
        with Session(engine) as s:
            assert s.get(VendorDispatch, out2["new_dispatch_id"]).rework_round == 2
            _admit(
                s.get(VendorDispatch, out2["new_dispatch_id"]).claim_token,
                _seg_ids(env["doc_id"]),
                GOOD,
            )
        out3 = _reject(out2["new_dispatch_id"])
        assert out3["reworked"] is False and out3["escalated"] is True, out3
        with Session(engine) as s:
            legs = s.exec(
                select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)
            ).all()
            assert len(legs) == 3, "original + two rework legs, no fourth past the cap"
    finally:
        settings.set_many({"vendor_rework_max_rounds": "1"})
        settings.invalidate_cache()
    print(
        "  [#632] config set vendor_rework_max_rounds 2 -> two rework rounds, third escalates: OK"
    )


def test_rework_max_rounds_invalid_refused_every_door():
    """#632: a value outside 1..10 (or not a whole number) is refused on every write door --
    the shared set_many seam (what the web forms write through), `config set`, POST
    /api/admin/config and MCP manage_install(set_config) -- and nothing is stored. A raw row
    written around the doors still reads as the default."""
    import asyncio
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import apikeys, security, settings

    key = "vendor_rework_max_rounds"
    bad_values = ["0", "11", "-1", "abc", "1.5", "", " ", "²"]
    settings.set_many({key: "1"})
    settings.invalidate_cache()
    # 1. the shared write seam (web door)
    for bad in bad_values:
        try:
            settings.set_many({key: bad})
        except settings.InvalidSettingValue:
            pass
        else:
            raise AssertionError(f"set_many stored a bad {key}: {bad!r}")
        settings.invalidate_cache()
        assert settings.get(key) == "1", (bad, settings.get(key))
    # 2. the CLI door
    for bad in ("0", "11", "abc"):
        rc, out = _run_cli(["config", "set", key, bad])
        assert rc not in (0, None), (bad, rc, out)
        assert "1 to 10" in out, (bad, out)
        settings.invalidate_cache()
        assert settings.get(key) == "1", (bad, settings.get(key))
    # 3. the /api door
    admin = security.create_user(
        "rework632@x.com", "Rework 632", Role.ADMIN, "rework632-admin-pw-1"
    )
    _row, raw = apikeys.create_key(admin.id, "rework632", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    settings.invalidate_cache()
    client = TestClient(app)
    headers = {"X-Loc-Token": raw}
    for bad in ("0", "11", "abc", 0, 99):
        r = client.post("/api/admin/config", headers=headers, json={"settings": {key: bad}})
        assert r.status_code == 400 and "1 to 10" in r.text, (bad, r.status_code, r.text[:300])
    settings.invalidate_cache()
    assert settings.get(key) == "1"
    r = client.post("/api/admin/config", headers=headers, json={"settings": {key: "3"}})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    settings.invalidate_cache()
    assert settings.get(key) == "3" and vendor_rework.rework_cap() == 3
    got = client.get("/api/admin/config", headers=headers).json()
    assert got["settings"][key] == "3", got.get("settings", {}).get(key)
    # 4. the MCP door (manage_install forwards to /api/admin/config)
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "integrations",
            "mcp-server",
        ),
    )
    import server

    async def mcp_checks():
        with patch.object(
            server,
            "_client",
            side_effect=lambda: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers=headers,
            ),
        ):
            for bad in ("0", "11", "abc"):
                try:
                    await server.manage_install(action="set_config", settings={key: bad})
                except Exception as exc:
                    assert "1 to 10" in str(exc), (bad, str(exc))
                else:
                    raise AssertionError(f"MCP stored a bad {key}: {bad!r}")
            await server.manage_install(action="set_config", settings={key: "4"})

    with patch.object(server, "TOKEN", raw):
        asyncio.run(mcp_checks())
    settings.invalidate_cache()
    assert settings.get(key) == "4" and vendor_rework.rework_cap() == 4
    # 5. a raw row written AROUND the doors reads as the default (never 0 / unbounded)
    from app.models import Setting

    for raw_val in ("0", "500", "junk"):
        with Session(engine) as s:
            row = s.get(Setting, key)
            row.value = raw_val
            s.add(row)
            s.commit()
        settings.bump_version()
        settings.invalidate_cache()
        assert vendor_rework.rework_cap() == vendor_rework.DEFAULT_REWORK_MAX_ROUNDS, raw_val
    settings.set_many({key: "1", "remote_admin_enabled": "0"})
    settings.invalidate_cache()
    print("  [#632] bad vendor_rework_max_rounds refused on set_many, CLI, /api and MCP: OK")


def test_rework_return_id_accounting():
    env = _seed(quote_gated=False)
    _run_id, leg_id = _deliver(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        new_leg_id = out["new_dispatch_id"]
    with Session(engine) as s:
        new_claim = s.get(VendorDispatch, new_leg_id).claim_token
    ids = _seg_ids(env["doc_id"])
    # an UNKNOWN id in the rework return fails the WHOLE return (the id accounting still
    # runs on the rework leg's own frozen contract ledger)
    try:
        _admit(new_claim, ids + [999999], GOOD + ["x"])  # noqa: RUF005
        assert False, "an unknown id must fail the rework return"
    except vendor_return.ReturnContractError:
        pass
    with Session(engine) as s:
        assert s.get(VendorDispatch, new_leg_id).status == VendorDispatchStatus.DISPATCHED
    print("  [R4-3] rework return id accounting: unknown id fails the whole return: OK")


def test_run_cancel_closes_quoted_and_rework_legs():
    # a QUOTED leg on a cancelled run is CLOSED
    env = _seed(quote_gated=True)
    run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        orchestrator.cancel_run(s, run_id, actor="op")
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.CANCELLED
        assert s.get(WorkflowRun, run_id).status == RunStatus.CANCELLED
    # an OPEN rework leg on a cancelled run is CLOSED too
    env2 = _seed(quote_gated=False)
    r2, l2 = _deliver(env2)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, l2)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        new_leg_id = out["new_dispatch_id"]
    with Session(engine) as s:
        orchestrator.cancel_run(s, r2, actor="op")
        assert s.get(VendorDispatch, new_leg_id).status == VendorDispatchStatus.CANCELLED
    print("  [R4-4] run-cancel closes a QUOTED leg and an open rework leg: OK")


def test_rework_refused_on_cancelled_run():
    """A RETURNED leg whose run was later CANCELLED cannot be reworked -- rework must never
    resurrect a run an operator deliberately killed."""
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    # R1 (round 2): install a WORKING transport so a guard-less run would actually dispatch a
    # rework -- otherwise the mock-egress perimeter would raise the SAME ReworkStateError and
    # the test would pass even with the CANCELLED guard deleted (mutation-proven).
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            run.status = RunStatus.CANCELLED
            s.add(run)
            s.commit()
            leg = s.get(VendorDispatch, leg_id)
            try:
                vendor_rework.reject_and_rework(s, leg, actor="op")
                assert False, "reworking a cancelled run must be refused"
            except vendor_rework.ReworkStateError as exc:
                # the refusal must be the CANCELLED guard, not an incidental error
                assert "CANCELLED" in str(exc) or "cancelled" in str(exc), str(exc)
            assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETURNED
            assert s.get(WorkflowRun, run_id).status == RunStatus.CANCELLED
    finally:
        vendor_dispatch.set_test_transport(None)
    # and NO rework leg was created (the guard fired before any dispatch)
    with Session(engine) as s:
        rework_legs = [
            l
            for l in s.exec(  # noqa: E741
                select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)
            ).all()
            if (l.rework_round or 0) > 0
        ]
        assert not rework_legs, "a cancelled-run reject must create no rework leg"
    print("  [R4-5] rework is refused on a CANCELLED run (never resurrects it): OK")


def test_spend_counts_rejected_and_rework(monkeypatch=None):
    """Finding #2/#9: after reject+rework, BOTH the rejected work ($ actual) AND the rework
    ($ actual) count toward Human spend -- the honest total is the rejected work plus the
    rework. A QUOTED leg counts at estimate grade."""
    from app.services import vendor_spend

    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)  # first delivery, actual $120.0
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.actual_cost_usd == 120.0
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        new_leg_id = out["new_dispatch_id"]
    with Session(engine) as s:
        new = s.get(VendorDispatch, new_leg_id)
        _admit(new.claim_token, _seg_ids(env["doc_id"]), BETTER, cost=90.0)
    with Session(engine) as s:
        summ = vendor_spend.spend_summary(s, [run_id])
        # rejected $120 (REJECTED, actual) + rework $90 (RETURNED, actual) = $210, both actual
        assert summ["human_spend_usd"] == 210.0, ("rejected work + rework both count", summ)
        assert summ["human_actual_usd"] == 210.0 and summ["human_spend_source"] == "actual", summ
        assert summ["human_leg_count"] == 2, summ
    print("  [#2/#9] spend counts the rejected work AND the rework ($210, both actual): OK")


def test_quoted_leg_priced_by_approved_quote():
    """Finding #9: an operator-APPROVED quote prices the leg (grade 'quote') above the raw
    scoping estimate, until a settled actual arrives."""
    from app.services import vendor_spend

    env = _seed(quote_gated=True)
    run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        # before approval: QUOTED counts at estimate grade
        summ0 = vendor_spend.spend_summary(s, [run_id])
        assert summ0["human_spend_source"] == "estimate" and summ0["human_leg_count"] == 1, summ0
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            vendor_quote.approve_quote(s, leg, quoted_cost_usd=250.0, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        cost, grade = vendor_spend.leg_human_cost(s.get(VendorDispatch, leg_id))
        assert grade == "quote" and cost == 250.0, (cost, grade)
        summ = vendor_spend.spend_summary(s, [run_id])
        assert summ["human_spend_usd"] == 250.0 and summ["human_spend_source"] == "quote", summ
        assert summ["human_quote_usd"] == 250.0, summ
    print("  [#9] an approved quote prices the leg at 'quote' grade above the estimate: OK")


def test_concurrent_reject_stale_read_refused():
    """Finding #3: a second (stale-read) reject cannot also dispatch a rework. The CAS
    RETURNED->REJECTED transition is exactly-once; the loser is dropped."""
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as sA:
            legA = sA.get(VendorDispatch, leg_id)  # A's view: RETURNED (will go stale)
            with Session(engine) as sB:
                legB = sB.get(VendorDispatch, leg_id)
                out = vendor_rework.reject_and_rework(sB, legB, actor="op")  # B wins
                assert out["reworked"] is True
            # A still holds a RETURNED leg object; its reject must LOSE the CAS.
            try:
                vendor_rework.reject_and_rework(sA, legA, actor="op")
                assert False, "the stale second reject must be refused"
            except vendor_rework.ReworkStateError:
                pass
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)).all()
        # exactly ONE rework leg was created (original REJECTED + one new DISPATCHED)
        rework_legs = [l for l in legs if (l.rework_round or 0) > 0]  # noqa: E741
        assert len(rework_legs) == 1, ("only one reject may dispatch", [l.id for l in legs])  # noqa: E741
    print("  [#3] a concurrent stale-read reject loses the CAS (no double rework): OK")


def test_terminal_run_approve_refused():
    """Finding #4: approving a quote on a terminal (completed/cancelled) run must NOT re-open
    the leg / restart the clock / notify -- it CLOSES the leg and refuses."""
    env = _seed(quote_gated=True)
    run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_quote.record_quote(s, leg, quoted_cost_usd=100.0, source="manual")
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.COMPLETED  # the run moved on
        s.add(run)
        s.commit()
        try:
            vendor_quote.approve_quote(
                s, leg, actor="op"
            )  # NO transport -> would notify if it proceeded
            assert False, "approving on a terminal run must be refused"
        except vendor_quote.QuoteStateError:
            pass
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.CANCELLED, leg.status
        assert leg.quote_approved_at is None, "the leg was never approved"
    print("  [#4] approve on a terminal run refuses + closes the leg (no notify/clock): OK")


def test_reject_mock_egress_is_clean_error():
    """Finding #5: a reject with a REAL egress posture on a keyless/mock install raises a
    clean ReworkStateError (a 4xx on every surface), never an uncaught MockEgressBlocked 500;
    the leg rolls back to RETURNED so the operator can retry."""
    env = _seed(quote_gated=False)
    _run_id, leg_id = _deliver(env)
    # NO test transport installed -> the egress perimeter sees a REAL client + a mock provider.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_rework.reject_and_rework(s, leg, actor="op")
            assert False, "a mock-egress reject must raise ReworkStateError, not 500"
        except vendor_rework.ReworkStateError:
            pass
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.RETURNED, ("rolled back to RETURNED", leg.status)
    print("  [#5] reject under a real-egress mock posture is a clean ReworkStateError: OK")


def test_rework_carries_glossary_asset():
    """Finding #6: a rework re-dispatch ships the SAME informing assets an original dispatch
    does (glossary/style/context) -- a rework must not blame the glossary lane then omit the
    glossary."""
    from app.services import glossary

    env = _seed(quote_gated=False)
    with Session(engine) as s:
        glossary.add_term(
            s,
            project_id=env["project_id"],
            source_term="cat",
            target_term="gato",
            source_locale="en-US",
            target_locale="es-ES",
        )
        s.commit()
    _run_id, leg_id = _deliver(env)
    cap = []
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport(capture=cap))
        try:
            vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
    sent = json.loads(cap[-1]["body"])
    assert sent.get("assets", {}).get("glossary"), (
        "the rework wire must carry assets.glossary",
        sent.get("assets"),
    )
    print("  [#6] the rework wire carries assets.glossary (informing context preserved): OK")


def test_rework_of_quote_gated_vendor_e2e():
    """Deviation (b): a rework of a QUOTE-GATED vendor re-enters the quote gate -- the rework
    leg parks QUOTED, is approved, then delivered + admitted through the same seam."""
    env = _seed(quote_gated=True)
    run_id, leg_id = _deliver(env, quote_gated=True)  # dispatch->approve->return
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_rework.reject_and_rework(s, leg, note="redo", actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        new_leg_id = out["new_dispatch_id"]
    with Session(engine) as s:
        new = s.get(VendorDispatch, new_leg_id)
        assert new.status == VendorDispatchStatus.QUOTED, "rework re-enters the quote gate"
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            vendor_quote.approve_quote(s, new, quoted_cost_usd=75.0, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        s.refresh(new)
        assert new.status == VendorDispatchStatus.DISPATCHED
        new_claim = new.claim_token
    out = _admit(new_claim, _seg_ids(env["doc_id"]), BETTER)
    assert out["complete"] and out["resumed"]
    with Session(engine) as s:
        assert s.get(VendorDispatch, new_leg_id).status == VendorDispatchStatus.RETURNED
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
    print("  [dev-b] rework of a quote-gated vendor re-quotes, approves, delivers, admits: OK")


def test_nonadmin_spend_mask_full_key_set():
    """R3 (round 2): the non-admin mask (vendor_spend.ai_only) zeroes EVERY human_* field and
    keeps the SAME key set as a real summary -- so a future human_* key (e.g. human_quote_usd)
    cannot leak by being forgotten in a hand-list."""
    from app.services import vendor_spend

    real = {
        "ai_spend_usd": 3.0,
        "human_spend_usd": 777.0,
        "human_actual_usd": 700.0,
        "human_quote_usd": 77.0,
        "human_estimate_usd": 0.0,
        "human_declined_usd": 0.0,
        "human_spend_source": "mixed",
        "human_leg_count": 2,
        "total_spend_usd": 780.0,
    }
    masked = vendor_spend.ai_only(real)
    assert set(masked) == set(vendor_spend._empty()), "mask must carry the full key set"
    assert set(masked) >= set(real), ("mask must not drop a key the real summary has", masked)
    for k, v in masked.items():
        if k.startswith("human_"):
            assert v in (0.0, "none", 0), ("every human_* field must be zeroed", k, v)
    assert masked["ai_spend_usd"] == 3.0 and masked["total_spend_usd"] == 3.0, masked
    print("  [R3] non-admin spend mask zeroes every human_* field (future-proof): OK")


def test_declined_quote_prices_zero():
    """R5 (round 2): a DECLINED quote (escalated, never approved) prices at $0 with a
    'declined' label -- a refused price never bills as Human spend."""
    from app.services import vendor_spend

    env = _seed(quote_gated=True)
    run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_quote.record_quote(s, leg, quoted_cost_usd=2.6, source="manual")
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            vendor_quote.decline_quote(s, leg, reason="too expensive", actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.ESCALATED
        cost, grade = vendor_spend.leg_human_cost(leg)
        assert cost == 0.0 and grade == "declined", (cost, grade)
        summ = vendor_spend.spend_summary(s, [run_id])
        assert summ["human_spend_usd"] == 0.0 and summ["human_spend_source"] == "declined", summ
        assert summ["human_declined_usd"] == 0.0, summ
    print("  [R5] a declined quote prices $0 with a 'declined' label (no phantom bill): OK")


def test_reject_rollback_on_dispatch_failure():
    """R2 (round 2): a NON-ReworkStateError failure AFTER the CAS flip (e.g. record_dispatch
    raising) must roll the leg back to RETURNED, never strand it REJECTED with no rework leg."""
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    real_record = vendor_dispatch.record_dispatch

    def _boom(*a, **k):
        raise RuntimeError("simulated record_dispatch failure")

    set_pieces("app.services.vendor_dispatch", "record_dispatch", _boom)
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            try:
                vendor_rework.reject_and_rework(s, leg, actor="op")
                assert False, "the RuntimeError must propagate"
            except RuntimeError:
                pass
            s.refresh(leg)
            assert leg.status == VendorDispatchStatus.RETURNED, (
                "a post-CAS failure must roll the leg back to RETURNED",
                leg.status,
            )
    finally:
        set_pieces("app.services.vendor_dispatch", "record_dispatch", real_record)
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        rework = [
            l
            for l in s.exec(  # noqa: E741
                select(VendorDispatch).where(VendorDispatch.workflow_run_id == run_id)
            ).all()
            if (l.rework_round or 0) > 0
        ]
        assert not rework, "no rework leg may survive a failed dispatch"
    print("  [R2] a post-CAS dispatch failure rolls the leg back to RETURNED (no orphan): OK")


def test_rework_no_context_asset_and_pinned_glossary():
    """R4 (round 2): a rework matches the SEGMENTED dispatch shape -- it ships glossary +
    style_guide but NO context asset -- and honors a workflow-PINNED module glossary."""
    from app.services import glossary_module as gm

    env = _seed(quote_gated=False)
    # a module glossary with a term (coverage comes from the term's locales), APPROVED and
    # PINNED on the external step
    with Session(engine) as s:
        g = gm.create(s, name="RworkG")
        gm.set_pairs(s, g.id, [("en-US", "es-ES")])  # coverage for the pin
        gm.add_term(
            s,
            g.id,
            source_term="cat",
            target_term="gato",
            source_locale="en-US",
            target_locale="es-ES",
        )
        gm.approve(s, g.id)
        step = s.exec(
            select(orchestrator.WorkflowStep).where(
                orchestrator.WorkflowStep.workflow_id == env["wf_id"],
                orchestrator.WorkflowStep.step_type == StepType.EXTERNAL,
            )
        ).first()
        step.glossary_id = g.id
        s.add(step)
        s.commit()
    _run_id, leg_id = _deliver(env)
    cap = []
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport(capture=cap))
        try:
            vendor_rework.reject_and_rework(s, leg, actor="op")
        finally:
            vendor_dispatch.set_test_transport(None)
    sent = json.loads(cap[-1]["body"])
    assert "context" not in (sent.get("assets") or {}), (
        "a segmented rework must NOT ship a context asset",
        sent.get("assets"),
    )
    assert sent.get("assets", {}).get("glossary"), "the pinned glossary must ride the rework"
    print("  [R4] rework ships glossary+style (no context) + honors the pinned glossary: OK")


def test_zero_quote_approval_refused():
    """#10: approving a $0/unknown quote is refused (approval authorizes spend)."""
    env = _seed(quote_gated=True)
    _run_id, leg_id, _claim = _dispatch(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_quote.approve_quote(s, leg, quoted_cost_usd=0.0, actor="op")
            assert False, "approving a $0 quote must be refused"
        except vendor_quote.QuoteContractError:
            pass
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.QUOTED and leg.quote_approved_at is None
    print("  [#10] approving a $0 quote is refused (leg stays QUOTED): OK")


def test_reject_precheck_refuses_foreign_pause():
    """#11: reject refuses (leg UNTOUCHED) when the run is paused at a DIFFERENT step than the
    leg's -- re-parking would clobber that foreign pause."""
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    with Session(engine) as s:
        # simulate the run parked at a DIFFERENT step (the translate step, not the external one)
        steps = orchestrator.get_steps(s, env["wf_id"])
        other_step = next(st for st in steps if st.step_type != StepType.EXTERNAL)
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.PAUSED
        s.add(run)
        s.add(
            orchestrator.StepRun(
                workflow_run_id=run_id,
                step_id=other_step.id,
                status=RunStatus.PAUSED,
                input_text="x",
                output_text="",
            )
        )
        s.commit()
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_rework.reject_and_rework(s, leg, actor="op")
            assert False, "reject must refuse when the run is paused at a different step"
        except vendor_rework.ReworkStateError as exc:
            assert "different step" in str(exc), str(exc)
        s.refresh(leg)
        assert leg.status == VendorDispatchStatus.RETURNED, "the leg is untouched on refusal"
        # the foreign pause is intact
        sr = orchestrator._latest_paused_steprun(s, run_id)
        assert sr.step_id == other_step.id, "the foreign pause survives"
    print("  [#11] reject precheck refuses a foreign-step pause, leg untouched: OK")


def test_feedback_never_blames_undispatched_segment():
    """#15: auto-derived feedback is restricted to the leg's dispatched segments -- a segment
    the vendor was never sent (flagged for another reason) is never blamed."""
    env = _seed(quote_gated=False)
    _run_id, leg_id = _deliver(env)
    from app.services import quality

    with Session(engine) as s:
        # flag a segment that is NOT in the leg's dispatched set (fabricate a foreign id path):
        # mark every current segment NEEDS_REVIEW, then dispatch feedback only covers dispatched
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        for sg in segs:
            sg.status = quality.SegStatus.NEEDS_REVIEW
            s.add(sg)
        s.commit()
        leg = s.get(VendorDispatch, leg_id)
        # shrink the leg's dispatched ledger to ONLY the first id, so the others are "undispatched"
        led = dict(leg.contract_ids or {})
        led["dispatched"] = [str(segs[0].id)]
        led["returned"] = [str(segs[0].id)]
        leg.contract_ids = led
        s.add(leg)
        s.commit()
        fb = vendor_rework.build_rework_feedback(s, leg, None, "")
        fb_ids = {str(x["id"]) for x in fb["segments"]}
        assert fb_ids <= {str(segs[0].id)}, ("only the dispatched segment may be blamed", fb_ids)
    print("  [#15] rework feedback never blames an undispatched segment: OK")


def test_cap_escalation_writes_notes_and_state():
    """#7: the cap escalation writes a run note and renders a 'rework_escalated' pause state."""
    env = _seed(quote_gated=False)
    run_id, leg_id = _deliver(env)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        vendor_dispatch.set_test_transport(_mock_transport())
        try:
            out = vendor_rework.reject_and_rework(s, leg, actor="op")  # round 1
        finally:
            vendor_dispatch.set_test_transport(None)
        new_leg_id = out["new_dispatch_id"]
        _admit(s.get(VendorDispatch, new_leg_id).claim_token, _seg_ids(env["doc_id"]), GOOD)
    with Session(engine) as s:
        new = s.get(VendorDispatch, new_leg_id)
        out = vendor_rework.reject_and_rework(s, new, actor="op")  # round 2 -> cap escalate
        assert out["escalated"] is True
        run = s.get(WorkflowRun, run_id)
        assert run.notes and "escalated to human review" in run.notes, run.notes
        _lg, state = vendor_dispatch.vendor_pause_state(s, run_id)
        assert state == "rework_escalated" and not vendor_dispatch.blocks_human_resume(state)
    print("  [#7] cap escalation writes a run note + renders 'rework_escalated' (non-block): OK")


def test_migration_downgrade_rejected_to_cancelled():
    """#13: the alembic downgrade normalizes a REJECTED leg to 'cancelled' (not 'returned',
    which would double-count it against its rework leg) before dropping the Wave-E columns."""
    import glob as _glob
    import sqlite3
    import subprocess
    import tempfile

    d = tempfile.mkdtemp(prefix="loctms_mig_e_")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env2 = dict(os.environ, LOC_TMS_DATA_DIR=d)

    def _alembic(*a):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *a],
            env=env2,
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (a, r.stdout[-800:], r.stderr[-800:])

    _alembic("upgrade", "head")
    dbs = _glob.glob(os.path.join(d, "*.db")) + _glob.glob(os.path.join(d, "*.sqlite*"))
    assert dbs, ("no db produced", os.listdir(d))
    dbf = dbs[0]
    con = sqlite3.connect(dbf)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        cols = con.execute("PRAGMA table_info(vendordispatch)").fetchall()
        names = [c[1] for c in cols if not c[5]]

        def _val(c):
            name, ctype, notnull = c[1], (c[2] or "").upper(), c[3]
            if name == "status":
                return "rejected"
            if name in ("vendor_id", "workflow_run_id"):
                return 1
            if not notnull:
                return None
            if "INT" in ctype or "BOOL" in ctype:
                return 0
            if "FLOAT" in ctype or "REAL" in ctype:
                return 0.0
            if "DATE" in ctype or "TIME" in ctype:
                return "2026-01-01 00:00:00"
            return ""

        ph = ",".join("?" for _ in names)
        con.execute(
            f"INSERT INTO vendordispatch ({','.join(names)}) VALUES ({ph})",
            [_val(c) for c in cols if not c[5]],
        )
        con.commit()
    finally:
        con.close()
    _alembic("downgrade", "b2c3d4e5f6a7")  # the Wave-E revision's parent
    con = sqlite3.connect(dbf)
    try:
        after = {c[1] for c in con.execute("PRAGMA table_info(vendordispatch)").fetchall()}
        assert "requires_quote" not in after and "rework_of" not in after, (
            "downgrade must drop the Wave-E columns"
        )
        rejected = con.execute(
            "SELECT COUNT(*) FROM vendordispatch WHERE status='rejected'"
        ).fetchone()[0]
        cancelled = con.execute(
            "SELECT COUNT(*) FROM vendordispatch WHERE status='cancelled'"
        ).fetchone()[0]
        assert rejected == 0 and cancelled >= 1, ("REJECTED -> cancelled", rejected, cancelled)
    finally:
        con.close()
    print("  [#13] downgrade normalizes REJECTED -> 'cancelled' (no double-count): OK")


# ============================================================================
# #448 -- a huge / infinite vendor Retry-After must not overflow the retry backoff
# ============================================================================
def _retry_after_transport(value):
    """A retryable 503 that echoes `value` verbatim as the Retry-After header (the raw
    vendor-supplied string, so '1e20'/'inf'/'nan' reach _parse_retry_after unmodified)."""
    return httpx.MockTransport(
        lambda req: httpx.Response(503, headers={"retry-after": str(value)}, json={})
    )


def _run_to_retry(env):
    """Drive the run to its external step under a retryable 503 (no Retry-After), parking a
    RETRY_WAIT leg (attempt 1). Returns (run_id, leg_id)."""
    vendor_dispatch.set_test_transport(
        httpx.MockTransport(lambda req: httpx.Response(503, json={}))
    )
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == env["wf_id"])
            .order_by(WorkflowRun.id.desc())
        ).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        return run.id, leg.id


def test_parse_retry_after_rejects_non_finite():
    """#448 (parser side): a huge but FINITE Retry-After (1e20) is a real number -- keep it
    (it is bounded later, in retry_leg). A NON-finite one (inf / -inf / nan) is not a usable
    wait, so it must parse to None rather than propagate a value a later timedelta(seconds=)
    would choke on."""
    p = vendor_dispatch._parse_retry_after
    assert p("1e20") == 1e20, "a huge FINITE value is kept (the cap is applied in retry_leg)"
    assert p("inf") is None, "inf must be rejected to None (not accepted, not a crash)"
    assert p("-inf") is None, "-inf must be rejected to None"
    assert p("nan") is None, "nan must be rejected to None"
    # Pre-existing accepted / rejected behaviour is unchanged.
    assert p("45") == 45.0 and p("0") == 0.0
    assert p("-1") is None and p("") is None and p(None) is None
    print("  [#448] _parse_retry_after: 1e20 kept; inf/-inf/nan rejected to None: OK")


def test_retry_leg_huge_or_infinite_retry_after_does_not_overflow():
    """#448 (retry_leg side): a Retry-After of 1e20 / inf / nan must NOT raise OverflowError.
    By the time the wait is computed the failed attempt is already committed, so a crash would
    strand the leg with a PAST next_attempt_at -- it would re-send every sweep (~30s), bypassing
    both the bounded backoff and RETRY_AFTER_FLOOR. The wait must instead land IN THE FUTURE and
    stay bounded by the next backoff step."""
    for value in ("1e20", "inf", "nan"):
        env = _seed()
        _run_id, leg_id = _run_to_retry(env)  # attempt 1 -> parked RETRY_WAIT
        with Session(engine) as s:
            assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.RETRY_WAIT
        now = datetime.utcnow()
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            outcome = vendor_dispatch.retry_leg(
                s, leg, now=now, transport=_retry_after_transport(value)
            )
            assert outcome == "retry_wait", (value, outcome)
            leg = s.get(VendorDispatch, leg_id)
            assert leg.next_attempt_at is not None and leg.next_attempt_at > now, (
                value,
                "next_attempt_at must be in the future (a past value = a per-sweep re-send)",
            )
            delta = (leg.next_attempt_at - now).total_seconds()
            assert math.isfinite(delta) and delta >= 0, (value, delta)
            cap = vendor_dispatch.next_backoff(leg.attempt_count).total_seconds()
            assert delta <= cap + 1, (value, "must be bounded by the backoff cap", delta, cap)
    print("  [#448] retry_leg with Retry-After 1e20/inf/nan: no OverflowError, bounded: OK")


def main():
    test_flag_off_is_byte_identical()
    test_quote_gated_parks_quoted_and_blocks()
    test_record_approve_then_return_reconciles()
    test_signed_quote_wire_message_and_negatives()
    test_decline_escalates_to_human()
    test_quoted_leg_expiry_escalates()
    test_full_native_roundtrip_with_rework()
    test_rework_cap_escalates()
    test_rework_max_rounds_setting_honored()
    test_rework_max_rounds_invalid_refused_every_door()
    test_rework_return_id_accounting()
    test_run_cancel_closes_quoted_and_rework_legs()
    test_rework_refused_on_cancelled_run()
    test_spend_counts_rejected_and_rework()
    test_quoted_leg_priced_by_approved_quote()
    test_concurrent_reject_stale_read_refused()
    test_terminal_run_approve_refused()
    test_reject_mock_egress_is_clean_error()
    test_rework_carries_glossary_asset()
    test_rework_of_quote_gated_vendor_e2e()
    test_nonadmin_spend_mask_full_key_set()
    test_declined_quote_prices_zero()
    test_reject_rollback_on_dispatch_failure()
    test_rework_no_context_asset_and_pinned_glossary()
    test_zero_quote_approval_refused()
    test_reject_precheck_refuses_foreign_pause()
    test_feedback_never_blames_undispatched_segment()
    test_cap_escalation_writes_notes_and_state()
    test_migration_downgrade_rejected_to_cancelled()
    test_parse_retry_after_rejects_non_finite()
    test_retry_leg_huge_or_infinite_retry_after_does_not_overflow()
    print("VENDOR WAVE E (quote gate + reject/rework) OK")


if __name__ == "__main__":
    main()
