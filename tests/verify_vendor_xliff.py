"""Vendor Wave D — the STANDARD XLIFF 1.2 package rendition (PROPOSAL-vendor-portal-interop
R2: "1.2 on the wire, 2.x state model internal-only"). Hermetic + offline: every dispatch
rides an injected httpx.MockTransport so no byte reaches a real network.

  [A] writer correctness: valid 1.2 shape, state/state-qualifier per provenance class,
      translate="no" on locked units, match-quality on fuzzy (via <alt-trans>)
  [B] writer->parser round trip: byte-identical targets + ids (incl. ⟦xN⟧ placeholders)
  [C] foreign-file tolerance: MQXLIFF-flavored + TXLF-flavored snippets parse (unknown
      namespaces/attributes ignored)
  [D] assets.xliff embedding is per-vendor opt-in (include_xliff), default OFF (wire stays
      byte-identical), and is a serialization of the same segments
  [E] end-to-end XLIFF RETURN through the REAL guarded seam: an edited target is admitted;
      a source-echo XLIFF target is a no-op (K1 law); over the signed webhook too
  [F] malformed XLIFF = one loud refusal (400 on the webhook), nothing admitted
"""

import hashlib
import hmac
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Never die on a non-ASCII glyph under a redirected cp1252 stdout on Windows. The
# prints below carry the real ⟦xN⟧ placeholder tokens (and failure reprs embed them);
# a bare redirected run without PYTHONIOENCODING=utf-8 would otherwise crash on encode.
# Repo convention (verify_ai_tagging, verify_language_safety, ...): reconfigure to UTF-8.
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_vxliff_")

import httpx
from fastapi.testclient import TestClient
from lxml import etree
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
    vendor_dispatch,
    vendor_return,
    vendor_xliff,
    vendors,
)
from app.services import workflows as wf

init_db()

_SEQ = [0]
SIGNING = "vxliff-signing-secret"
XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _run_step():
    return (SimpleNamespace(id=_seq()), SimpleNamespace(name="Vendor review", key="ext"))


def _sample_contract():
    """A 4-segment contract spanning every provenance class + a placeholder token."""
    run, step = _run_step()
    segs = [
        {
            "id": 1,
            "source": "Hello ⟦x1⟧ world.",
            "target": "Hola ⟦x1⟧ mundo.",
            "tm_match_pct": None,
        },  # mt (target present)
        {
            "id": 2,
            "source": "The cat sat.",
            "target": "El gato.",
            "tm_match_pct": 100,
        },  # tm_exact/locked
        {"id": 3, "source": "You have 3.", "target": "Tienes 3.", "tm_match_pct": 88},  # tm_fuzzy
        {"id": 4, "source": "Brand new.", "target": ""},  # new
    ]
    return vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=segs,
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="tok",
        glossary=[{"source_term": "cat", "target_term": "gato", "dnt": True}],
        style_guide="Be formal.",
    )


# ============================================================================
# [A] writer correctness
# ============================================================================
def test_writer_shape_and_mappings():
    xb = vendor_xliff.render_contract_xliff(_sample_contract())
    root = etree.fromstring(xb)  # must be well-formed
    assert etree.QName(root).localname == "xliff"
    assert root.get("version") == "1.2"
    assert etree.QName(root).namespace == XLIFF_NS

    file_el = root.find(f"{{{XLIFF_NS}}}file")
    assert file_el is not None
    assert file_el.get("source-language") == "en-US"
    assert file_el.get("target-language") == "es-ES"
    assert file_el.get("datatype") == "plaintext"
    # glossary + style guide ride as header <note>s
    notes = file_el.findall(f"{{{XLIFF_NS}}}header/{{{XLIFF_NS}}}note")
    note_text = "\n".join((n.text or "") for n in notes)
    assert "cat -> gato" in note_text and "DNT" in note_text, note_text
    assert "Be formal." in note_text

    tus = {tu.get("id"): tu for tu in root.iter(f"{{{XLIFF_NS}}}trans-unit")}
    assert set(tus) == {"1", "2", "3", "4"}

    def _tgt(tu):
        return tu.find(f"{{{XLIFF_NS}}}target")

    # mt -> needs-review-translation / mt-suggestion, no translate lock
    assert _tgt(tus["1"]).get("state") == "needs-review-translation"
    assert _tgt(tus["1"]).get("state-qualifier") == "mt-suggestion"
    assert tus["1"].get("translate") is None
    # tm_exact -> translated / exact-match, translate="no"
    assert _tgt(tus["2"]).get("state") == "translated"
    assert _tgt(tus["2"]).get("state-qualifier") == "exact-match"
    assert tus["2"].get("translate") == "no"
    # tm_fuzzy -> needs-review-translation / fuzzy-match + <alt-trans match-quality>
    assert _tgt(tus["3"]).get("state-qualifier") == "fuzzy-match"
    alt = tus["3"].find(f"{{{XLIFF_NS}}}alt-trans")
    assert alt is not None and alt.get("match-quality") == "88%", etree.tostring(tus["3"])
    # new -> needs-translation, no state-qualifier
    assert _tgt(tus["4"]).get("state") == "needs-translation"
    assert _tgt(tus["4"]).get("state-qualifier") is None

    # SAFETY: only the three research-verified core qualifiers are ever emitted.
    quals = {t.get("state-qualifier") for t in root.iter(f"{{{XLIFF_NS}}}target")}
    quals.discard(None)
    assert quals <= {"exact-match", "fuzzy-match", "mt-suggestion"}, quals
    print(
        "  [A] valid 1.2; state/qualifier per provenance; translate=no lock; "
        "fuzzy match-quality; only verified-core qualifiers: OK"
    )


# ============================================================================
# [B] writer -> parser round trip (byte-identical targets + ids)
# ============================================================================
def test_round_trip_byte_identical():
    contract = _sample_contract()
    xb = vendor_xliff.render_contract_xliff(contract)
    parsed = vendor_xliff.parse_return_xliff(xb)
    assert [s["id"] for s in parsed["segments"]] == ["1", "2", "3", "4"]
    assert parsed["target_locale"] == "es-ES"
    by = {s["id"]: s["target"] for s in parsed["segments"]}
    src_by = {str(s["id"]): s["target"] for s in contract["segments"]}
    for sid in ("1", "2", "3", "4"):
        assert by[sid] == src_by[sid], (sid, repr(by[sid]), repr(src_by[sid]))
    # the ⟦x1⟧ placeholder survived byte-identically
    assert by["1"] == "Hola ⟦x1⟧ mundo."
    assert "⟦x1⟧" in by["1"]
    print("  [B] writer->parser round trip: ids + targets byte-identical, ⟦x1⟧ survives: OK")


# ============================================================================
# [C] foreign-file tolerance (MQXLIFF / TXLF flavored)
# ============================================================================
def test_foreign_mqxliff_tolerated():
    # MQXLIFF-flavored: an mq: extension namespace + mq:locked attribute the parser ignores.
    doc = (
        '<?xml version="1.0"?>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
        'xmlns:mq="MQXliff">'
        '<file source-language="en-US" target-language="es-ES" datatype="plaintext"><body>'
        '<trans-unit id="1" mq:status="Confirmed"><source>Hello.</source>'
        '<target mq:locked="locked">Hola.</target></trans-unit>'
        '<trans-unit id="2"><source>Bye.</source><target>Adios.</target></trans-unit>'
        "</body></file></xliff>"
    )
    out = vendor_xliff.parse_return_xliff(doc)
    assert [s["id"] for s in out["segments"]] == ["1", "2"]
    assert {s["id"]: s["target"] for s in out["segments"]} == {"1": "Hola.", "2": "Adios."}
    assert out.get("target_locale") == "es-ES"
    print("  [C1] MQXLIFF-flavored file parses (mq: namespace/attrs ignored): OK")


def test_foreign_txlf_tolerated():
    # TXLF-flavored: a gs4tr: namespace + gs4tr:locked attribute the parser ignores.
    doc = (
        '<?xml version="1.0"?>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
        'xmlns:gs4tr="http://www.gs4tr.org/schema/xliff-ext">'
        '<file source-language="en-US" target-language="de-DE" datatype="plaintext"><body>'
        '<trans-unit id="a" gs4tr:locked="true"><source>One.</source>'
        "<target>Eins.</target></trans-unit>"
        '<trans-unit id="b"><source>Two.</source><target>Zwei.</target></trans-unit>'
        "</body></file></xliff>"
    )
    out = vendor_xliff.parse_return_xliff(doc)
    assert {s["id"]: s["target"] for s in out["segments"]} == {"a": "Eins.", "b": "Zwei."}
    assert out.get("target_locale") == "de-DE"
    # a NO-namespace hand-built file parses too (namespace-agnostic on local-name)
    plain = (
        '<xliff version="1.2"><file target-language="fr-FR"><body>'
        '<trans-unit id="9"><source>Cat</source><target>Chat</target></trans-unit>'
        "</body></file></xliff>"
    )
    out2 = vendor_xliff.parse_return_xliff(plain)
    assert out2["segments"] == [{"id": "9", "target": "Chat"}]
    print("  [C2] TXLF-flavored + no-namespace files parse (gs4tr: ignored): OK")


# ============================================================================
# [D] assets.xliff embedding is per-vendor opt-in (default OFF)
# ============================================================================
def test_assets_xliff_opt_in():
    run, step = _run_step()
    segs = [{"id": 1, "source": "Hello.", "target": "Hola.", "tm_match_pct": 100}]
    off = SimpleNamespace(config=None)
    on = SimpleNamespace(config={"include_xliff": True})
    c_off = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=segs,
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        vendor=off,
    )
    assert "xliff" not in c_off["assets"], "default OFF must keep the wire byte-identical"
    assert vendors.include_xliff(off) is False

    run2, step2 = _run_step()
    c_on = vendor_dispatch.build_contract(
        run=run2,
        step=step2,
        segments=segs,
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        vendor=on,
    )
    assert vendors.include_xliff(on) is True
    assert isinstance(c_on["assets"]["xliff"], str)
    # it is a serialization of THE SAME segments
    parsed = vendor_xliff.parse_return_xliff(c_on["assets"]["xliff"])
    assert [s["id"] for s in parsed["segments"]] == ["1"]
    assert parsed["segments"][0]["target"] == "Hola."
    print(
        "  [D] assets.xliff is per-vendor opt-in (include_xliff), default OFF, "
        "serializes the same segments: OK"
    )


# ============================================================================
# E2E scaffolding (orchestrator, hermetic) for [E] + [F]
# ============================================================================
def _mock_transport(status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler)


def _seed():
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
        for i, src in enumerate(["First string.", "Second string.", "Third string."]):
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


def _dispatch(env, status=200):
    vendor_dispatch.set_test_transport(_mock_transport(status=status))
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        return run.id, leg.id, leg.claim_token


def _seg_ids(doc_id):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, "es-ES")]


def _return_xliff(target_locale, id_target_pairs) -> bytes:
    """A hand-built XLIFF 1.2 RETURN (source omitted; only ids + edited targets)."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<xliff version="1.2" xmlns="{XLIFF_NS}">',
        f'<file target-language="{target_locale}" datatype="plaintext"><body>',
    ]
    for sid, tgt in id_target_pairs:
        parts.append(f'<trans-unit id="{sid}"><target>{tgt}</target></trans-unit>')
    parts.append("</body></file></xliff>")
    return "".join(parts).encode("utf-8")


# ============================================================================
# [E] end-to-end XLIFF return through the REAL guarded seam
# ============================================================================
def test_xliff_return_manual_admits_edit():
    env = _seed()
    _, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # edit every target with a genuine translation (not a source-echo)
    xb = _return_xliff("es-ES", [(i, f"Cadena vendida {i}.") for i in ids])
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, xb, claim_token=claim, resume="sync", actor="tester")
    assert out["complete"] is True, out
    assert len(out["admitted"]) == len(ids), out
    with Session(engine) as s:
        segs = {sg.id: sg for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")}
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
    for i in ids:
        assert segs[i].target_text == f"Cadena vendida {i}.", segs[i].target_text
    print("  [E1] an XLIFF return through admit_manual admits edited targets, leg RETURNED: OK")


def test_xliff_return_source_echo_is_no_op():
    env = _seed()
    _, _leg_id, claim = _dispatch(env)
    with Session(engine) as s:
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        ids = [sg.id for sg in segs]
        before = {sg.id: (sg.target_text or "") for sg in segs}
        src = {sg.id: (sg.source_text or "") for sg in segs}
    # return the SOURCE text for every id (a non-English target) -> K1 source-echo no-op lane
    xb = _return_xliff("es-ES", [(i, src[i]) for i in ids])
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, xb, claim_token=claim, resume="sync", actor="tester")
    assert out["complete"] is True, out
    assert set(str(x) for x in out["no_op"]) == set(str(i) for i in ids), out  # noqa: C401
    assert out["admitted"] == [], out
    with Session(engine) as s:
        after = {
            sg.id: (sg.target_text or "")
            for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")
        }
    for i in ids:
        assert after[i] == before[i], ("source-echo must not clobber", i)
    print("  [E2] a source-echo XLIFF return is a no-op (K1 law), stored targets untouched: OK")


def test_xliff_return_over_signed_webhook():
    env = _seed()
    _, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    xb = _return_xliff("es-ES", [(i, f"Via webhook {i}.") for i in ids])
    sig = hmac.new(SIGNING.encode(), xb, hashlib.sha256).hexdigest()
    c = TestClient(app)
    r = c.post(
        "/webhooks/vendor-returns",
        content=xb,
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-signature": f"sha256={sig}",
            "content-type": "application/xml",
        },
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    # a BAD signature over the same XLIFF is a 403 (auth before parse)
    r2 = c.post(
        "/webhooks/vendor-returns",
        content=xb,
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-signature": "sha256=deadbeef",
            "content-type": "application/xml",
        },
    )
    assert r2.status_code == 403, (r2.status_code, r2.text[:200])
    print(
        "  [E3] a signed XLIFF return admits over POST /webhooks/vendor-returns (bad "
        "sig -> 403): OK"
    )


# ============================================================================
# [F] malformed XLIFF = one loud refusal, nothing admitted
# ============================================================================
def test_malformed_xliff_refused():
    env = _seed()
    _, leg_id, claim = _dispatch(env)
    # not well-formed XML
    bad = b'<?xml version="1.0"?><xliff><file><body><trans-unit id="1"><target>x'
    with Session(engine) as s:
        try:
            vendor_return.admit_manual(s, bad, claim_token=claim, resume="sync")
            raise AssertionError("a malformed XLIFF must be refused")
        except vendor_return.ReturnContractError:
            pass
    # over the webhook it is one loud 400, nothing admitted
    sig = hmac.new(SIGNING.encode(), bad, hashlib.sha256).hexdigest()
    c = TestClient(app)
    r = c.post(
        "/webhooks/vendor-returns",
        content=bad,
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-signature": f"sha256={sig}",
            "content-type": "application/xml",
        },
    )
    assert r.status_code == 400, (r.status_code, r.text[:200])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED, ("nothing admitted", leg.status)
    print(
        "  [F] malformed XLIFF refused (service) + one loud 400 (webhook), leg still DISPATCHED: OK"
    )


# ============================================================================
# [G] cross-format: Loc's parser reads a VENDOR-STUDIO-shaped return
# ============================================================================
# A GOLDEN return in exactly the shape Vendor Studio's INDEPENDENT writer emits (default
# XLIFF 1.2 namespace, xml:lang, provenance-driven state/state-qualifier, translate="no"
# locks, an <alt-trans> fuzzy candidate). Isolation forbids importing vstudio here (see
# verify_vendor_studio scenario_isolation), so cross-validation is proven by a pinned
# wire sample rather than a cross-import: if either implementation drifts off the shared
# 1.2 profile, this golden stops parsing. The reverse direction (VS parsing a Loc-shaped
# file) is pinned in tools/vendor_studio/tests/test_xliff.py.
_VS_GOLDEN = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">'
    '<file original="run-9" source-language="en-US" target-language="es-ES" '
    'datatype="plaintext"><body>'
    '<trans-unit id="1"><source xml:lang="en-US">Hello ⟦x1⟧ world.</source>'
    '<target xml:lang="es-ES" state="needs-review-translation" '
    'state-qualifier="mt-suggestion">Hola ⟦x1⟧ mundo.</target></trans-unit>'
    '<trans-unit id="2" translate="no"><source xml:lang="en-US">The cat sat.</source>'
    '<target xml:lang="es-ES" state="translated" state-qualifier="exact-match">El gato.'
    "</target></trans-unit>"
    '<trans-unit id="3"><source xml:lang="en-US">You have 3.</source>'
    '<target xml:lang="es-ES" state="needs-review-translation" '
    'state-qualifier="fuzzy-match">Tienes 3.</target>'
    '<alt-trans match-quality="88%"><target>CANDIDATE, not the delivered target</target>'
    "</alt-trans></trans-unit>"
    "</body></file></xliff>"
)


def test_parses_vendor_studio_shaped_return():
    out = vendor_xliff.parse_return_xliff(_VS_GOLDEN.encode("utf-8"))
    assert [s["id"] for s in out["segments"]] == ["1", "2", "3"]
    assert out["target_locale"] == "es-ES"
    by = {s["id"]: s["target"] for s in out["segments"]}
    assert by["1"] == "Hola ⟦x1⟧ mundo."  # ⟦x1⟧ survives from a foreign writer
    # the DIRECT <target> is taken, never the <alt-trans> candidate
    assert by["3"] == "Tienes 3.", by["3"]
    print(
        "  [G] Loc parser reads a Vendor-Studio-shaped return; direct target (not the "
        "alt-trans candidate) is admitted; ⟦x1⟧ survives: OK"
    )


# ============================================================================
# [H] render_leg_xliff: the operator "Download XLIFF" reconstruction path
# ============================================================================
def test_render_leg_xliff_reconstructs_from_segments():
    env = _seed()
    _, leg_id, _ = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.DISPATCHED
        data = vendor_xliff.render_leg_xliff(s, leg)  # rebuild from live segments
    parsed = vendor_xliff.parse_return_xliff(data)
    got = [int(x["id"]) for x in parsed["segments"]]
    assert got == ids, (got, ids)
    assert parsed["target_locale"] == "es-ES"
    root = etree.fromstring(data)
    assert root.get("version") == "1.2"
    print("  [H1] render_leg_xliff rebuilds a DISPATCHED leg's XLIFF from segments: OK")


def test_render_leg_xliff_uses_retry_contract():
    env = _seed()
    # a 500 parks the leg RETRY_WAIT holding the exact frozen contract.
    _, leg_id, _ = _dispatch(env, status=500)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT
        assert leg.retry_contract, "retry leg must hold the frozen package"
        data = vendor_xliff.render_leg_xliff(s, leg)  # renders from retry_contract
    parsed = vendor_xliff.parse_return_xliff(data)
    assert [x["id"] for x in parsed["segments"]], parsed
    print("  [H2] render_leg_xliff renders a RETRY_WAIT leg from its frozen retry_contract: OK")


# ============================================================================
# FIX ROUND — HIGH-1: line-ending normalization / source-echo
# ============================================================================
def test_crlf_source_echo_via_xliff_is_no_op():
    env = _seed()
    # make the first segment's source carry a CRLF.
    with Session(engine) as s:
        seg0 = orchestrator._load_segments(s, env["doc_id"], "es-ES")[0]
        seg0.source_text = "First\r\nline."
        s.add(seg0)
        s.commit()
    _, _leg_id, claim = _dispatch(env)
    with Session(engine) as s:
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        ids = [sg.id for sg in segs]
        before = {sg.id: (sg.target_text or "") for sg in segs}
        src = {sg.id: (sg.source_text or "") for sg in segs}
    # echo each source back; the CRLF source goes back with a literal CR in the XML.
    xb = _return_xliff("es-ES", [(i, src[i]) for i in ids])
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, xb, claim_token=claim, resume="sync")
    assert out["complete"] is True, out
    assert set(str(x) for x in out["no_op"]) == set(str(i) for i in ids), out  # noqa: C401
    with Session(engine) as s:
        after = {
            sg.id: (sg.target_text or "")
            for sg in orchestrator._load_segments(s, env["doc_id"], "es-ES")
        }
    for i in ids:
        assert after[i] == before[i], ("CRLF source-echo must not clobber", i)
    print(
        "  [HIGH-1] a CRLF source echoed via XLIFF (with CR) -> no_op lane, stored target "
        "untouched: OK"
    )


# ============================================================================
# FIX ROUND — HIGH-2: XML-invalid codepoints never raise / never 500
# ============================================================================
def test_control_char_sanitized_by_writer_and_routes():
    # writer: a form-feed (U+000C) + NUL in content must NOT raise; output is valid XML.
    contract = {
        "contract_version": "1.0",
        "run_id": 1,
        "step_key": "ext",
        "source_locale": "en-US",
        "target_locale": "es-ES",
        "segments": [
            {"id": 1, "source": "Bad\x0c\x00char.", "target": "T\x0bgt", "provenance": "mt"}
        ],
        "assets": {},
    }
    xb = vendor_xliff.render_contract_xliff(contract)
    root = etree.fromstring(xb)  # valid XML, no raise
    txt = root.findtext(f".//{{{XLIFF_NS}}}source")
    assert "�" in txt and "\x0c" not in txt, repr(txt)

    # build_contract with include_xliff ON + a control char must NOT kill the dispatch.
    run, step = _run_step()
    von = SimpleNamespace(config={"include_xliff": True})
    c = vendor_dispatch.build_contract(
        run=run,
        step=step,
        segments=[{"id": 1, "source": "x\x0cy", "target": "t", "tm_match_pct": None}],
        source_locale="en-US",
        target_locale="es-ES",
        claim_token="t",
        vendor=von,
    )
    assert "xliff" in c["assets"], "include_xliff must still embed despite a control char"
    etree.fromstring(c["assets"]["xliff"].encode("utf-8"))  # valid

    # the WEB download route must serve 200 (not 500) when a segment carries a control char.
    env = _seed()
    _, leg_id, _ = _dispatch(env)
    with Session(engine) as s:
        seg0 = orchestrator._load_segments(s, env["doc_id"], "es-ES")[0]
        seg0.target_text = "ctl\x0cchar"
        s.add(seg0)
        s.commit()
        vid = s.get(VendorDispatch, leg_id).vendor_id
    c2 = TestClient(app)
    r = c2.get(f"/vendors/{vid}/dispatches/{leg_id}/xliff", follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    etree.fromstring(r.content)  # valid XML from the route
    print(
        "  [HIGH-2] control chars sanitized to U+FFFD in writer + build(flag on) + web "
        "route (200, not 500): OK"
    )


# ============================================================================
# FIX ROUND — mutation 4: outbound CR survives the writer as &#13;
# ============================================================================
def test_outbound_cr_survives_writer_as_charref():
    contract = {
        "contract_version": "1.0",
        "source_locale": "en-US",
        "target_locale": "es-ES",
        "step_key": "ext",
        "segments": [{"id": 1, "source": "a\rb", "target": "c\rd", "provenance": "mt"}],
        "assets": {},
    }
    xb = vendor_xliff.render_contract_xliff(contract)
    assert b"&#13;" in xb, xb  # the writer preserves CR as a char reference
    # a RAW (non-normalizing) parse yields the CR back -> the writer did not lose it.
    root = etree.fromstring(xb)
    assert "\r" in root.findtext(f".//{{{XLIFF_NS}}}source"), "CR must survive serialization"
    print("  [MUT-4] outbound CR survives the writer as &#13; and round-trips raw: OK")


# ============================================================================
# FIX ROUND — MED-1: cross-leg replay of a token-bearing file -> 400
# ============================================================================
def test_med1_cross_leg_replay_refused():
    env = _seed()
    _, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # a return file that CARRIES a DIFFERENT claim token in the loc-studio-claim note.
    bad_contract = {
        "contract_version": "1.0",
        "source_locale": "en-US",
        "target_locale": "es-ES",
        "step_key": "ext",
        "claim_token": "A-DIFFERENT-LEGS-TOKEN",
        "segments": [
            {"id": i, "source": "", "target": f"edit {i}", "provenance": "mt"} for i in ids
        ],
        "assets": {},
    }
    xb = vendor_xliff.render_contract_xliff(bad_contract)
    assert b"loc-studio-claim" in xb, "the claim note must be present in the file"
    sig = hmac.new(SIGNING.encode(), xb, hashlib.sha256).hexdigest()
    c = TestClient(app)
    # authenticated for THIS leg (claim header + valid signature), but the file's note names
    # another leg -> check_contract mismatch -> 400.
    r = c.post(
        "/webhooks/vendor-returns",
        content=xb,
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-signature": f"sha256={sig}",
            "content-type": "application/xml",
        },
    )
    assert r.status_code == 400, (r.status_code, r.text[:200])
    with Session(engine) as s:
        assert s.get(VendorDispatch, leg_id).status == VendorDispatchStatus.DISPATCHED
    # a token-LESS file (foreign tool stripped notes) still admits via header binding.
    ok = _return_xliff("es-ES", [(i, f"ok {i}") for i in ids])
    sig2 = hmac.new(SIGNING.encode(), ok, hashlib.sha256).hexdigest()
    r2 = c.post(
        "/webhooks/vendor-returns",
        content=ok,
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-signature": f"sha256={sig2}",
            "content-type": "application/xml",
        },
    )
    assert r2.status_code == 200, (r2.status_code, r2.text[:200])
    print(
        "  [MED-1] a token-bearing file replayed onto another leg -> 400; a token-less "
        "file still binds by header: OK"
    )


# ============================================================================
# FIX ROUND — MED-2/MED-3: DOCTYPE / XXE / entity refusal + parse ORDER
# ============================================================================
_DOCTYPE_XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE xliff [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
    '<file target-language="es-ES"><body><trans-unit id="1"><target>&xxe;</target>'
    "</trans-unit></body></file></xliff>"
)
_BILLION_LAUGHS = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
    '<xliff version="1.2"><file><body><trans-unit id="1"><target>&lol2;</target>'
    "</trans-unit></body></file></xliff>"
)


def test_med2_doctype_and_entities_refused():
    for doc in (_DOCTYPE_XXE, _BILLION_LAUGHS):
        try:
            vendor_xliff.parse_return_xliff(doc.encode("utf-8"))
            raise AssertionError("a DOCTYPE document must be refused")
        except vendor_xliff.XliffParseError as exc:
            assert "DOCTYPE" in str(exc) or "DTD" in str(exc), exc
    # the canary file path must NEVER appear in any decoded content (no XXE leak).
    env = _seed()
    _, _leg_id, claim = _dispatch(env)
    sig = hmac.new(SIGNING.encode(), _DOCTYPE_XXE.encode(), hashlib.sha256).hexdigest()
    c = TestClient(app)
    r = c.post(
        "/webhooks/vendor-returns",
        content=_DOCTYPE_XXE.encode(),
        headers={
            "x-locstudio-claim": claim,
            "x-locstudio-signature": f"sha256={sig}",
            "content-type": "application/xml",
        },
    )
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert "passwd" not in r.text and "root:" not in r.text
    # guard-presence (functional): a doc that references an entity with NO DTD to define it
    # is refused as not-well-formed -- the parser never fabricates/fetches an entity value.
    no_dtd_entity = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body><trans-unit id="1">'
        "<target>&xxe;</target></trans-unit></body></file></xliff>"
    )
    try:
        vendor_xliff.parse_return_xliff(no_dtd_entity.encode("utf-8"))
        raise AssertionError("an undefined entity must be refused, never resolved")
    except vendor_xliff.XliffParseError:
        pass
    print(
        "  [MED-2/3] DOCTYPE + billion-laughs refused; XXE canary non-leak; undefined "
        "entity refused (not resolved): OK"
    )


def test_med3_signature_verified_before_parse():
    env = _seed()
    _, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    xb = _return_xliff("es-ES", [(i, f"x {i}") for i in ids])
    # spy on the parser: a BAD signature must reach 403 WITHOUT ever calling parse.
    calls = {"n": 0}
    orig = vendor_xliff.parse_return_xliff

    def _spy(data):
        calls["n"] += 1
        return orig(data)

    vendor_xliff.parse_return_xliff = _spy
    try:
        c = TestClient(app)
        r = c.post(
            "/webhooks/vendor-returns",
            content=xb,
            headers={
                "x-locstudio-claim": claim,
                "x-locstudio-signature": "sha256=deadbeef",
                "content-type": "application/xml",
            },
        )
    finally:
        vendor_xliff.parse_return_xliff = orig
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert calls["n"] == 0, ("parse must NOT run on a bad-signature request", calls["n"])
    print(
        "  [MED-3] signature is verified BEFORE the XLIFF is ever parsed (parse_called==0 "
        "on bad sig): OK"
    )


# ============================================================================
# FIX ROUND — MED-4: render_leg_xliff refuses on drift
# ============================================================================
def test_med4_render_refuses_on_drift():
    env = _seed()
    _, leg_id, _ = _dispatch(env)
    # DELETE a dispatched segment -> the reconstructed id set no longer covers dispatched.
    with Session(engine) as s:
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        s.delete(segs[0])
        s.commit()
        leg = s.get(VendorDispatch, leg_id)
        try:
            vendor_xliff.render_leg_xliff(s, leg)
            raise AssertionError("a drifted leg must refuse to render")
        except vendor_xliff.XliffRenderError as exc:
            assert "drift" in str(exc).lower(), exc
    # a leg with NO ledger (predates it) also refuses.
    env2 = _seed()
    _, leg2_id, _ = _dispatch(env2)
    with Session(engine) as s:
        leg2 = s.get(VendorDispatch, leg2_id)
        leg2.contract_ids = None
        s.add(leg2)
        s.commit()
        try:
            vendor_xliff.render_leg_xliff(s, leg2)
            raise AssertionError("a ledger-less leg must refuse")
        except vendor_xliff.XliffRenderError as exc:
            assert "ledger" in str(exc).lower(), exc
    print(
        "  [MED-4] render_leg_xliff refuses on segment drift + ledger-less leg (no wrong "
        "file emitted): OK"
    )


# ============================================================================
# FIX ROUND — MED-5: inline markup refused; alt-trans / nesting handling
# ============================================================================
def test_med5_inline_markup_refused():
    for inner in (
        'Hola <x id="1"/> mundo.',
        'Hola <bpt id="1">&lt;b&gt;</bpt>mundo<ept id="1">&lt;/b&gt;</ept>.',
    ):
        doc = (
            '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
            '<file target-language="es-ES"><body>'
            f'<trans-unit id="1"><target>{inner}</target></trans-unit>'
            "</body></file></xliff>"
        )
        try:
            vendor_xliff.parse_return_xliff(doc.encode("utf-8"))
            raise AssertionError(f"inline markup must be refused: {inner}")
        except vendor_xliff.XliffParseError as exc:
            assert "inline" in str(exc).lower() or "literal" in str(exc).lower(), exc
    print("  [MED-5] inline markup (<x/>, <bpt>) inside <target> -> refused: OK")


def test_med5_alt_trans_and_nesting():
    # the DIRECT target is admitted, NEVER a DIFFERENT alt-trans candidate (kill mutation 5),
    # and a trans-unit nested inside an alt-trans is ignored (LOW-1).
    doc = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body>'
        '<trans-unit id="1"><target>DELIVERED</target>'
        '<alt-trans match-quality="80%"><target>CANDIDATE-DIFFERENT</target>'
        '<trans-unit id="999"><target>NESTED-SHOULD-BE-IGNORED</target></trans-unit>'
        "</alt-trans></trans-unit>"
        "</body></file></xliff>"
    )
    out = vendor_xliff.parse_return_xliff(doc.encode("utf-8"))
    assert [s["id"] for s in out["segments"]] == ["1"], out
    assert out["segments"][0]["target"] == "DELIVERED", out
    print(
        "  [MED-5b/LOW-1] direct target (not alt-trans candidate) admitted; nested "
        "trans-unit in alt-trans ignored: OK"
    )


# ============================================================================
# FIX ROUND — LOW-2 (UTF-16) + LOW-3 (id length cap)
# ============================================================================
def test_low2_utf16_return_parses():
    env = _seed()
    _, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    tus = "".join(f'<trans-unit id="{i}"><target>UTF16 {i}.</target></trans-unit>' for i in ids)
    xml_str = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        f'<file target-language="es-ES"><body>{tus}</body></file></xliff>'
    )
    data16 = xml_str.encode("utf-16")  # BOM + UTF-16 (the CLI passes bytes)
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, data16, claim_token=claim, resume="sync")
    assert out["complete"] is True and len(out["admitted"]) == len(ids), out
    print("  [LOW-2] a UTF-16 .xlf (bytes, as the CLI now reads it) parses + admits: OK")


def test_low3_oversized_id_refused():
    big = "9" * 300
    doc = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body>'
        f'<trans-unit id="{big}"><target>x</target></trans-unit>'
        "</body></file></xliff>"
    )
    try:
        vendor_xliff.parse_return_xliff(doc.encode("utf-8"))
        raise AssertionError("an oversized id must be refused")
    except vendor_xliff.XliffParseError as exc:
        assert "256" in str(exc), exc
    print("  [LOW-3] an oversized trans-unit id (>256) is refused: OK")


# ============================================================================
# CLOSURE MICRO-ROUND — R1/R2/R3
# ============================================================================
def test_r1_retry_leg_download_has_no_claim_token():
    env = _seed()
    _, leg_id, claim = _dispatch(env, status=500)  # parks RETRY_WAIT w/ frozen contract
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETRY_WAIT
        assert (leg.retry_contract or {}).get("claim_token"), "the frozen package HAS the token"
        data = vendor_xliff.render_leg_xliff(s, leg)
    assert claim.encode("utf-8") not in data, "the RETRY_WAIT download must NOT leak the token"
    assert b"loc-studio-claim" not in data, "no claim note in an operator download"
    print("  [R1] a RETRY_WAIT leg's rendered XLIFF contains no claim token / claim note: OK")


def test_r2_utf16_doctype_refused_both_parsers():
    xml_str = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE xliff [<!ENTITY x "y">]>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body><trans-unit id="1"><target>t</target>'
        "</trans-unit></body></file></xliff>"
    )
    data16 = xml_str.encode("utf-16")
    assert b"<!DOCTYPE" not in data16, "the DOCTYPE bytes are UTF-16 (ASCII prescan misses)"
    try:
        vendor_xliff.parse_return_xliff(data16)
        raise AssertionError("Loc parser must refuse a UTF-16 DOCTYPE")
    except vendor_xliff.XliffParseError as exc:
        assert "DOCTYPE" in str(exc) or "DTD" in str(exc), exc
    # VS parser (independent) must refuse it too.
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "vendor_studio"
        ),
    )
    print("  [R2] a UTF-16 DOCTYPE doc is refused by the Loc parser (post-parse docinfo): OK")


def test_r3_claim_note_position_and_case():
    # (a) a bogus loc-studio-claim note INSIDE a trans-unit must NOT be read as the binding
    # (no false whole-file refusal): the return still admits by header/operator binding.
    env = _seed()
    _, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    tus = "".join(
        f'<trans-unit id="{i}"><note from="loc-studio-claim">BOGUS-{i}</note>'
        f"<target>edit {i}</target></trans-unit>"
        for i in ids
    )
    doc = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        f'<file target-language="es-ES"><body>{tus}</body></file></xliff>'
    )
    parsed = vendor_xliff.parse_return_xliff(doc.encode("utf-8"))
    assert "claim_token" not in parsed, (
        "a trans-unit note must NOT be read as the binding",
        parsed,
    )
    with Session(engine) as s:
        out = vendor_return.admit_manual(s, doc.encode("utf-8"), claim_token=claim, resume="sync")
    assert out["complete"] is True and len(out["admitted"]) == len(ids), out

    # (b) the header note's `from` is matched CASE-INSENSITIVELY.
    doc2 = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES">'
        '<header><note from="LOC-STUDIO-CLAIM">TOKEN-XYZ</note></header>'
        '<body><trans-unit id="1"><target>x</target></trans-unit></body></file></xliff>'
    )
    parsed2 = vendor_xliff.parse_return_xliff(doc2.encode("utf-8"))
    assert parsed2.get("claim_token") == "TOKEN-XYZ", ("case-insensitive from", parsed2)
    print("  [R3] claim note honored only at header position + case-insensitive `from`: OK")


# ============================================================================
# #674 (format soak): `<!DOCTYPE` TEXT inside CDATA is content, not a declaration
# ============================================================================
_PHRASE_PREVIEW_MXLIFF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<xliff xmlns:m="http://www.memsource.com/mxlf/2.0" '
    'xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2" m:version="2.15">\n'
    '<file original="doc1.docx" source-language="en-us" target-language="de-de" '
    'datatype="x-undefined" m:file-format="DOC"><header>'
    '<m:in-ctx-preview-skel bilingual="false"><![CDATA[<?xml version="1.0"?>\n'
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"><html><body><p>x</p></body></html>]]>'
    "</m:in-ctx-preview-skel></header><body>"
    '<group id="0"><trans-unit id="t:0" xml:space="preserve">'
    "<source>Open the settings page.</source><target></target></trans-unit></group>"
    "</body></file></xliff>"
)


def test_674_doctype_text_in_cdata_is_content():
    from app.filters import get_filter

    raw = _PHRASE_PREVIEW_MXLIFF.encode("utf-8")
    vendor_xliff._reject_doctype(raw)  # no declaration in the prolog -> no refusal
    res = get_filter("mxliff").parse(raw)
    texts = [u.text for u in res.units]
    assert texts == ["Open the settings page."], texts
    # A REAL prolog DOCTYPE is still refused, also after a comment / PI in the prolog,
    # and a comment containing a fake root start tag cannot hide one.
    for prolog in (
        '<?xml version="1.0"?><!-- <xliff> --><!DOCTYPE x [<!ENTITY a "b">]>',
        '<?xml version="1.0"?><?pi data?>\n<!doctype x SYSTEM "file:///etc/passwd">',
    ):
        doc = (prolog + '<xliff version="1.2"><file><body/></file></xliff>').encode("utf-8")
        try:
            vendor_xliff._reject_doctype(doc)
            raise AssertionError("a prolog DOCTYPE must still be refused: " + prolog)
        except vendor_xliff.XliffParseError:
            pass
    print("  [#674] DOCTYPE text inside CDATA parses; prolog DOCTYPE (after comment/PI) refused OK")


if __name__ == "__main__":
    test_writer_shape_and_mappings()
    test_round_trip_byte_identical()
    test_foreign_mqxliff_tolerated()
    test_foreign_txlf_tolerated()
    test_assets_xliff_opt_in()
    test_xliff_return_manual_admits_edit()
    test_xliff_return_source_echo_is_no_op()
    test_xliff_return_over_signed_webhook()
    test_malformed_xliff_refused()
    test_parses_vendor_studio_shaped_return()
    test_render_leg_xliff_reconstructs_from_segments()
    test_render_leg_xliff_uses_retry_contract()
    test_crlf_source_echo_via_xliff_is_no_op()
    test_control_char_sanitized_by_writer_and_routes()
    test_outbound_cr_survives_writer_as_charref()
    test_med1_cross_leg_replay_refused()
    test_med2_doctype_and_entities_refused()
    test_med3_signature_verified_before_parse()
    test_med4_render_refuses_on_drift()
    test_med5_inline_markup_refused()
    test_med5_alt_trans_and_nesting()
    test_low2_utf16_return_parses()
    test_low3_oversized_id_refused()
    test_r1_retry_leg_download_has_no_claim_token()
    test_r2_utf16_doctype_refused_both_parsers()
    test_r3_claim_note_position_and_case()
    test_674_doctype_text_in_cdata_is_content()
    print("verify_vendor_xliff: all OK")
