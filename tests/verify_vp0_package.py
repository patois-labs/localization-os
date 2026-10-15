"""VP-0 dispatch-package enrichment + the Vendor Studio SSO mint (PROPOSAL-vendor-platform
§3; the VP-0 / SSO train). Hermetic + offline: every dispatch rides an injected
httpx.MockTransport (vendor_dispatch.set_test_transport) so no byte reaches a real network.

Field names are pinned to the WIRE CONTRACT the Vendor Studio companion app reads
(tools/vendor_studio/vstudio/contract.py): per-segment `provenance` / `match_score` /
`locked` / `locked_reason`, the package-level `assets.glossary`, and the SSO token shape
`b64url(payload).hex(hmac)`. The SSO verification here is re-implemented FROM THE README
SPEC (never importing vstudio), so the two sides are proven to agree by construction.

  [A] enrichment: provenance/match_score/locked/locked_reason correct per TM band class
  [B] omit_locked_targets: a locked segment ships source + marker but an EMPTY target
  [C] assets.glossary: normalized shape + the cap + the truncation note
  [D] locked round-trip is a byte-identical no-op (an unchanged echo never clobbers)
  [E] retry_contract carries the enriched fields (the frozen package re-sends them)
  [F] SSO mint: token shape + HMAC verifiable per the README; admin-only + unconfigured-404;
      the "Open Vendor Studio" button renders only when configured
"""

import base64
import hashlib
import hmac
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_vp0_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
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
    settings,
    vendor_dispatch,
    vendor_return,
    vendor_studio_sso,
    vendors,
)
from app.services import workflows as wf

init_db()

_SEQ = [0]
SIGNING = "vp0-signing-secret"


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _run_step():
    """Minimal run/step stand-ins build_contract reads (run.id, step.key, step.name)."""
    return (SimpleNamespace(id=_seq()), SimpleNamespace(name="Vendor review", key="ext"))


# ============================================================================
# [A] per-segment enrichment, correct per band class
# ============================================================================
def test_enrichment_fields_per_band():
    run, step = _run_step()
    segs = [
        {"id": 1, "source": "Alpha one.", "target": "T-alpha", "tm_match_pct": 100},
        {"id": 2, "source": "Beta two.", "target": "T-beta", "tm_match_pct": 88},
        {"id": 3, "source": "Gamma three.", "target": "T-gamma", "tm_match_pct": 97},
        {"id": 4, "source": "Delta four.", "target": "T-delta"},
        {"id": 5, "source": "Echo five.", "target": ""},
        {"id": 6, "source": "Delta four.", "target": "T-delta-again"},  # repetition of #4
        {"id": 7, "source": "Ctx seven.", "target": "T-ctx", "lock_band": "context"},
    ]
    c = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=segs,
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="tok",
    )
    by = {s["id"]: s for s in c["segments"]}

    # exact (pct=100) -> tm_exact, locked "100% TM match", NO match_score
    assert by[1]["provenance"] == "tm_exact"
    assert by[1]["locked"] is True and by[1]["locked_reason"] == "100% TM match"
    assert "match_score" not in by[1]
    # fuzzy tiers -> tm_fuzzy + match_score, NOT locked
    assert by[2]["provenance"] == "tm_fuzzy" and by[2]["match_score"] == 88
    assert "locked" not in by[2]
    assert by[3]["provenance"] == "tm_fuzzy" and by[3]["match_score"] == 97
    # new band, target present -> "mt"; empty target -> "new"; neither locked
    assert by[4]["provenance"] == "mt" and "locked" not in by[4] and "match_score" not in by[4]
    assert by[5]["provenance"] == "new"
    # repetition -> covered/locked "repetition", tm_exact
    assert by[6]["provenance"] == "tm_exact"
    assert by[6]["locked"] is True and by[6]["locked_reason"] == "repetition"
    # lock_band context -> covered/locked "context match", tm_exact
    assert by[7]["provenance"] == "tm_exact"
    assert by[7]["locked"] is True and by[7]["locked_reason"] == "context match"
    # additive: contract_version stays 1.0
    assert c["contract_version"] == "1.0"
    print("  [A] provenance/match_score/locked/locked_reason correct per band class: OK")


# ============================================================================
# [B] omit_locked_targets: locked segments ship no target
# ============================================================================
def test_omit_locked_targets():
    run, step = _run_step()
    segs = [
        {"id": 1, "source": "Locked src.", "target": "LOCKED-TGT", "tm_match_pct": 100},
        {"id": 2, "source": "Open src.", "target": "OPEN-TGT", "tm_match_pct": 80},
    ]
    vendor_off = SimpleNamespace(config=None)
    vendor_on = SimpleNamespace(config={"omit_locked_targets": True})

    c_off = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=segs,
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        vendor=vendor_off,
    )
    off = {s["id"]: s for s in c_off["segments"]}
    assert off[1]["target"] == "LOCKED-TGT", "default (flag off) ships the locked target"

    run2, step2 = _run_step()
    c_on = vendor_dispatch.build_contract(
        run=run2,
        step=step2,
        segments=segs,
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        vendor=vendor_on,
    )
    on = {s["id"]: s for s in c_on["segments"]}
    # the LOCKED segment keeps its source + marker but ships NO target
    assert on[1]["source"] == "Locked src." and on[1]["locked"] is True
    assert on[1]["target"] == "", "omit_locked_targets must blank a locked segment's target"
    # the non-locked (fuzzy) segment is untouched
    assert on[2]["target"] == "OPEN-TGT", "a non-locked target must never be omitted"
    # the vendors service helper agrees
    assert vendors.omit_locked_targets(vendor_on) is True
    assert vendors.omit_locked_targets(vendor_off) is False
    print("  [B] omit_locked_targets blanks locked targets only, source + marker kept: OK")


# ============================================================================
# [C] assets.glossary: shape + cap + note
# ============================================================================
def test_glossary_block_and_cap():
    run, step = _run_step()
    gloss = [
        {"source_term": "API", "target_term": "API", "dnt": True},
        {"source_term": "login", "target_term": "inicio de sesion"},
        {"source_term": "API", "target_term": "dup dropped"},  # dedup by term
    ]
    c = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=[{"id": 1, "source": "x", "target": "y"}],
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        glossary=gloss,
    )
    g = c["assets"]["glossary"]
    assert isinstance(g, list) and len(g) == 2, g
    assert g[0] == {"term": "API", "target_term": "API", "dnt": True}, g[0]
    assert g[1] == {"term": "login", "target_term": "inicio de sesion"}, g[1]
    assert "glossary_note" not in c["assets"], "no note when nothing was truncated"

    # the cap bites -> the list is bounded and a count note rides alongside
    orig_cap = vendor_dispatch.GLOSSARY_TERM_CAP
    set_pieces("app.services.vendor_dispatch", "GLOSSARY_TERM_CAP", 1)
    try:
        run2, step2 = _run_step()
        big = [{"source_term": f"t{i}", "target_term": f"v{i}"} for i in range(5)]
        c2 = vendor_dispatch.build_contract(
            run=run2,
            step=step2,
            segments=[{"id": 1, "source": "x", "target": "y"}],
            source_locale="en-US",
            target_locale="es-ES",
            claim_token="t",
            glossary=big,
        )
    finally:
        set_pieces("app.services.vendor_dispatch", "GLOSSARY_TERM_CAP", orig_cap)
    assert len(c2["assets"]["glossary"]) == 1, "glossary capped"
    note = c2["assets"].get("glossary_note", "")
    assert "1 of 5" in note, ("truncation note names the counts", note)
    # a pre-rendered string passes through verbatim
    run3, step3 = _run_step()
    c3 = vendor_dispatch.build_contract(
        run=run3,
        step=step3,
        segments=[{"id": 1, "source": "x", "target": "y"}],
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        glossary="term -> target (rendered)",
    )
    assert c3["assets"]["glossary"] == "term -> target (rendered)"
    print(
        "  [C] assets.glossary normalized {term,target_term,dnt}; capped + noted; "
        "string passthrough: OK"
    )


# ============================================================================
# dispatch fixtures (orchestrator, hermetic) for [D] + [E]
# ============================================================================
def _mock_transport(status=200, capture=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append({"body": req.content, "headers": dict(req.headers)})
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler)


def _seed(pct_first=None):
    """A customer + project + 3-seg doc + a vendor linked to it + a translate->external wf.
    `pct_first` seeds tm_match_pct on the first segment (to make it a covered-band lock)."""
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
        rows = [("The locked one.", pct_first), ("Second string.", None), ("Third string.", None)]
        for i, (src, pct) in enumerate(rows):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                    tm_match_pct=pct,
                )
            )
        s.commit()
        v = Vendor(
            name=f"V-{_seq()}",
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
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
        wf.add_step(
            s,
            w.id,
            name="Vendor review",
            step_type=StepType.EXTERNAL,
            vendor_id=v.id,
            external_wait_hours=72,
        )
        return {"doc_id": d.id, "wf_id": w.id, "vendor_id": v.id}


def _dispatch(env, capture=None, status=200):
    vendor_dispatch.set_test_transport(_mock_transport(status=status, capture=capture))
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        return run.id, (leg.id if leg else None), (leg.claim_token if leg else None)


# ============================================================================
# [D] locked round-trip is a byte-identical no-op
# ============================================================================
def test_locked_roundtrip_no_clobber():
    cap = []
    env = _seed(pct_first=100)
    _run_id, leg_id, claim = _dispatch(env, capture=cap)
    # The wire contract marks the pct-100 first segment locked (covered band).
    contract = json.loads(cap[-1]["body"])
    locked = [sg for sg in contract["segments"] if sg.get("locked")]
    assert locked, ("a pct-100 segment must ship locked=true", contract["segments"])
    assert locked[0]["locked_reason"] == "100% TM match"

    with Session(engine) as s:
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        ids = [sg.id for sg in segs]
        before = {sg.id: (sg.target_text or "") for sg in segs}
    # The vendor returns EVERY id echoing its current stored target UNCHANGED (as a locked
    # id returns as-dispatched). The complete return must leave every stored target byte-
    # identical -- an unchanged echo is a no-op write, never a clobber.
    payload = {
        "contract_version": "1.0",
        "target_locale": "es-ES",
        "claim_token": claim,
        "segments": [{"id": i, "target": before[i]} for i in ids],
    }
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, payload, resume="sync", actor="tester")
    assert out["complete"] is True, out
    with Session(engine) as s:
        after = {
            sg.id: (sg.target_text or "")
            for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")
        }
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
    for i in ids:
        assert after[i] == before[i], (
            "stored target must be byte-identical",
            i,
            before[i],
            after[i],
        )
    print(
        "  [D] locked segment ships locked=true; an unchanged round-trip is byte-"
        "identical (no clobber), leg RETURNED: OK"
    )


def _seg_ids(doc_id):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, "es-ES")]


def test_omit_locked_translation_survives():
    """VARIANT the reviewer probed: with omit_locked_targets ON, a locked segment translated
    IN-RUN (target not yet persisted at pause) ships blank, comes back blank -> the in-run
    translation must SURVIVE (the old code dropped it to needs_review + empty)."""
    env = _seed(pct_first=100)
    # turn omit_locked_targets ON for this vendor
    with Session(engine) as s:
        vendors.update(s, env["vendor_id"], config_json={"omit_locked_targets": True})
    cap = []
    _run_id, _leg_id, claim = _dispatch(env, capture=cap)
    contract = json.loads(cap[-1]["body"])
    locked = [sg for sg in contract["segments"] if sg.get("locked")]
    assert locked and locked[0]["target"] == "", (
        "locked seg must ship an EMPTY target",
        contract["segments"],
    )
    assert locked[0]["state"] == "needs-review-translation", "locked-omitted state (LOW-1)"
    ids = _seg_ids(env["doc_id"])
    locked_id = ids[0]
    # vendor returns: blank for the omitted locked id, edited targets for the other two.
    segs = [{"id": locked_id, "target": ""}]
    segs += [{"id": i, "target": f"Vendor edit {i}."} for i in ids[1:]]
    payload = {
        "contract_version": "1.0",
        "target_locale": "es-ES",
        "claim_token": claim,
        "segments": segs,
    }
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, payload, resume="sync", actor="tester")
    assert out["complete"] is True, out
    assert locked_id in [int(x) if str(x).isdigit() else x for x in out["omitted_noop"]] or str(
        locked_id
    ) in [str(x) for x in out["omitted_noop"]], out["omitted_noop"]
    with Session(engine) as s:
        seg = [  # noqa: RUF015
            sg
            for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")
            if sg.id == locked_id
        ][0]
    assert (seg.target_text or "").strip(), (
        "the in-run translation must SURVIVE a blank omitted return, not be dropped",
        seg.target_text,
    )
    assert seg.target_text.strip() != "The locked one.", "must not collapse to source"
    assert seg.status != SegStatus.NEEDS_REVIEW, (
        "a withheld locked target must NOT demote the segment to needs_review",
        seg.status,
    )
    print(
        "  [D2] omit_locked_targets: an in-run-translated locked segment survives a blank "
        "return (not dropped, not demoted): OK"
    )


def test_omit_locked_approved_status_preserved():
    """VARIANT the reviewer probed: a locked segment whose target is ALREADY PERSISTED +
    APPROVED (an ICE/approved leverage) ships blank, comes back blank -> its APPROVED status
    and stored target are left EXACTLY as they were (a re-commit would demote to MT)."""
    env = _seed(pct_first=100)
    with Session(engine) as s:
        vendors.update(s, env["vendor_id"], config_json={"omit_locked_targets": True})
    _run_id, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    locked_id = ids[0]
    # Simulate a PERSISTED, APPROVED leverage on the locked segment at pause time.
    with Session(engine) as s:
        seg = [  # noqa: RUF015
            sg
            for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")
            if sg.id == locked_id
        ][0]
        seg.target_text = "Aprobado ICE."
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    segs = [{"id": locked_id, "target": ""}]
    segs += [{"id": i, "target": f"Vendor edit {i}."} for i in ids[1:]]
    payload = {
        "contract_version": "1.0",
        "target_locale": "es-ES",
        "claim_token": claim,
        "segments": segs,
    }
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, payload, resume="sync", actor="tester")
    assert out["complete"] is True, out
    with Session(engine) as s:
        seg = [  # noqa: RUF015
            sg
            for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")
            if sg.id == locked_id
        ][0]
    assert seg.target_text == "Aprobado ICE.", (
        "the approved target must be byte-identical",
        seg.target_text,
    )
    assert seg.status == SegStatus.APPROVED, (
        "APPROVED must NOT be demoted by a withheld locked target's blank return",
        seg.status,
    )
    print(
        "  [D3] omit_locked_targets: a persisted APPROVED locked segment keeps its status "
        "+ target on a blank return: OK"
    )


# ============================================================================
# [E] retry_contract carries the enriched fields
# ============================================================================
def test_retry_contract_carries_enrichment():
    env = _seed(pct_first=100)
    # A 500 is a TRANSIENT failure -> the leg parks RETRY_WAIT holding the exact package.
    _run_id, leg_id, _ = _dispatch(env, status=500)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT, leg.status
        rc = leg.retry_contract or {}
        segs = rc.get("segments") or []
        assert segs, "the retry package must carry the frozen contract segments"
        # every frozen segment carries provenance; the pct-100 one is still locked.
        assert all("provenance" in sg for sg in segs), segs
        assert any(sg.get("locked") for sg in segs), ("locked enrichment survives the freeze", segs)
    print("  [E] retry_contract re-sends the enriched fields (provenance + locked): OK")


# ============================================================================
# [F] SSO mint + route + button
# ============================================================================
def _verify_token(token: str, secret: str) -> dict:
    """Verify + decode a handoff token per the Vendor Studio README, WITHOUT importing
    vstudio: token = b64url(payload) + '.' + hex(HMAC_SHA256(secret, b64url(payload)))."""
    body, dot, sig = token.partition(".")
    assert dot == ".", "token must be body.signature"
    expect = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig, expect), "HMAC does not verify against the shared secret"
    pad = "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))


def test_sso_mint_shape_and_hmac():
    tok = vendor_studio_sso.mint_token(
        "alice@example.com", role="manager", secret="shared-xyz", now=1_000_000, ttl=60
    )
    payload = _verify_token(tok, "shared-xyz")
    assert payload["user"] == "alice@example.com"
    assert payload["exp"] == 1_000_060, ("exp = now + ttl", payload)
    # #557: the signed claims Vendor Studio fails closed without.
    assert payload["role"] == "manager" and payload["aud"] == "vendor-studio", payload
    assert isinstance(payload["jti"], str) and len(payload["jti"]) >= 16, payload
    assert payload["org"] is None and payload["mo"] is False, payload
    again = _verify_token(
        vendor_studio_sso.mint_token("alice@example.com", role="manager", secret="shared-xyz"),
        "shared-xyz",
    )
    assert again["jti"] != payload["jti"], "every token needs its own jti (single use)"
    # a token minted under a DIFFERENT secret must not verify against ours.
    other = vendor_studio_sso.mint_token("x", role="admin", secret="another", now=1_000_000)
    try:
        _verify_token(other, "shared-xyz")
        raise AssertionError("a foreign-secret token must fail verification")
    except AssertionError as exc:
        assert "HMAC" in str(exc), exc
    # no secret -> refuse to mint (never an unsigned/blank-secret token)
    try:
        vendor_studio_sso.mint_token("x", role="admin", secret="")
        raise AssertionError("minting without a secret must raise")
    except ValueError:
        pass
    # no role -> refuse to mint (Vendor Studio would refuse it anyway; never a role-less token)
    for bad_role in ("", "  ", None):
        try:
            vendor_studio_sso.mint_token("x", role=bad_role, secret="shared-xyz")
            raise AssertionError("minting without a role must raise")
        except ValueError:
            pass
    print("  [F] SSO token = b64url(payload).hex(hmac), exp=now+60, verifiable per spec: OK")


def test_sso_route_and_button_gating():
    # ensure any stray env override does not mask the settings-driven gate
    os.environ.pop("VENDOR_STUDIO_URL", None)
    os.environ.pop("VENDOR_STUDIO_SSO_SECRET", None)
    c = TestClient(app)

    # UNCONFIGURED: the route 404s (non-enumerable) and the button is hidden.
    settings.set_many({"vendor_studio_url": ""})
    settings.set_secret("vendor_studio_sso_secret", "")
    settings.invalidate_cache()
    assert vendor_studio_sso.is_configured() is False
    r = c.post("/vendor-studio/open", follow_redirects=False)
    assert r.status_code == 404, ("unconfigured mint route must 404", r.status_code)
    assert "Open Vendor Studio" not in c.get("/vendors").text, "button must be hidden"

    # CONFIGURED (host-only settings): the button renders and the route mints a handoff.
    settings.set_many({"vendor_studio_url": "https://vendorstudio.example"})
    settings.set_secret("vendor_studio_sso_secret", "wire-shared-secret")
    settings.invalidate_cache()
    assert vendor_studio_sso.is_configured() is True
    assert "Open Vendor Studio" in c.get("/vendors").text, "button must render when configured"
    r = c.post("/vendor-studio/open", follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    # POST-form handoff: the token is in the FORM BODY, the action posts to {url}/sso,
    # and the token is NEVER in a query string.
    assert 'action="https://vendorstudio.example/sso"' in r.text, r.text[:400]
    assert 'name="token"' in r.text and 'method="post"' in r.text
    import re

    m = re.search(r'name="token"\s+value="([^"]+)"', r.text)
    assert m, "no token in the handoff form"
    payload = _verify_token(m.group(1), "wire-shared-secret")
    assert "user" in payload and "exp" in payload, payload
    assert payload["role"] == "admin" and payload["aud"] == "vendor-studio", payload
    assert payload["jti"] and payload["mo"] is False, payload
    # the secret itself never appears in the rendered page
    assert "wire-shared-secret" not in r.text, "the shared secret must never render"

    # host-only law: the settings are refused over the R3 /api config route.
    from app.services import settings as _settings

    assert "vendor_studio_url" in _settings.REMOTE_IMMUTABLE_KEYS
    assert "vendor_studio_sso_secret" in _settings.REMOTE_IMMUTABLE_KEYS
    assert "vendor_studio_sso_secret" in _settings.SECRET_SETTING_KEYS

    # reset for hermetic neighbours
    settings.set_many({"vendor_studio_url": ""})
    settings.set_secret("vendor_studio_sso_secret", "")
    settings.invalidate_cache()
    print(
        "  [F] mint route 404s unconfigured; button gated; POST-form handoff w/ token in "
        "body; settings host-only: OK"
    )


def test_sso_route_carries_org_scope_under_multi_org():
    """#557: with multi-org active the token states the operator's org and mo=true, so a
    Vendor Studio pinned to one org can refuse another org's admin."""
    from unittest.mock import patch

    os.environ.pop("VENDOR_STUDIO_URL", None)
    os.environ.pop("VENDOR_STUDIO_SSO_SECRET", None)
    settings.set_many({"vendor_studio_url": "https://vendorstudio.example"})
    settings.set_secret("vendor_studio_sso_secret", "wire-shared-secret")
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        with (
            patch("app.kernel.access.multi_org_active", return_value=True),
            patch("app.kernel.access.current_org_id", return_value=7),
        ):
            r = c.post("/vendor-studio/open", follow_redirects=False)
        assert r.status_code == 200, (r.status_code, r.text[:200])
        import re

        m = re.search(r'name="token"\s+value="([^"]+)"', r.text)
        payload = _verify_token(m.group(1), "wire-shared-secret")
        assert payload["org"] == 7 and payload["mo"] is True, payload
    finally:
        settings.set_many({"vendor_studio_url": ""})
        settings.set_secret("vendor_studio_sso_secret", "")
        settings.invalidate_cache()
    print("  [F] handoff token carries org + mo under multi-org: OK")


def test_api_account_exposes_org_only_under_multi_org():
    """#557: GET /api/account adds multi_org + org_id ONLY when multi-org is active (the
    single-tenant key set is pinned by verify_onboarding), so Vendor Studio's API-key login
    can enforce org scope."""
    from unittest.mock import patch

    from app.web import api_cli_misc_1

    assert api_cli_misc_1._org_scope_fields(object()) == {}, "single-tenant: no extra keys"
    with (
        patch("app.kernel.access.multi_org_active", return_value=True),
        patch("app.kernel.access.current_org_id", return_value=7),
    ):
        scoped = api_cli_misc_1._org_scope_fields(object())
    assert scoped == {"multi_org": True, "org_id": 7}, scoped
    # and the live route stays on the pre-existing key set while multi-org is off
    plain = TestClient(app).get("/api/account").json()
    assert "org_id" not in plain and "multi_org" not in plain, plain
    print("  [F] /api/account org_id + multi_org only under multi-org: OK")


if __name__ == "__main__":
    test_enrichment_fields_per_band()
    test_omit_locked_targets()
    test_glossary_block_and_cap()
    test_locked_roundtrip_no_clobber()
    test_omit_locked_translation_survives()
    test_omit_locked_approved_status_preserved()
    test_retry_contract_carries_enrichment()
    test_sso_mint_shape_and_hmac()
    test_sso_route_and_button_gating()
    test_sso_route_carries_org_scope_under_multi_org()
    test_api_account_exposes_org_only_under_multi_org()
    print("verify_vp0_package: all OK")
