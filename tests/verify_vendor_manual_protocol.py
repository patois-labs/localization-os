"""The `manual` (file-exchange) transport protocol for the EXTERNAL / vendor dispatch step
(feat/vendor-manual-protocol; interop research F2 Tier-2 — the LSP majority: SFTP/email tiers
and any human running a desktop CAT tool like OmegaT).

The gap this closes: before this change the EXTERNAL step could only dispatch via `native`
(a signed JSON contract POSTed to a reachable HTTPS endpoint, SSRF-guarded). A vendor with NO
system endpoint could not be dispatched to at all — `_native_send` refused with "Blocked
target: scheme '(none)' not allowed" — and a leg only admits a return while DISPATCHED, so
there was no workaround. `manual` registers a SECOND transport whose `send` performs NO
network I/O: delivery is the operator downloading the leg's XLIFF/JSON rendition and handing
it to the vendor out-of-band; the leg still lands exactly like a delivered native dispatch and
its return funnels through the SAME guarded admit_manual -> commit_mt_output seam (R1).

Hermetic + offline. Crucially, the manual dispatch tests install an httpx transport that
RAISES if any request is attempted, PROVING zero network I/O on the manual path (a native
dispatch to no endpoint would instead be refused by the SSRF/scheme guard):

  [A] manual dispatch, NO endpoint: leg DISPATCHED, claim token + dispatched_ids ledger +
      estimate + expires_at recorded, endpoint_host EMPTY, run parked PAUSED; the raising
      transport is never touched.
  [B] a quote-gated manual vendor parks QUOTED (Wave-E snapshot composes unchanged).
  [C] render_leg_xliff produces bytes carrying the leg's exact trans-unit ids.
  [D] a manual leg admits a manual return end-to-end: targets land, leg RETURNED, run resumes.
  [E] manual legs are NEVER polled (pollable filter + poll_leg refusal), even with a poll_url.
  [F] protocol validation: `manual` accepted at vendor create/update on CLI + API; an unknown
      protocol is still refused on both.
  [G] a manual vendor MAY carry endpoint_url (ignored, never sent to); a leg still records an
      EMPTY host.
  [H] a NATIVE vendor is byte-identical: it DOES ride the wire (the same transport), records a
      host, and dispatches DISPATCHED — the manual path did not perturb it.

NOTE: every test here covers NEW behaviour (`manual` did not exist before this branch), so
none is a fail-before proof of a pre-existing defect and none pins a commit sha.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_vmanual_")


import httpx
from lxml import etree
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    AuditLog,
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
    WorkflowStep,
)
from app.services import (
    orchestrator,
    vendor_dispatch,
    vendor_quote,
    vendor_return,
    vendor_rework,
    vendor_xliff,
    vendors,
)
from app.services import workflows as wf

init_db()

_SEQ = [0]
XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
SOURCES = ["Hello ⟦x1⟧ world.", "The cat sat on the mat.", "Please sign in now."]
GOOD = ["Hola ⟦x1⟧ mundo.", "El gato se sento en la alfombra.", "Por favor inicie sesion ahora."]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _raising_transport():
    """An httpx transport that FAILS LOUDLY the instant any request is attempted. A manual
    dispatch must never reach it (no wire); if it fires, the manual path leaked a request."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"a manual dispatch attempted a network request to {req.url!r} — the "
            "file-exchange transport must perform NO network I/O"
        )

    return httpx.MockTransport(handler)


def _ok_transport(status=200, capture=None):
    """A benign 2xx transport for the NATIVE control test (proves native still rides the wire)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(str(req.url))
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler)


# --- seeding -----------------------------------------------------------------
def _seed(
    *,
    protocol="manual",
    endpoint="",
    quote_gated=False,
    poll_url=None,
    signing=None,
    include_xliff=False,
):
    """A customer + project + doc + a vendor (manual by default, NO endpoint) linked to that
    customer + a workflow (translate -> external)."""
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
        cfg = {}
        if quote_gated:
            cfg["requires_quote_approval"] = True
        if poll_url:
            cfg["poll_url"] = poll_url
        if include_xliff:
            cfg["include_xliff"] = True
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url=endpoint,
            protocol=protocol,
            config=cfg or None,
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        if signing:
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


def _dispatch(env, *, transport, locale="es-ES"):
    """Run the workflow to its external step under `transport`; return (run_id, leg_id, claim)."""
    vendor_dispatch.set_test_transport(transport)
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], locale))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        return run.id, (leg.id if leg else None), (leg.claim_token if leg else None)


def _seg_ids(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, locale)]


def _targets(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [
            (sg.target_text or "", sg.status)
            for sg in orchestrator._load_segments(s, doc_id, locale)
        ]


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


# ============================================================================
# [A] manual dispatch with NO endpoint: lands exactly like a delivered native leg,
#     zero network I/O.
# ============================================================================
def test_manual_dispatch_lands_dispatched_with_no_network_io():
    env = _seed(protocol="manual", endpoint="")  # NO endpoint at all
    # A native dispatch to no endpoint would be refused by the SSRF/scheme guard; a manual
    # dispatch must SUCCEED without ever touching the raising transport.
    run_id, leg_id, _claim = _dispatch(env, transport=_raising_transport())
    assert leg_id is not None, "the manual external step must produce a dispatch leg"
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, leg.status
        assert leg.claim_token, "a claim token must be minted for a manual leg"
        assert vendor_return.dispatched_ids(leg) == [str(i) for i in ids], (
            "the manual leg must freeze the same dispatched-ids ledger a native leg does"
        )
        assert leg.estimate and leg.estimate_cost_usd > 0, (
            "the scoping estimate must be priced + recorded for a manual leg"
        )
        assert leg.expires_at is not None, (
            "external_wait_hours must still set an expiry — a manual leg nobody returns "
            "escalates to a human on expiry"
        )
        assert leg.endpoint_host == "", "a manual leg records NO host (there is no endpoint)"
        assert vendor_dispatch.vendor_protocol(s.get(Vendor, leg.vendor_id)) == "manual"
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED, "the run parks PAUSED awaiting the vendor"
    print(
        "  [A] manual dispatch: leg DISPATCHED, token + ledger + estimate + expiry, host "
        "empty, run PAUSED, zero network I/O: OK"
    )


# ============================================================================
# [B] a quote-gated manual vendor parks QUOTED (Wave-E snapshot composes unchanged).
# ============================================================================
def test_quote_gated_manual_leg_parks_quoted():
    env = _seed(protocol="manual", endpoint="", quote_gated=True)
    run_id, leg_id, _claim = _dispatch(env, transport=_raising_transport())
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.QUOTED, (
            "a quote-gated manual dispatch must park QUOTED, not DISPATCHED",
            leg.status,
        )
        assert leg.requires_quote is True
        assert leg.endpoint_host == ""
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    print("  [B] a quote-gated manual leg parks QUOTED (Wave-E composition unchanged): OK")


# ============================================================================
# [C] render_leg_xliff renders a manual leg (the operator download surface).
# ============================================================================
def test_render_leg_xliff_for_a_manual_leg():
    env = _seed(protocol="manual", endpoint="")
    _run_id, leg_id, _claim = _dispatch(env, transport=_raising_transport())
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        data = vendor_xliff.render_leg_xliff(s, leg)
    assert isinstance(data, bytes) and data, "render_leg_xliff must return XLIFF bytes"
    root = etree.fromstring(data)
    tu_ids = {tu.get("id") for tu in root.iter(f"{{{XLIFF_NS}}}trans-unit")}
    assert tu_ids == {str(i) for i in ids}, (
        "the rendered XLIFF must carry exactly the manual leg's dispatched ids",
        tu_ids,
    )
    print("  [C] render_leg_xliff rebuilds a manual leg's XLIFF with the right ids: OK")


# ============================================================================
# [D] a manual leg admits a manual return end-to-end (the whole point of `manual`).
# ============================================================================
def test_manual_leg_admits_a_manual_return_and_resumes():
    env = _seed(protocol="manual", endpoint="")
    run_id, leg_id, claim = _dispatch(env, transport=_raising_transport())
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids, GOOD, claim=claim, cost=77.0), resume="sync", actor="tester"
        )
    assert out["complete"] is True and out["resumed"] is True, out
    assert len(out["admitted"]) == 3, out
    for (txt, _st), want in zip(_targets(env["doc_id"]), GOOD, strict=False):
        assert txt == want, ("the vendor's returned text must land", txt, want)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
        assert leg.returned_at is not None and leg.actual_cost_usd == 77.0
        assert vendor_return.outstanding_ids(leg) == []
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED, (
            "the return must resume the run to completion"
        )
    print(
        "  [D] a manual leg admits a manual return end-to-end: targets land, leg RETURNED, "
        "run resumed: OK"
    )


# ============================================================================
# [E] manual legs are NEVER polled.
# ============================================================================
def test_manual_legs_are_never_polled():
    # A manual vendor that even (harmlessly) carries a poll_url must still never be polled.
    env = _seed(protocol="manual", endpoint="", poll_url="https://vendor.example.com/returns")
    _run_id, leg_id, _claim = _dispatch(env, transport=_raising_transport())
    with Session(engine) as s:
        # invisible to the automatic sweep despite the poll_url...
        assert vendor_return.pollable_legs(s, vendor_id=env["vendor_id"]) == []
        assert vendor_return.poll_due_legs(s, vendor_id=env["vendor_id"])["polled"] == 0
        # ...and a direct poll is refused loudly (no poll implementation for a manual transport).
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_return.poll_leg(s, leg, transport=_raising_transport())
            raise AssertionError("a manual leg must not be pollable")
        except vendor_return.VendorPollError as exc:
            assert "manual" in str(exc), str(exc)
        # the leg_to_dict `pollable` flag reflects it too.
        assert vendor_return.leg_to_dict(s, leg)["pollable"] is False
    print(
        "  [E] a manual leg is never polled (sweep filter + poll_leg refusal + pollable "
        "flag), even with a poll_url present: OK"
    )


# ============================================================================
# [E-flip] a manual-dispatched leg stays UNPOLLABLE after the vendor is flipped to native
#          while the leg is parked (kills re-review mutation E: sweep filter + pollable flag
#          reverted to the vendor's LIVE protocol instead of the leg's snapshot).
# ============================================================================
def test_manual_leg_stays_unpollable_after_vendor_flipped_to_native():
    # A manual leg was never put on the wire, so there is nothing to poll for and forwarding the
    # bearer + claim token would leak credentials for a package never delivered -- that must hold
    # even after the vendor is flipped to `native`. The poll decision keys off the LEG's SNAPSHOT
    # (`manual`), NOT the vendor's now-live protocol. Mutation E (pollable_legs / leg_to_dict
    # keying off vendor_protocol) would wrongly resurrect this leg into the poll sweep.
    env = _seed(protocol="manual", endpoint="", poll_url="https://vendor.example.com/returns")
    _run_id, leg_id, _claim = _dispatch(env, transport=_raising_transport())
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.protocol == "manual", ("the leg must snapshot the manual protocol", leg.protocol)
        # flip the vendor to native (with an endpoint, as a real flip would) WHILE the leg is parked.  # noqa: E501
        v = s.get(Vendor, env["vendor_id"])
        v.protocol = "native"
        v.endpoint_url = "https://vendor.example.com/intake"
        s.add(v)
        s.commit()
        assert vendor_dispatch.vendor_protocol(v) == "native"
        assert vendor_dispatch.leg_protocol(leg, v) == "manual", (
            "leg_protocol must return the LEG's snapshot, not the vendor's flipped protocol"
        )
    with Session(engine) as s:
        # the sweep filter still EXCLUDES it despite the vendor now being native + carrying a poll_url.  # noqa: E501
        assert vendor_return.pollable_legs(s, vendor_id=env["vendor_id"]) == [], (
            "a manual-dispatched leg must stay out of the poll sweep after a flip to native"
        )
        assert vendor_return.poll_due_legs(s, vendor_id=env["vendor_id"])["polled"] == 0
        leg = s.get(VendorDispatch, leg_id)
        # the leg_to_dict `pollable` flag reflects the snapshot, not the vendor's live protocol.
        assert vendor_return.leg_to_dict(s, leg)["pollable"] is False, (
            "leg_to_dict.pollable must key off the leg snapshot, not the vendor's live protocol"
        )
        # a direct poll is refused (the raising transport proves no wire I/O is even attempted).
        try:
            vendor_return.poll_leg(s, leg, transport=_raising_transport())
            raise AssertionError(
                "a manual-snapshot leg must not be pollable after a flip to native"
            )
        except vendor_return.VendorPollError as exc:
            assert "manual" in str(exc), str(exc)
    print(
        "  [E-flip] a manual-dispatched leg stays unpollable (sweep filter + pollable flag + "
        "poll refusal) after the vendor is flipped to native while parked: OK"
    )


# ============================================================================
# [F] protocol validation across the surfaces (`manual` accepted; unknown refused).
# ============================================================================
def test_manual_is_a_registered_protocol():
    assert "manual" in vendor_dispatch.protocol_names()
    assert vendor_dispatch.is_known_protocol("manual") is True
    assert vendor_dispatch.is_known_protocol("MANUAL") is True  # normalized
    assert vendor_dispatch.is_known_protocol("carrier-pigeon") is False
    print("  [F] `manual` is a registered protocol; an unknown one is not: OK")


def test_service_create_update_accepts_manual_and_refuses_unknown():
    with Session(engine) as s:
        cust = Customer(name=f"C-{_seq()}")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        # create with protocol=manual
        v = vendors.create(s, f"Manual-{_seq()}", protocol="manual")
        assert v.protocol == "manual"
        # update a native vendor to manual, and back
        v2 = vendors.create(s, f"Native-{_seq()}")  # defaults to native
        assert (v2.protocol or "native") == "native"
        vendors.update(s, v2.id, protocol="manual")
        assert s.get(Vendor, v2.id).protocol == "manual"
        # an unknown protocol is refused at WRITE time on both verbs.
        for fn in (
            lambda: vendors.create(s, f"Bad-{_seq()}", protocol="smtp-magic"),
            lambda: vendors.update(s, v.id, protocol="smtp-magic"),
        ):
            try:
                fn()
                raise AssertionError("an unknown protocol must be refused")
            except vendors.VendorError as exc:
                assert "manual" in str(exc) and "native" in str(exc), (
                    "the refusal must list the valid protocols",
                    str(exc),
                )
    print("  [F] vendors.create / update accept `manual` and refuse an unknown protocol: OK")


def test_cli_and_api_accept_manual_and_refuse_unknown():
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import app
    from tests._cli_runner import run_cli

    # CLI: `manual` is an offered choice and creates a manual vendor (name is positional).
    code, out, err = run_cli(
        ["--json", "vendor", "add", f"CliManual-{_seq()}", "--protocol", "manual"]
    )
    assert code == 0, (code, out, err)
    row = _json.loads(out.strip().splitlines()[-1])
    assert row["ok"] is True and row["protocol"] == "manual", row
    # CLI: an unknown protocol is rejected by argparse choices (before any service call).
    code, out, err = run_cli(["vendor", "add", "Nope", "--protocol", "bogus"])
    assert code != 0 and "bogus" in (out + err), (code, out, err)
    assert (out + err).isascii(), "CLI output must stay ASCII"

    # API: create with protocol=manual over the ADMIN-gated route (the whole vendor /api
    # surface is admin-only, H1). Mint a real admin api key exactly as verify_vendor_api does.
    from app.models import Role
    from app.services import apikeys, security, settings

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    u = security.create_user(
        f"vmanual-admin-{_seq()}@x.com", "VM Admin", Role.ADMIN, "vmanual-admin-password-123"
    )
    _row, raw = apikeys.create_key(u.id, f"vmanual-api-{_seq()}", scopes="api docs")
    hdr = {"X-Loc-Token": raw}
    client = TestClient(app)
    r = client.post(
        "/api/vendors", json={"name": f"ApiManual-{_seq()}", "protocol": "manual"}, headers=hdr
    )
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["vendor"]["protocol"] == "manual", r.json()
    # an unknown protocol is a clean 400 (VendorError -> 400), not a 500.
    r2 = client.post(
        "/api/vendors", json={"name": f"ApiBad-{_seq()}", "protocol": "bogus"}, headers=hdr
    )
    assert r2.status_code == 400 and r2.json()["ok"] is False, (r2.status_code, r2.text)
    print("  [F] CLI + API accept `manual` and refuse an unknown protocol: OK")


# ============================================================================
# [G] a manual vendor MAY carry endpoint_url (ignored); the leg still records no host.
# ============================================================================
def test_manual_vendor_may_carry_endpoint_but_it_is_ignored():
    # A vendor migrated from native to manual might still have an https endpoint on file.
    env = _seed(protocol="manual", endpoint="https://old-endpoint.example.com/intake")
    _run_id, leg_id, _claim = _dispatch(env, transport=_raising_transport())
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED
        assert leg.endpoint_host == "", (
            "a manual leg must record an empty host even when endpoint_url is set",
            leg.endpoint_host,
        )
    print(
        "  [G] a manual vendor's endpoint_url is tolerated but ignored; the leg has no "
        "host and nothing was sent: OK"
    )


# ============================================================================
# [H] a NATIVE vendor is byte-identical: it rides the wire and records a host.
# ============================================================================
def test_native_dispatch_is_unperturbed():
    env = _seed(
        protocol="native", endpoint="https://vendor.example.com/intake", signing="native-signing"
    )
    cap = []
    run_id, leg_id, _claim = _dispatch(env, transport=_ok_transport(200, capture=cap))
    assert cap and "vendor.example.com" in cap[0], (
        "a native dispatch MUST ride the wire through the transport",
        cap,
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED
        assert leg.endpoint_host == "vendor.example.com", (
            "a native leg still records its endpoint host",
            leg.endpoint_host,
        )
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    print("  [H] a native dispatch is byte-identical - rides the wire, records its host: OK")


# ============================================================================
# F2/F1: a native RETRY_WAIT leg must NOT phantom-"deliver" through the manual no-op
#        after the vendor is flipped to manual while the leg is parked.
# ============================================================================
def test_native_retry_leg_does_not_phantom_deliver_after_flip_to_manual():
    # native dispatch whose FIRST attempt returns a retryable 503 -> the leg parks RETRY_WAIT,
    # snapshotting protocol='native'. The run parks PAUSED at the external step.
    env = _seed(protocol="native", endpoint="https://vendor.example.com/intake", signing="s")
    run_id, leg_id, _claim = _dispatch(env, transport=_ok_transport(503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT, (
            "a 503 first dispatch must park RETRY_WAIT",
            leg.status,
        )
        assert leg.protocol == "native", (
            "the leg must SNAPSHOT the protocol it was dispatched under",
            leg.protocol,
        )
        # flip the vendor to `manual` WHILE the native leg is parked for retry.
        v = s.get(Vendor, env["vendor_id"])
        v.protocol = "manual"
        s.add(v)
        s.commit()
        assert vendor_dispatch.vendor_protocol(v) == "manual"
        assert vendor_dispatch.leg_protocol(leg, v) == "native", (
            "leg_protocol must return the LEG's snapshot, not the vendor's flipped protocol"
        )

    # Retry with a RAISING transport. If retry_leg keyed off the vendor's LIVE (now manual)
    # protocol, the manual no-op transport would return None and the success path would flip
    # the leg DISPATCHED -- a PHANTOM delivery of a package that never left the process. With
    # the F2 snapshot the leg instead DEAD-LETTERS (documented belt-and-braces decision), and
    # the raising transport is NEVER touched (nothing is sent).
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        outcome = vendor_dispatch.retry_leg(s, leg, transport=_raising_transport())
        assert outcome == "dead_letter", (
            "a protocol flip while parked must dead-letter, never phantom-deliver",
            outcome,
        )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DEAD_LETTER, leg.status
        assert leg.status != VendorDispatchStatus.DISPATCHED, (
            "the leg must NOT flip to DISPATCHED -- that would be a phantom delivery"
        )
        assert leg.retry_contract is None, "a dead-lettered leg drops its held package"
        assert "protocol changed" in (leg.last_error or ""), (
            "the sanitized last_error must name the protocol change",
            leg.last_error,
        )
        # decision 3: dead-letter escalates to a human -> the run stays PAUSED, never FAILED.
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
        rows = s.exec(
            select(AuditLog).where(AuditLog.action == "vendor.dispatch_dead_lettered")
        ).all()
        assert any(f"dispatch {leg_id} " in (r.target or "") for r in rows), (
            "the dead-letter must be audited",
            [r.target for r in rows],
        )
    print(
        "  [F1] a native RETRY_WAIT leg dead-letters (no phantom delivery, nothing sent) "
        "when the vendor is flipped to manual while parked: OK"
    )


# ============================================================================
# [F1-legacy]: a native RETRY_WAIT leg whose protocol SNAPSHOT is NULL (a leg dispatched
#     BEFORE migration d4e5f6a7b8c9 added the column) must ALSO dead-letter on a flip to
#     manual -- leg_protocol resolves a NULL snapshot to NATIVE (a pre-migration leg was
#     native by construction), NOT to the vendor's live protocol. This is the re-review's
#     reproduced phantom (BLOCKING #1) + kills mutation B (NULL fallback -> live protocol).
# ============================================================================
def test_legacy_null_snapshot_native_leg_dead_letters_on_flip_to_manual():
    env = _seed(protocol="native", endpoint="https://vendor.example.com/intake", signing="s")
    run_id, leg_id, _claim = _dispatch(env, transport=_ok_transport(503))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT, (
            "a 503 first dispatch must park RETRY_WAIT",
            leg.status,
        )
        # FORCE the snapshot NULL on the row to simulate a pre-migration legacy leg, then flip
        # the vendor to manual WHILE the leg is parked (the raising transport must never fire).
        leg.protocol = None
        s.add(leg)
        v = s.get(Vendor, env["vendor_id"])
        v.protocol = "manual"
        s.add(v)
        s.commit()
        assert vendor_dispatch.leg_protocol(s.get(VendorDispatch, leg_id), v) == "native", (
            "a NULL snapshot must resolve to native (a pre-migration leg was native by "
            "construction), NOT the vendor's flipped-to-manual live protocol"
        )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        outcome = vendor_dispatch.retry_leg(s, leg, transport=_raising_transport())
        assert outcome == "dead_letter", (
            "a legacy NULL-snapshot native leg must dead-letter on a flip to manual, never "
            "phantom-deliver through the manual no-op",
            outcome,
        )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DEAD_LETTER, leg.status
        assert leg.status != VendorDispatchStatus.DISPATCHED, (
            "the leg must NOT flip DISPATCHED -- that would be the phantom delivery"
        )
        assert leg.retry_contract is None, "a dead-lettered leg drops its held package"
        assert "protocol changed" in (leg.last_error or ""), leg.last_error
        # decision 3: dead-letter escalates to a human -> the run stays PAUSED, never FAILED.
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    print(
        "  [F1-legacy] a legacy NULL-snapshot native leg dead-letters (no phantom delivery, "
        "nothing sent) when the vendor is flipped to manual while parked: OK"
    )


# ============================================================================
# F3: a rework re-dispatch keys off the ORIGINAL leg's protocol snapshot, follows the
#     manual contract (skip egress, empty host, no wire I/O), and works offline.
# ============================================================================
def _make_returned_leg(env, *, transport, claim_getter=None):
    """Dispatch to the external step under `transport` and admit a full manual return so the
    leg lands RETURNED (the reworkable state). Returns (run_id, leg_id)."""
    run_id, leg_id, claim = _dispatch(env, transport=transport)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        vendor_return.admit_manual(
            s, _payload(ids, GOOD, claim=claim, cost=50.0), resume="sync", actor="tester"
        )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED, leg.status
    return run_id, leg_id


def test_rework_of_manual_leg_follows_original_snapshot_no_io():
    # An original MANUAL leg (no endpoint), delivered + RETURNED. Then the vendor is FLIPPED to
    # native AND given an endpoint. A rework keyed off the vendor's LIVE protocol would try to
    # POST to that endpoint (hitting the raising transport); keyed off the ORIGINAL leg's
    # SNAPSHOT it must stay manual -- empty host, no wire I/O.
    env = _seed(protocol="manual", endpoint="")
    _run_id, orig_leg_id = _make_returned_leg(env, transport=_raising_transport())
    with Session(engine) as s:
        v = s.get(Vendor, env["vendor_id"])
        v.protocol = "native"
        v.endpoint_url = "https://vendor.example.com/intake"
        s.add(v)
        s.commit()

    with Session(engine) as s:
        leg = s.get(VendorDispatch, orig_leg_id)
        # the raising transport proves zero I/O: a native re-dispatch would hit it and blow up.
        out = vendor_rework.reject_and_rework(
            s, leg, note="please fix tone", actor="tester", transport=_raising_transport()
        )
    assert out["reworked"] is True, out
    new_id = out["new_dispatch_id"]
    with Session(engine) as s:
        new_leg = s.get(VendorDispatch, new_id)
        assert new_leg.protocol == "manual", (
            "the rework leg must inherit the ORIGINAL leg's protocol snapshot, not the "
            "vendor's flipped-to-native protocol",
            new_leg.protocol,
        )
        assert new_leg.endpoint_host == "", (
            "a manual rework records NO host even though the vendor now carries an endpoint",
            new_leg.endpoint_host,
        )
        assert new_leg.status == VendorDispatchStatus.DISPATCHED, new_leg.status
    print(
        "  [F3] a rework of a manual leg follows the ORIGINAL leg's snapshot (manual: empty "
        "host, no wire I/O) even after the vendor is flipped to native: OK"
    )


def test_rework_egress_posture_manual_not_refused_native_refused():
    # egress_is_real posture (a real-classified transport) + a MOCK provider. The egress skip
    # for a wireless (manual) rework is LOAD-BEARING: it must NOT be refused, while a native
    # rework in the SAME posture IS refused (MockEgressBlocked -> ReworkStateError).
    real = httpx.HTTPTransport()  # egress_guard.egress_is_real(...) == True

    # (A) manual rework: succeeds despite the real-egress posture (nothing goes on the wire).
    env_m = _seed(protocol="manual", endpoint="")
    _run_m, leg_m = _make_returned_leg(env_m, transport=_raising_transport())
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_m)
        out = vendor_rework.reject_and_rework(s, leg, note="n", actor="tester", transport=real)
    assert out["reworked"] is True, (
        "a manual rework must NOT be refused in an egress_is_real "
        "posture (the skip is load-bearing)",
        out,
    )

    # (B) native rework in the same posture: refused (offline/mock content to a real endpoint).
    env_n = _seed(protocol="native", endpoint="https://vendor.example.com/intake", signing="s")
    _run_n, leg_n = _make_returned_leg(env_n, transport=_ok_transport(200))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_n)
        try:
            vendor_rework.reject_and_rework(s, leg, note="n", actor="tester", transport=real)
            raise AssertionError(
                "a native rework of mock content to a real endpoint must be "
                "refused (MockEgressBlocked)"
            )
        except vendor_rework.ReworkStateError as exc:
            assert "refus" in str(exc).lower() or "mock" in str(exc).lower(), str(exc)
        # the refusal rolled the leg back to RETURNED (retryable), nothing half-done.
        assert s.get(VendorDispatch, leg_n).status == VendorDispatchStatus.RETURNED
    print(
        "  [F3] egress posture: a manual rework is NOT refused (skip load-bearing) while a "
        "native rework in the same real-egress posture IS refused: OK"
    )


# ============================================================================
# F4: the egress skip is load-bearing at FIRST dispatch too -- manual + a real-classified
#     transport + mock content dispatches; the mirrored native vendor is refused.
# ============================================================================
def test_manual_first_dispatch_skips_egress_with_real_transport_native_refused():
    real_manual = httpx.HTTPTransport()  # egress_is_real == True, but manual never sends
    # (A) manual: the egress perimeter is SKIPPED (wireless) -> leg DISPATCHED, nothing sent.
    env_m = _seed(protocol="manual", endpoint="")
    run_m, leg_m, _claim = _dispatch(env_m, transport=real_manual)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_m)
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED, (
            "a manual dispatch with a real-classified transport + mock content must SUCCEED "
            "(the `if not _is_manual:` egress skip is load-bearing)",
            leg and leg.status,
        )
        assert leg.endpoint_host == ""
        assert leg.protocol == "manual"
        assert s.get(WorkflowRun, run_m).status == RunStatus.PAUSED

    # (B) native mirror in the SAME posture: the egress perimeter FIRES -> the step FAILS, no leg.
    env_n = _seed(protocol="native", endpoint="https://vendor.example.com/intake", signing="s")
    run_n, leg_n, _c = _dispatch(env_n, transport=httpx.HTTPTransport())
    with Session(engine) as s:
        assert leg_n is None, "a native dispatch of mock content to a real endpoint must be refused"
        assert s.get(WorkflowRun, run_n).status == RunStatus.FAILED, (
            "the mock-egress block FAILS the external step for a native vendor"
        )
    print(
        "  [F4] first dispatch: manual skips the egress perimeter (DISPATCHED) while the "
        "mirrored native vendor is refused (MockEgressBlocked -> FAILED): OK"
    )


# ============================================================================
# F9: a manual leg round-trips a FILLED XLIFF (.xlf) back through admit_manual (test [D]
#     only covered a JSON return).
# ============================================================================
def _fill_xliff_targets(xliff_bytes, id_to_target):
    """Fill each trans-unit's DIRECT <target> with the operator/vendor translation (literal
    text, contract v1), the way a CAT tool would after opening the downloaded .xlf."""
    ns = f"{{{XLIFF_NS}}}"
    root = etree.fromstring(xliff_bytes)
    for tu in root.iter(f"{ns}trans-unit"):
        # skip a trans-unit nested in an <alt-trans> (a candidate, not the delivered unit).
        if any(etree.QName(a).localname == "alt-trans" for a in tu.iterancestors()):
            continue
        tgt = tu.find(f"{ns}target")
        if tgt is None:
            tgt = etree.SubElement(tu, f"{ns}target")
        for child in list(tgt):
            tgt.remove(child)
        tgt.text = id_to_target.get(tu.get("id"), "")
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def test_manual_leg_xliff_return_round_trip():
    env = _seed(protocol="manual", endpoint="")
    run_id, leg_id, claim = _dispatch(env, transport=_raising_transport())
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        xliff = vendor_xliff.render_leg_xliff(s, leg)  # the operator DOWNLOAD (no claim note)
    filled = _fill_xliff_targets(xliff, {str(i): t for i, t in zip(ids, GOOD, strict=False)})
    assert vendor_xliff._looks_like_xliff(filled), "the filled return must be an XLIFF body"
    # Admit the .xlf through the guarded manual upload, binding it to the leg by claim token
    # (the download blanks the token, so the operator's --claim/arg supplies it -- the manual
    # arg-selection convenience). Every gate (id accounting + guard lanes) runs identically.
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, filled, claim_token=claim, resume="sync", actor="tester"
        )
    assert out["complete"] is True and out["resumed"] is True, out
    assert len(out["admitted"]) == 3, out
    for (txt, _st), want in zip(_targets(env["doc_id"]), GOOD, strict=False):
        assert txt == want, ("the XLIFF-returned target must land", txt, want)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
        assert vendor_return.outstanding_ids(leg) == []
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED, (
            "the XLIFF return must resume the run to completion"
        )
    print(
        "  [I] a manual leg round-trips a filled .xlf back through admit_manual: targets "
        "land, leg RETURNED, run resumed: OK"
    )


# ============================================================================
# FROZEN-PACKAGE (AUDIT-omegat-roundtrip-2026-08-10): a wire-less (manual) leg freezes
# the DISPATCH-TIME contract so the operator download renders it verbatim, instead of
# rebuilding from the still-EMPTY Segment rows mid-run.
# ============================================================================
def _direct_manual_dispatch(env, *, targets, glossary=None, style_guide="", transport=None):
    """Dispatch the manual external step DIRECTLY with hand-built segment dicts whose targets
    are the dispatch-time MT DRAFTS, while the DB Segment rows stay EMPTY. This reproduces the
    mid-run reality the audit found: a run's MT output lives in the run's ROLLING TEXT and is
    NOT committed to Segment rows while the run is parked at the external step, so the in-flight
    dicts carry the drafts the rows do not. Returns (run_id, leg_id, claim)."""
    with Session(engine) as s:
        step = s.exec(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == env["wf_id"],
                WorkflowStep.step_type == StepType.EXTERNAL,
            )
        ).first()
        run = WorkflowRun(
            workflow_id=env["wf_id"],
            document_id=env["doc_id"],
            target_locale="es-ES",
            status=RunStatus.RUNNING,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        rows = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        segments = [
            {"id": r.id, "source": r.source_text or "", "target": t}
            for r, t in zip(rows, targets, strict=False)
        ]
        proj = s.get(Project, env["project_id"])
        leg = vendor_dispatch.dispatch_external_step(
            s,
            run=run,
            step=step,
            document_id=env["doc_id"],
            segments=segments,
            source_locale="en-US",
            target_locale="es-ES",
            customer_id=proj.customer_id,
            project=proj,
            glossary=glossary,
            style_guide=style_guide,
            transport=transport,
        )
        return run.id, leg.id, leg.claim_token


def _rendered_targets(xliff_bytes):
    """{id -> rendered <target> text} for the DIRECT trans-unit targets of an XLIFF."""
    ns = f"{{{XLIFF_NS}}}"
    root = etree.fromstring(xliff_bytes)
    out = {}
    for tu in root.iter(f"{ns}trans-unit"):
        if any(etree.QName(a).localname == "alt-trans" for a in tu.iterancestors()):
            continue
        tgt = tu.find(f"{ns}target")
        out[tu.get("id")] = (tgt.text or "") if tgt is not None else ""
    return out


def test_manual_leg_renders_dispatch_time_targets_not_empty_db_rows():
    # THE reproduced OmegaT round-trip bug: dispatch a manual leg with segment dicts whose
    # targets are the MT drafts, while the DB rows are still empty. Before the fix the download
    # rebuilt from those empty rows and emitted <target state="needs-translation"></target> for
    # every unit; with the freeze the download renders the DISPATCH-TIME targets verbatim.
    env = _seed(protocol="manual", endpoint="")
    _run_id, leg_id, claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    # precondition: the DB Segment rows carry NO target (the MT draft is only in rolling text).
    assert all(txt == "" for txt, _st in _targets(env["doc_id"])), (
        "precondition failed: the DB rows must be empty mid-run",
        _targets(env["doc_id"]),
    )
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, leg.status
        rc = leg.retry_contract
        assert isinstance(rc, dict) and rc.get("segments"), (
            "a manual leg must FREEZE the dispatch-time package",
            rc,
        )
        assert [sg.get("target") for sg in rc["segments"]] == GOOD, (
            "the frozen package must carry the dispatch-time targets",
            rc["segments"],
        )
        xliff = vendor_xliff.render_leg_xliff(s, leg)
    rendered = _rendered_targets(xliff)
    got = [rendered[str(i)] for i in ids]
    assert got == GOOD, (
        "render_leg_xliff must return the DISPATCH-TIME targets, not the empty DB rows",
        got,
    )
    assert any(t for t in got), "at least one full target string must round-trip (not empty)"
    # the download must NEVER contain the claim token (R1 blanking).
    assert claim and claim not in xliff.decode("utf-8"), (
        "the downloaded rendition must never contain the claim token"
    )
    print(
        "  [J] a manual leg renders the DISPATCH-TIME targets (frozen package), not the "
        "still-empty DB rows; claim token never in the download: OK"
    )


def test_frozen_package_helper_strips_only_xliff():
    # Unit: _frozen_package strips ONLY assets.xliff (the download re-renders XLIFF), keeps
    # every other asset + the real claim token, and does NOT mutate the source contract.
    contract = {
        "segments": [{"id": 1, "target": "x"}],
        "claim_token": "TOK",
        "assets": {
            "xliff": "<xliff/>",
            "glossary": [{"term": "a"}],
            "style_guide": "s",
            "context": "c",
        },
    }
    frozen = vendor_dispatch._frozen_package(contract)
    assert "xliff" not in frozen["assets"], "assets.xliff must be stripped from the frozen copy"
    assert (
        frozen["assets"]["glossary"]
        and frozen["assets"]["style_guide"]
        and frozen["assets"]["context"]
    ), "every other asset must survive the freeze"
    assert frozen["claim_token"] == "", (
        "F3: the frozen copy BLANKS the claim token -- nothing needs the stored token on a "
        "wire-less leg (contract_for_leg blanks it on download anyway, and retry_leg, the only "
        "re-signing consumer, never sees a _frozen_package output)"
    )
    assert contract["claim_token"] == "TOK", (
        "the SOURCE contract's claim token must NOT be mutated (shallow-copy hygiene)"
    )
    assert "xliff" in contract["assets"], (
        "the source contract must NOT be mutated (shallow-copy hygiene)"
    )
    print(
        "  [K0] _frozen_package strips ONLY assets.xliff, keeps other assets, BLANKS the "
        "claim token (F3), does not mutate the source: OK"
    )


def test_manual_frozen_package_strips_xliff_but_keeps_header_notes():
    # include_xliff ON -> build_contract embeds a full assets.xliff STRING; the frozen copy
    # strips it (size hygiene) but keeps glossary/style, and the rendered header still carries
    # the glossary + style-guide notes.
    env = _seed(protocol="manual", endpoint="", include_xliff=True)
    with Session(engine) as s:
        assert vendors.include_xliff(s.get(Vendor, env["vendor_id"])) is True, (
            "precondition: the vendor must have include_xliff on"
        )
    gloss = [{"term": "world", "target_term": "mundo"}]
    _run_id, leg_id, _claim = _direct_manual_dispatch(
        env,
        targets=GOOD,
        glossary=gloss,
        style_guide="Keep it formal.",
        transport=_raising_transport(),
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        rc = leg.retry_contract
        assert isinstance(rc, dict), rc
        assert "xliff" not in (rc.get("assets") or {}), (
            "the frozen copy must strip the duplicated assets.xliff",
            list((rc.get("assets") or {}).keys()),
        )
        assert (rc.get("assets") or {}).get("glossary"), "the glossary asset must survive"
        assert (rc.get("assets") or {}).get("style_guide"), "the style-guide asset must survive"
        xliff = vendor_xliff.render_leg_xliff(s, leg)
    text = xliff.decode("utf-8")
    assert "mundo" in text and "world" in text, "the glossary note must render in the file header"
    assert "Keep it formal." in text, "the style-guide note must render in the file header"
    print(
        "  [K] the frozen manual package strips assets.xliff but keeps glossary/style notes "
        "in the rendered header: OK"
    )


def test_native_delivered_leg_stores_no_frozen_package():
    # The byte-identity control: a NATIVE delivered leg must store NO frozen package (its signed
    # wire JSON stays authoritative; the download remains best-effort rebuild).
    env = _seed(protocol="native", endpoint="https://vendor.example.com/intake", signing="s")
    _run_id, leg_id, _claim = _dispatch(env, transport=_ok_transport(200))
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, leg.status
        assert leg.retry_contract is None, (
            "a NATIVE delivered leg must store NO frozen package (retry_contract NULL)",
            leg.retry_contract,
        )
    print(
        "  [L] a native delivered leg stores no frozen package (retry_contract NULL, "
        "byte-identical control): OK"
    )


def test_manual_rework_leg_freezes_package_too():
    # A manual REWORK re-dispatch is an operator-delivered package exactly like a first manual
    # dispatch, so it must freeze too (with the rework marker preserved).
    env = _seed(protocol="manual", endpoint="")
    _run_id, orig_leg_id = _make_returned_leg(env, transport=_raising_transport())
    with Session(engine) as s:
        leg = s.get(VendorDispatch, orig_leg_id)
        out = vendor_rework.reject_and_rework(
            s, leg, note="please fix tone", actor="tester", transport=_raising_transport()
        )
    assert out["reworked"] is True, out
    with Session(engine) as s:
        new_leg = s.get(VendorDispatch, out["new_dispatch_id"])
        assert new_leg.protocol == "manual", new_leg.protocol
        rc = new_leg.retry_contract
        assert isinstance(rc, dict) and rc.get("segments"), (
            "a manual rework leg must freeze its package too",
            rc,
        )
        assert rc.get("rework"), "the frozen rework package must carry the rework marker"
        assert "xliff" not in (rc.get("assets") or {}), "assets.xliff stripped on the rework too"
        xliff = vendor_xliff.render_leg_xliff(s, new_leg)
        assert new_leg.claim_token and new_leg.claim_token not in xliff.decode("utf-8"), (
            "the rework download must never leak the claim token"
        )
    print(
        "  [M] a manual rework leg freezes its package too (rework marker preserved, claim "
        "token blanked on download): OK"
    )


def test_manual_dispatched_leg_with_frozen_package_is_inert_to_retry_and_deadletter():
    # Option (a) inertness proof: a DISPATCHED manual leg carrying a frozen package in
    # retry_contract must be invisible to the retry scheduler (due_retry_legs / retry_leg both
    # key on status == RETRY_WAIT) and can never reach the dead-letter path (only retry_leg
    # calls it). The frozen package survives a forced sweep untouched.
    env = _seed(protocol="manual", endpoint="")
    _run_id, leg_id, _claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED and leg.retry_contract, (
            "precondition: a DISPATCHED manual leg carrying a frozen package"
        )
        # the retry sweep's selection EXCLUDES it (status != RETRY_WAIT AND next_attempt_at NULL).
        assert leg_id not in [d.id for d in vendor_dispatch.due_retry_legs(s)], (
            "a DISPATCHED manual leg must be invisible to the retry sweep"
        )
        # a FORCED retry_leg call is a no-op on a non-RETRY_WAIT leg (never reaches dead_letter).
        outcome = vendor_dispatch.retry_leg(s, leg, transport=_raising_transport())
        assert outcome == "noop", ("retry_leg must no-op on a DISPATCHED leg", outcome)
    with Session(engine) as s:
        leg2 = s.get(VendorDispatch, leg_id)
        assert leg2.status == VendorDispatchStatus.DISPATCHED and leg2.retry_contract, (
            "the frozen package must survive untouched -- no retry, no dead-letter"
        )
    print(
        "  [N] a DISPATCHED manual leg carrying a frozen package is inert to the retry sweep "
        "(due_retry_legs excludes it, retry_leg no-ops, dead-letter unreachable): OK"
    )


# ============================================================================
# FROZEN-PACKAGE LIFECYCLE (fix round, AUDIT-omegat-roundtrip-2026-08-10 F4 review):
# ONE status-keyed rule, single-sourced in vendor_dispatch._clear_or_keep_frozen. A wire-less
# leg's operator-delivery package is KEPT while WORKABLE (DISPATCHED/QUOTED/ESCALATED) and
# CLEARED on a no-pending-delivery state (RETURNED/REJECTED/CANCELLED/DEAD_LETTER + on
# retention.delete_document). A native leg is never wire-less, so it clears on every exit.
# ============================================================================
def test_frozen_package_kept_on_escalate_and_still_downloads_dispatch_time_targets():
    # A wire-less leg that EXPIRES and escalates to a human must KEEP its frozen package: the
    # operator must still be able to download the correct dispatch-time file (the review's core
    # regression -- escalation's old blanket clear silently regressed the post-expiry download
    # to the empty rebuild).
    env = _seed(protocol="manual", endpoint="")
    _run_id, leg_id, _claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED and leg.retry_contract, (
            "precondition: a DISPATCHED wire-less leg carrying a frozen package"
        )
        # the DB rows are still EMPTY (mid-run) -- only the frozen package has the targets.
        assert all(t == "" for t, _ in _targets(env["doc_id"]))
        escalated = vendor_dispatch.escalate_leg(s, leg, actor="system")
        assert escalated is True, "an open wire-less leg must escalate on expiry"
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.ESCALATED, leg.status
        assert leg.retry_contract and leg.retry_contract.get("segments"), (
            "a wire-less leg KEEPS its frozen package through ESCALATED (still workable)"
        )
        xliff = vendor_xliff.render_leg_xliff(s, leg)
    rendered = _rendered_targets(xliff)
    assert [rendered[str(i)] for i in ids] == GOOD, (
        "the escalated wire-less leg must still download the DISPATCH-TIME targets, not the "
        "empty rebuild"
    )
    print(
        "  [P] a wire-less leg KEEPS its frozen package on escalate and still downloads the "
        "dispatch-time targets: OK"
    )


def test_frozen_package_cleared_on_complete_return_kept_on_partial():
    # KEEP on a PARTIAL return (leg stays DISPATCHED -- more delivery pending); CLEAR on the
    # COMPLETING return (RETURNED -- no delivery pending, and post-return the Segment rows carry
    # the vendor text, which becomes the correct rendition).
    env = _seed(protocol="manual", endpoint="")
    _run_id, leg_id, claim = _dispatch(env, transport=_raising_transport())
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED and leg.retry_contract, (
            "precondition: a DISPATCHED wire-less leg carrying a frozen package"
        )
    # PARTIAL return: only the first id. The leg stays DISPATCHED, package KEPT.
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids[:1], GOOD[:1], claim=claim), resume="sync", actor="tester"
        )
    assert out["complete"] is False, ("a partial return must not complete the leg", out)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, leg.status
        assert leg.retry_contract and leg.retry_contract.get("segments"), (
            "a PARTIAL return KEEPS the frozen package (the leg is still workable)"
        )
    # COMPLETING return: the rest. The leg flips RETURNED and the package is CLEARED.
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _payload(ids[1:], GOOD[1:], claim=claim), resume="sync", actor="tester"
        )
    assert out["complete"] is True, out
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED, leg.status
        assert leg.retry_contract is None, (
            "a COMPLETING return has no pending delivery -> the frozen package is cleared"
        )
        # post-return the rebuild path renders the returned text (F7's terminal fall-through).
        xliff = vendor_xliff.render_leg_xliff(s, leg)
    rendered = _rendered_targets(xliff)
    assert [rendered[str(i)] for i in ids] == GOOD, (
        "a RETURNED wire-less leg (package correctly cleared) falls back to the rebuild, which "
        "now renders the vendor's returned Segment text"
    )
    print(
        "  [Q] frozen package KEPT on a partial return, CLEARED on the completing return; a "
        "returned leg falls back to the rebuild of the vendor text: OK"
    )


def test_frozen_package_cleared_on_cancel_via_close_open_legs():
    # CANCELLED (no pending delivery) clears the wire-less package. Exercised through
    # close_open_legs_for_run, the seam a terminal run funnels through.
    env = _seed(protocol="manual", endpoint="")
    run_id, leg_id, _claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED and leg.retry_contract, (
            "precondition: a DISPATCHED wire-less leg carrying a frozen package"
        )
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.COMPLETED  # the run left the external step / finished
        s.add(run)
        s.commit()
        n = vendor_dispatch.close_open_legs_for_run(s, run_id)
        assert n == 1, ("the open leg must be closed", n)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.CANCELLED, leg.status
        assert leg.retry_contract is None, (
            "CANCELLED has no pending delivery -> the frozen package is cleared"
        )
    print(
        "  [R] a wire-less leg's frozen package is CLEARED on CANCELLED "
        "(close_open_legs_for_run): OK"
    )


def test_decline_quote_keeps_wireless_package():
    # A QUOTED wire-less leg that is DECLINED goes ESCALATED (a human owns the parked run):
    # ESCALATED is still workable, so the operator-delivery package is KEPT (consistent with
    # escalate_leg). A native leg holds none.
    env = _seed(protocol="manual", endpoint="", quote_gated=True)
    _run_id, leg_id, _claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.QUOTED and leg.retry_contract, (
            "precondition: a QUOTED wire-less leg carrying a frozen package",
            leg.status,
        )
        out = vendor_quote.decline_quote(s, leg, reason="too pricey", actor="tester")
        assert out["escalated"] is True, out
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.ESCALATED, leg.status
        assert leg.retry_contract and leg.retry_contract.get("segments"), (
            "a declined-quote (ESCALATED) wire-less leg KEEPS its frozen package"
        )
        xliff = vendor_xliff.render_leg_xliff(s, leg)
    rendered = _rendered_targets(xliff)
    assert [rendered[str(i)] for i in ids] == GOOD, (
        "the escalated leg must still download the dispatch-time targets"
    )
    print("  [S] decline_quote (-> ESCALATED) KEEPS the wire-less frozen package: OK")


def test_approve_quote_on_terminal_run_cancels_and_clears_wireless_package():
    # F2 mirror of [S] for approve_quote's run-terminal guard: approving a QUOTED wire-less leg
    # whose run has moved on (terminal) must CLOSE the leg CANCELLED and refuse -- and CANCELLED
    # has no pending delivery, so the frozen operator-delivery package is CLEARED (routed through
    # the single-sourced vendor_dispatch._clear_or_keep_frozen, like decline_quote's guard).
    env = _seed(protocol="manual", endpoint="", quote_gated=True)
    run_id, leg_id, _claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.QUOTED and leg.retry_contract, (
            "precondition: a QUOTED wire-less leg carrying a frozen package",
            leg.status,
        )
        run = s.get(WorkflowRun, run_id)
        run.status = RunStatus.CANCELLED  # the run moved on before the quote was approved
        s.add(run)
        s.commit()
        try:
            vendor_quote.approve_quote(s, leg, quoted_cost_usd=100.0, actor="tester")
            raise AssertionError("approving a quote on a terminal run must refuse")
        except vendor_quote.QuoteStateError:
            pass
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.CANCELLED, leg.status
        assert leg.retry_contract is None, (
            "CANCELLED has no pending delivery -> the frozen package is cleared"
        )
    print(
        "  [X] approve_quote on a terminal run -> CANCELLED -> the wire-less frozen package "
        "is cleared: OK"
    )


def test_retention_clears_frozen_package_on_cross_run_leg():
    # F2 data governance. retention.delete_document clears a leg's retry_contract (the frozen
    # CONTENT) and NULLs the dangling VendorDispatch.document_id FK, so a wire-less frozen
    # package does not outlive its document. This test pins that CLEAR on the CROSS-RUN shape:
    # the leg's RUN points at a SEPARATE document, so the run (and therefore the leg row)
    # survives the delete and the reconciliation LEDGER row is what we get to inspect.
    #
    # This shape used to be the only one that could be tested at all -- a document with its own
    # run could not be deleted, full stop (WorkflowRun.document_id is a NON-NULL FK and
    # delete_document did not delete run rows, so the DELETE raised IntegrityError and rolled
    # the whole transaction, clear included, back). That FK bug is FIXED (2026-08-31), and the
    # ORDINARY shape -- the run pointing at the document being deleted, where the leg row itself
    # goes with its run -- is pinned in test_retention_delete_document_erases_leg_on_real_shape
    # below. Both shapes are real; this one just keeps the ledger row.
    from app.services import retention

    env = _seed(protocol="manual", endpoint="")
    with Session(engine) as s:
        other = Document(
            project_id=env["project_id"], filename="other.txt", mode=TransMode.SEGMENTED
        )
        s.add(other)
        s.commit()
        s.refresh(other)
        run = WorkflowRun(
            workflow_id=env["wf_id"],
            document_id=other.id,
            target_locale="es-ES",
            status=RunStatus.PAUSED,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        leg = VendorDispatch(
            vendor_id=env["vendor_id"],
            workflow_run_id=run.id,
            document_id=env["doc_id"],
            target_locale="es-ES",
            status=VendorDispatchStatus.DISPATCHED,
            protocol="manual",
            claim_token="tok",
            contract_ids={"dispatched": ["1"], "returned": []},
            retry_contract={"segments": [{"id": 1, "target": "Hola"}], "claim_token": ""},
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        leg_id = leg.id
    with Session(engine) as s:
        counts = retention.delete_document(s, env["doc_id"], cascade_tm=True)
    assert counts.get("documents") == 1, counts
    assert counts.get("dispatch_packages_cleared") == 1, (
        "the delete must report clearing the leg's frozen package",
        counts,
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg is not None, "the VendorDispatch ledger ROW must survive (audit trail)"
        assert leg.retry_contract is None, (
            "the frozen CONTENT must be cleared -- it must not outlive its deleted document"
        )
        assert leg.document_id is None, (
            "the dangling document_id FK must be severed (the document is gone)"
        )
        assert leg.contract_ids is not None, (
            "the id ledger (counts/ids, not content) is retained on the surviving row"
        )
        assert s.get(Document, env["doc_id"]) is None, "the target document is deleted"
    print(
        "  [T] retention.delete_document on the CROSS-RUN shape (leg's run points elsewhere): "
        "clears the frozen package + severs the FK, keeps the ledger row: OK"
    )


def test_retention_delete_document_erases_leg_on_real_shape():
    # THE ORDINARY SHAPE, and the one that matters: a leg whose WorkflowRun points at the SAME
    # document being deleted (every real dispatch looks like this). This used to RAISE
    # IntegrityError -- WorkflowRun.document_id is a NON-NULL FK with no cascade and
    # delete_document did not delete run rows -- so no document that had ever been translated
    # could be deleted or aged out, and the frozen-package clear rolled back with everything
    # else. FIXED 2026-08-31: delete_document reaps the document's run history (StepRun ->
    # WorkflowRun), and a leg anchored to one of those runs CANNOT survive it (workflow_run_id
    # is NOT NULL), so the ledger row goes with its run and the frozen CONTENT with it.
    from app.services import retention

    env = _seed(protocol="manual", endpoint="")
    with Session(engine) as s:
        # REAL shape: the run points at the document being deleted (as every real dispatch does).
        run = WorkflowRun(
            workflow_id=env["wf_id"],
            document_id=env["doc_id"],
            target_locale="es-ES",
            status=RunStatus.PAUSED,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        leg = VendorDispatch(
            vendor_id=env["vendor_id"],
            workflow_run_id=run.id,
            document_id=env["doc_id"],
            target_locale="es-ES",
            status=VendorDispatchStatus.DISPATCHED,
            protocol="manual",
            claim_token="tok",
            contract_ids={"dispatched": ["1"], "returned": []},
            retry_contract={"segments": [{"id": 1, "target": "Hola"}], "claim_token": ""},
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        leg_id, run_id = leg.id, run.id
    with Session(engine) as s:
        counts = retention.delete_document(s, env["doc_id"], cascade_tm=True)
    assert counts.get("documents") == 1, counts
    assert counts.get("workflow_runs") == 1, ("the document's run must be reaped with it", counts)
    assert counts.get("dispatch_legs_deleted") == 1, (
        "the run-anchored leg cannot outlive its run (workflow_run_id NOT NULL)",
        counts,
    )
    with Session(engine) as s:
        assert s.get(Document, env["doc_id"]) is None, "the document must be deleted"
        assert s.get(WorkflowRun, run_id) is None, "the run must be deleted with its document"
        assert s.get(VendorDispatch, leg_id) is None, (
            "a leg anchored to the deleted run must be gone -- with the frozen CONTENT it held"
        )
    print(
        "  [T2] retention.delete_document on the REAL shape (run -> same doc): document + run "
        "+ the run-anchored leg (and its frozen package) all erased, no IntegrityError: OK"
    )


def test_wireless_workable_leg_without_package_refuses_render():
    # F7: a wire-less leg in a WORKABLE state (DISPATCHED/QUOTED/ESCALATED) with NO frozen
    # package (it predates the freeze, or its package was cleared abnormally) must REFUSE with
    # XliffRenderError -- NOT silently rebuild an all-empty delivery from the mid-run rows.
    env = _seed(protocol="manual", endpoint="")
    _run_id, leg_id, _claim = _direct_manual_dispatch(
        env, targets=GOOD, transport=_raising_transport()
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        # simulate a legacy / abnormally-cleared package while the leg stays DISPATCHED.
        leg.retry_contract = None
        s.add(leg)
        s.commit()
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_xliff.render_leg_xliff(s, leg)
            raise AssertionError("a package-less workable wire-less leg must refuse to render")
        except vendor_xliff.XliffRenderError as exc:
            assert "wire-less" in str(exc) and "re-dispatch" in str(exc), str(exc)
    print(
        "  [U] a package-less WORKABLE wire-less leg refuses to render (honest "
        "XliffRenderError, no silent empty delivery): OK"
    )


def test_native_retry_wait_package_cleared_on_successful_retry_unchanged():
    # Byte-identity pin for the NATIVE RETRY_WAIT re-send use of the column: it is cleared on a
    # SUCCESSFUL retry (DISPATCHED restore) exactly as before -- a native leg is never wire-less,
    # so the single-sourced helper clears it unconditionally.
    env = _seed(protocol="native", endpoint="https://vendor.example.com/intake", signing="s")
    _run_id, leg_id, _claim = _dispatch(env, transport=_ok_transport(503))  # parks RETRY_WAIT
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT and leg.retry_contract, (
            "precondition: a native leg parked RETRY_WAIT carrying its re-send package"
        )
        outcome = vendor_dispatch.retry_leg(s, leg, transport=_ok_transport(200))
        assert outcome == "dispatched", ("a clean retry must deliver", outcome)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, leg.status
        assert leg.retry_contract is None, (
            "the native RETRY_WAIT re-send package is cleared on the successful retry "
            "(unchanged, byte-identical)"
        )
    print(
        "  [V] the native RETRY_WAIT re-send package clears on a successful retry "
        "(byte-identical): OK"
    )


if __name__ == "__main__":
    test_manual_dispatch_lands_dispatched_with_no_network_io()
    test_quote_gated_manual_leg_parks_quoted()
    test_render_leg_xliff_for_a_manual_leg()
    test_manual_leg_admits_a_manual_return_and_resumes()
    test_manual_legs_are_never_polled()
    test_manual_leg_stays_unpollable_after_vendor_flipped_to_native()
    test_manual_is_a_registered_protocol()
    test_service_create_update_accepts_manual_and_refuses_unknown()
    test_manual_vendor_may_carry_endpoint_but_it_is_ignored()
    test_native_dispatch_is_unperturbed()
    # F2/F1/F3/F4/F9 fix-round scenarios (protocol-snapshot on the leg).
    test_native_retry_leg_does_not_phantom_deliver_after_flip_to_manual()
    test_legacy_null_snapshot_native_leg_dead_letters_on_flip_to_manual()
    test_rework_of_manual_leg_follows_original_snapshot_no_io()
    test_rework_egress_posture_manual_not_refused_native_refused()
    test_manual_first_dispatch_skips_egress_with_real_transport_native_refused()
    test_manual_leg_xliff_return_round_trip()
    # Frozen-package (AUDIT-omegat-roundtrip-2026-08-10): a wire-less leg renders its
    # dispatch-time targets, not the still-empty DB rows.
    test_manual_leg_renders_dispatch_time_targets_not_empty_db_rows()
    test_frozen_package_helper_strips_only_xliff()
    test_manual_frozen_package_strips_xliff_but_keeps_header_notes()
    test_native_delivered_leg_stores_no_frozen_package()
    test_manual_rework_leg_freezes_package_too()
    test_manual_dispatched_leg_with_frozen_package_is_inert_to_retry_and_deadletter()
    # Frozen-package LIFECYCLE (fix round): the single status-keyed rule across every transition.
    test_frozen_package_kept_on_escalate_and_still_downloads_dispatch_time_targets()
    test_frozen_package_cleared_on_complete_return_kept_on_partial()
    test_frozen_package_cleared_on_cancel_via_close_open_legs()
    test_decline_quote_keeps_wireless_package()
    test_approve_quote_on_terminal_run_cancels_and_clears_wireless_package()
    test_retention_clears_frozen_package_on_cross_run_leg()
    test_retention_delete_document_erases_leg_on_real_shape()
    test_wireless_workable_leg_without_package_refuses_render()
    test_native_retry_wait_package_cleared_on_successful_retry_unchanged()
    # Run LAST: this one flips security_auth_required ON to exercise the admin-gated API route.
    test_cli_and_api_accept_manual_and_refuse_unknown()
    print("verify_vendor_manual_protocol OK")
