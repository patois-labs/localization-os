"""Wire-less (manual) vendor hand-off surfaces on the RUN VIEW and the DOCUMENT page
(feat/run-doc-xliff-surfaces).

Direct user feedback that drove this (2026-08-10): the `manual` (wire-less) transport
ships delivery as "operator downloads the leg's XLIFF, returns via a guarded upload", but the
ONLY place those controls lived was the vendor detail page. The maintainer looked in the project's
Segments menu (/documents/{id}) and the run's Human-translation step (/runs/{id}) and found
nothing but a "download an MD file" -- "there should be a download and upload xliff here for
human review". This suite pins the new discoverability:

  [1] RUN VIEW (/runs/{id}) -- a paused run whose CURRENT external pause is an OPEN wire-less
      leg (DISPATCHED / QUOTED, keyed off the leg's protocol SNAPSHOT) shows the Download-XLIFF
      link + the guarded return form (action + hidden `next=/runs/{id}`); a NATIVE leg keeps
      the old "re-enters through the vendor return path" copy and gets NO upload form; an
      ESCALATED leg keeps its existing escalated card (no hand-off).
  [2] DOCUMENT PAGE (/documents/{id}) -- the selected locale with an OPEN wire-less leg shows
      the Download-XLIFF link + a link into the run view's hand-off card; no leg -> absent; a
      NATIVE (wired) leg -> absent (the gate is wire-less, not merely "open").
  [3] RETURN-ROUTE `next` UX -- POST .../return with next=/runs/{id} redirects to
      /runs/{id}?saved=1 on success and /runs/{id}?err=... on a refused payload; open-redirect
      attempts (//evil.com, https://evil.com, /\\evil.com) fall back to the vendor page.
  [4] PERMISSIONS -- a non-admin who can still VIEW the pages gets NONE of the controls (the
      GET/POST routes stay independently admin-gated regardless -- proven in verify_vendor_
      return_web).
  [5] END-TO-END -- a filled .xlf uploaded through the RUN-VIEW form path admits, lands the
      targets, closes the leg RETURNED, and schedules the resume.

Hermetic + offline. The manual dispatch rides a RAISING httpx transport (any request is a hard
failure) proving the wire-less path performs zero network I/O; the resume is stubbed by an
autouse fixture WITH TEARDOWN so nothing leaks into sibling suites (the MEDIUM-7 lesson from
verify_vendor_return_web). Every test covers NEW display behaviour -- none pins a commit sha.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_rundocxliff_")

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from lxml import etree
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    Role,
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
    security,
    settings,
    vendor_dispatch,
    vendor_return,
    vendor_xliff,
    vendors,
)
from app.services import workflows as wf
from tests._csrf import csrf_for

init_db()

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
SIGNING = "rundoc-signing-secret"
SOURCES = ["Hello world.", "The cat sat on the mat."]
GOOD = ["Hola mundo.", "El gato se sento en la alfombra."]
_SEQ = [0]
_RESUMED: list = []


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _app():
    from app.main import app

    return app


def _install_resume_stub():
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


@pytest.fixture(autouse=True)
def _resume_stubbed():
    """Per-test stub install + guaranteed restore (no leak into sibling suites)."""
    real = _install_resume_stub()
    try:
        yield
    finally:
        orchestrator.resume_run_async, orchestrator.resume_workflow = real


def _raising_transport():
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a wire-less dispatch attempted a network request to {req.url!r}")

    return httpx.MockTransport(handler)


def _ok_transport():
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def _seed_leg(*, protocol="manual"):
    """A customer + project + segmented doc + linked vendor (`protocol`), run to a PARKED
    external leg. A manual vendor rides the RAISING transport (zero I/O); a native vendor an
    endpoint + signing secret + benign 2xx transport. Returns the ids the surfaces need."""
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
        if protocol == "native":
            v = Vendor(
                name=f"V-{_seq()}",
                endpoint_url="https://vendor.example.com/intake",
                protocol="native",
                rate_card={"currency": "USD", "bands": {"new": 0.2}},
            )
            v.secret_enc = vendors._enc_secret({"signing_secret": SIGNING})
        else:
            v = Vendor(
                name=f"V-{_seq()}",
                endpoint_url="",
                protocol="manual",
                rate_card={"currency": "USD", "bands": {"new": 0.2}},
            )
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
        wid, did, vid = w.id, d.id, v.id
    transport = _ok_transport() if protocol == "native" else _raising_transport()
    vendor_dispatch.set_test_transport(transport)
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wid, did, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED, (
            "the seed must park an OPEN dispatch leg",
            leg and leg.status,
        )
        segs = orchestrator._load_segments(s, did, "es-ES")
        return {
            "doc_id": did,
            "vendor_id": vid,
            "run_id": run.id,
            "leg_id": leg.id,
            "claim": leg.claim_token,
            "seg_ids": [sg.id for sg in segs],
        }


def _targets(doc_id):
    with Session(engine) as s:
        return [sg.target_text or "" for sg in orchestrator._load_segments(s, doc_id, "es-ES")]


def _json_payload(env, targets=None, *, ids=None) -> str:
    body = {
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
    return json.dumps(body)


def _fill_xliff_targets(xliff_bytes, id_to_target):
    ns = f"{{{XLIFF_NS}}}"
    root = etree.fromstring(xliff_bytes)
    for tu in root.iter(f"{ns}trans-unit"):
        if any(etree.QName(a).localname == "alt-trans" for a in tu.iterancestors()):
            continue
        tgt = tu.find(f"{ns}target")
        if tgt is None:
            tgt = etree.SubElement(tu, f"{ns}target")
        for child in list(tgt):
            tgt.remove(child)
        tgt.text = id_to_target.get(tu.get("id"), "")
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _browser_multipart(
    fields,
    *,
    file_field="file",
    file_filename="",
    file_content=b"",
    file_ctype="application/octet-stream",
    boundary="rdbndryA1b2C3",
):
    """Hand-build a REAL-BROWSER-shaped multipart/form-data body (round-2 finding #3).

    A browser ALWAYS emits a part for a file <input> — even when the operator never chose a
    file it sends `Content-Disposition: ...; name="file"; filename=""` with empty content, and
    Starlette parses that into an UploadFile whose `.read()` is b"". TestClient's `data=`/`files=`
    never emit that empty part, so this reproduces the exact wire the bug needs. Returns
    (body_bytes, content_type). CSRF rides the client's default X-CSRF-Token header."""
    crlf = b"\r\n"
    bnd = boundary.encode()
    out = b""
    for name, val in fields.items():
        out += b"--" + bnd + crlf
        out += b'Content-Disposition: form-data; name="' + name.encode() + b'"' + crlf + crlf
        out += val.encode("utf-8") + crlf
    out += b"--" + bnd + crlf
    out += (
        b'Content-Disposition: form-data; name="'
        + file_field.encode()
        + b'"; filename="'
        + file_filename.encode()
        + b'"'
        + crlf
    )
    out += b"Content-Type: " + file_ctype.encode() + crlf + crlf
    out += file_content + crlf
    out += b"--" + bnd + b"--" + crlf
    return out, f"multipart/form-data; boundary={boundary}"


# --- web clients (login wall ON, multi-org OFF) ------------------------------
def _web_client_for(role):
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    n = _seq()
    email = f"rd-web-{n}@x.com"
    security.create_user(email, "RD Web", role, "rd-web-password-123")
    c = TestClient(_app())
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": "rd-web-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    csrf_for(c)
    return c


def _web_admin_client():
    return _web_client_for(Role.ADMIN)


def _to_status(env, status, **cols):
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        leg.status = status
        for k, val in cols.items():
            setattr(leg, k, val)
        s.add(leg)
        s.commit()


# ============================================================================
# [1] RUN VIEW
# ============================================================================
def test_run_view_wireless_leg_shows_download_and_return_form():
    env = _seed_leg(protocol="manual")
    c = _web_admin_client()
    r = c.get(f"/runs/{env['run_id']}")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    t = r.text
    xliff = f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/xliff"
    ret = f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return"
    assert xliff in t, "the Download-XLIFF href must render for a wire-less leg"
    assert f'action="{ret}"' in t, "the guarded return form action must render"
    assert 'name="next"' in t and f'value="/runs/{env["run_id"]}"' in t, (
        "the return form must carry a hidden next back to this run"
    )
    assert "Manual delivery" in t, "the honest wire-less copy must render"
    assert "Upload return" in t, "the upload button must render"
    assert "cannot be approved or resumed by hand here" not in t, (
        "the pre-manual 'cannot be done here' copy must be REPLACED for a wire-less leg"
    )
    assert env["claim"] not in t, "the claim token must never render in the page"
    print(
        "  [1] run view of a wire-less leg: download href + return form + hidden next, "
        "honest copy, no claim token: OK"
    )


def test_run_view_native_leg_keeps_old_copy_no_upload():
    env = _seed_leg(protocol="native")
    c = _web_admin_client()
    r = c.get(f"/runs/{env['run_id']}")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    t = r.text
    assert "re-enters through the vendor return path" in t, (
        "a native leg keeps the webhook/poll/vendor-page copy"
    )
    assert f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return" not in t, (
        "a native leg must NOT get the inline return form"
    )
    assert "Download XLIFF" not in t and "Upload return" not in t, (
        "a native leg must NOT get the wire-less hand-off controls"
    )
    print("  [1] run view of a NATIVE leg keeps the old copy and shows NO upload form: OK")


def test_run_view_escalated_leg_unchanged():
    env = _seed_leg(protocol="manual")
    _to_status(
        env, VendorDispatchStatus.ESCALATED, escalated_at=datetime.utcnow(), retry_contract=None
    )
    c = _web_admin_client()
    r = c.get(f"/runs/{env['run_id']}")
    assert r.status_code == 200
    t = r.text
    assert "escalated to human review" in t.lower(), (
        "an ESCALATED leg keeps its existing escalated card",
        t[:600],
    )
    assert "Download XLIFF" not in t and "Upload return" not in t, (
        "an ESCALATED leg must NOT get the wire-less hand-off controls (it is human-resumable)"
    )
    print(
        "  [1] run view of an ESCALATED wire-less leg keeps its escalated card, no "
        "hand-off controls: OK"
    )


def test_run_view_banner_surfaces_refusal_then_success():
    """Round-2 finding #1 (HIGH): the run-view upload form posts next=/runs/{id}, so a refusal
    and a success land back on THIS page with ?err=/?saved=1. The run view MUST render those
    (it did not -> every refusal vanished silently, a regression vs the vendor page). Assert the
    ACTUAL error text is VISIBLE on the landed page, not merely that the Location carried it."""
    env = _seed_leg(protocol="manual")
    _RESUMED.clear()
    c = _web_admin_client()
    nxt = f"/runs/{env['run_id']}"
    # A refusal: a payload naming an id this leg never sent -> the WHOLE return is refused.
    bad = _json_payload(env, ids=[env["seg_ids"][0], 987654])
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": bad, "next": nxt},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    landed = c.get(r.headers["location"])
    assert landed.status_code == 200, (landed.status_code, landed.text[:200])
    # a11y T7 (deliberate update): the old "Could not complete: <raw>" banner is now the
    # page error summary (role=alert, focused on load) at the top of main.
    assert 'id="page-error"' in landed.text and 'role="alert"' in landed.text, (
        "the refusal summary must render on the landed run page (not vanish silently)"
    )
    assert "never sent" in landed.text, (
        "the ACTUAL refusal reason must be visible on the page, not just in the Location header"
    )
    assert _targets(env["doc_id"]) != GOOD, "a refused return writes nothing"
    # A success: a good payload -> ?saved=1 -> the success line renders on the landed page.
    good = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _json_payload(env), "next": nxt},
        follow_redirects=False,
    )
    assert good.headers["location"] == f"{nxt}?saved=1", good.headers["location"]
    ok = c.get(good.headers["location"])
    assert ok.status_code == 200
    assert "Return accepted" in ok.text, "the success banner must render after a good return"
    assert _targets(env["doc_id"]) == GOOD, "the good return admits its targets"
    print(
        "  [1] run-view banner shows the refusal REASON (?err) and the success line "
        "(?saved) on the landed page: OK"
    )


def test_run_view_banner_escapes_reflected_segment_id():
    """The refusal reason echoes the offending segment id verbatim (vendor_return: `... names
    id {sid!r}, which dispatch N never sent`). A hand-built return can put ATTACKER BYTES in
    that id, so the id rides through ?err= onto the landed run page. The banner MUST render it
    Jinja-escaped ({{ ... }}, never `| safe`): the escaped `&lt;img` appears and the live
    `<img src=x` tag does NOT. This kills the `| safe` mutation on run_view.html's err banner."""
    env = _seed_leg(protocol="manual")
    c = _web_admin_client()
    nxt = f"/runs/{env['run_id']}"
    xss = "<img src=x onerror=alert(1)>"
    bad = _json_payload(env, ids=[env["seg_ids"][0], xss])
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": bad, "next": nxt},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    landed = c.get(r.headers["location"])
    assert landed.status_code == 200, (landed.status_code, landed.text[:200])
    assert "never sent" in landed.text, "the refusal reason (which reflects the id) must render"
    assert "&lt;img src=x" in landed.text, (
        "the reflected attacker id must appear HTML-ESCAPED on the landed page"
    )
    assert xss not in landed.text, (
        "the RAW <img> tag must NEVER reach the page -- the err banner must not be `| safe`"
    )
    assert _targets(env["doc_id"]) != GOOD, "a refused return writes nothing"
    print(
        "  [1] run-view err banner renders a reflected attacker segment id ESCAPED, never as "
        "a live tag (kills the `| safe` mutation): OK"
    )


def test_run_view_quoted_leg_is_quote_gated_not_upload():
    """Round-2 finding #2 (MED): assert_leg_admitting REFUSES a return on a QUOTED leg (only a
    DISPATCHED leg admits), so a QUOTED wire-less leg must get an HONEST quote-gate card -- not
    a doomed upload form. Download link + a link to the vendor page's quote controls stay."""
    env = _seed_leg(protocol="manual")
    _to_status(env, VendorDispatchStatus.QUOTED)
    c = _web_admin_client()
    r = c.get(f"/runs/{env['run_id']}")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    t = r.text
    ret = f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return"
    xliff = f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/xliff"
    assert f'action="{ret}"' not in t, (
        "a QUOTED leg must NOT render the doomed upload form (a return is refused until approval)"
    )
    assert "Upload return" not in t, "no upload button on a quote-gated leg"
    assert "quote-gated" in t, "the honest quote-gate copy must render for a QUOTED leg"
    assert xliff in t, "the Download-XLIFF link stays on a quote-gated leg"
    assert f'href="/vendors/{env["vendor_id"]}"' in t, (
        "a QUOTED leg must link to the vendor page's record/approve/decline quote controls"
    )
    print(
        "  [2] run view of a QUOTED wire-less leg: quote-gate copy + download + vendor-page "
        "link, and NO upload form: OK"
    )


def test_browser_empty_file_part_keeps_pasted_payload():
    """Round-2 finding #3 (MED): a real browser submits an EMPTY filename="" file part for an
    untouched file input; the route read those b"" bytes and CLOBBERED a valid PASTED payload,
    so an operator who pasted JSON got "paste ... before submitting". A browser-shaped multipart
    (empty file part + a filled payload) must now ADMIT the paste. Also guards the same latent
    bug on the vendor page (shared route)."""
    env = _seed_leg(protocol="manual")
    _RESUMED.clear()
    c = _web_admin_client()
    nxt = f"/runs/{env['run_id']}"
    body, ctype = _browser_multipart({"payload": _json_payload(env), "next": nxt})
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        content=body,
        headers={"content-type": ctype},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    assert r.headers["location"] == f"{nxt}?saved=1", (
        "the pasted payload must survive the empty browser file part (not be clobbered by b'')",
        r.headers["location"],
    )
    assert _targets(env["doc_id"]) == GOOD, "the pasted targets must land"
    assert [env["run_id"]] == _RESUMED, "the completing return resumes the run"
    print(
        '  [3] a browser-shaped multipart (empty filename="" part + pasted payload) admits '
        "the paste instead of clobbering it: OK"
    )


# ============================================================================
# [2] DOCUMENT PAGE
# ============================================================================
def test_document_page_wireless_leg_shows_download_and_handoff_link():
    env = _seed_leg(protocol="manual")
    c = _web_admin_client()
    r = c.get(f"/documents/{env['doc_id']}?locale=es-ES")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    t = r.text
    assert f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/xliff" in t, (
        "the document page must offer the Download-XLIFF link for a wire-less leg"
    )
    assert f"/runs/{env['run_id']}#vendor-handoff" in t, (
        "the document page must link into the run view's hand-off card"
    )
    assert f"vendor dispatch #{env['leg_id']}" in t, "the dispatch number must be named"
    print(
        "  [2] document page shows the Download-XLIFF link + run-view hand-off anchor for a "
        "wire-less leg: OK"
    )


def test_document_page_native_leg_and_no_leg_absent():
    # native (wired) leg -> the wire-less gate excludes it.
    env_n = _seed_leg(protocol="native")
    c = _web_admin_client()
    rn = c.get(f"/documents/{env_n['doc_id']}?locale=es-ES")
    assert rn.status_code == 200
    assert "Download XLIFF (vendor dispatch" not in rn.text, (
        "a NATIVE leg must NOT surface the wire-less download on the document page"
    )
    # a document with NO vendor leg at all -> nothing renders.
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
        d = Document(project_id=p.id, filename="plain.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale="es-ES",
                order_index=0,
                source_text="Hi.",
                status=SegStatus.NEW,
            )
        )
        s.commit()
        plain_id = d.id
    r0 = c.get(f"/documents/{plain_id}?locale=es-ES")
    assert r0.status_code == 200
    assert "Download XLIFF (vendor dispatch" not in r0.text, (
        "a document with no open vendor leg must show no hand-off controls"
    )
    print("  [2] document page: NATIVE leg and no-leg both show NO wire-less hand-off: OK")


def test_document_page_renders_each_open_wireless_leg():
    """Round-2 finding #4: a doc+locale can hold MORE THAN ONE open wire-less leg (parallel
    routing lanes). The Segments page must surface a hand-off row for EACH, not only the newest
    (which silently hid the rest)."""
    env = _seed_leg(protocol="manual")  # leg #1 (DISPATCHED, on this doc+locale)
    with Session(engine) as s:
        extra = VendorDispatch(
            vendor_id=env["vendor_id"],
            workflow_run_id=env["run_id"],
            document_id=env["doc_id"],
            target_locale="es-ES",
            claim_token=f"rd-extra-{_seq()}",
            protocol="manual",
            status=VendorDispatchStatus.DISPATCHED,
        )
        s.add(extra)
        s.commit()
        s.refresh(extra)
        extra_id = extra.id
    c = _web_admin_client()
    r = c.get(f"/documents/{env['doc_id']}?locale=es-ES")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    t = r.text
    assert f"vendor dispatch #{env['leg_id']}" in t, "the first open wire-less leg must render"
    assert f"vendor dispatch #{extra_id}" in t, (
        "a SECOND open wire-less leg on the same doc+locale must ALSO render (not be hidden)"
    )
    assert f"/vendors/{env['vendor_id']}/dispatches/{extra_id}/xliff" in t, (
        "the second leg needs its own Download-XLIFF link"
    )
    print(
        "  [4] document page renders a hand-off row for EACH open wire-less leg on the "
        "doc+locale: OK"
    )


# ============================================================================
# [3] RETURN-ROUTE next UX (open-redirect guard)
# ============================================================================
def test_return_next_honored_on_success_and_error():
    env = _seed_leg(protocol="manual")
    _RESUMED.clear()
    c = _web_admin_client()
    nxt = f"/runs/{env['run_id']}"
    # success -> next?saved=1
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _json_payload(env), "next": nxt},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    assert r.headers["location"] == f"{nxt}?saved=1", r.headers["location"]
    assert _targets(env["doc_id"]) == GOOD
    assert [env["run_id"]] == _RESUMED, "a completing return schedules the resume once"
    # a refused payload (bad segment id) -> next?err=...
    env2 = _seed_leg(protocol="manual")
    nxt2 = f"/runs/{env2['run_id']}"
    bad = _json_payload(env2, ids=[env2["seg_ids"][0], 987654])
    r = c.post(
        f"/vendors/{env2['vendor_id']}/dispatches/{env2['leg_id']}/return",
        data={"payload": bad, "next": nxt2},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    loc = r.headers["location"]
    assert loc.startswith(f"{nxt2}?err="), loc
    assert _targets(env2["doc_id"]) != GOOD, "a refused return must write nothing"
    print("  [3] return route honors next on BOTH success (?saved=1) and refusal (?err=...): OK")


def test_return_next_open_redirect_attempts_fall_back_to_vendor_page():
    env = _seed_leg(protocol="manual")
    c = _web_admin_client()
    vpath = f"/vendors/{env['vendor_id']}"
    # An EMPTY payload short-circuits to the first err branch quickly; the point is the
    # redirect TARGET, which must be the vendor page for every hostile `next`.
    for evil in (
        "//evil.com",
        "https://evil.com",
        "/\\evil.com",
        "http://evil.com/x",
        "\\\\evil.com",
        "javascript:alert(1)",
    ):
        r = c.post(
            f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
            data={"payload": "", "next": evil},
            follow_redirects=False,
        )
        assert r.status_code == 303, (evil, r.status_code)
        loc = r.headers["location"]
        assert loc.startswith(f"{vpath}?err="), (evil, loc)
        assert "evil.com" not in loc and "javascript" not in loc, (
            "a hostile next must never reach the redirect",
            evil,
            loc,
        )
    # A benign same-site path WITH its own query string composes with & (not a second ?).
    env2 = _seed_leg(protocol="manual")
    nxt = f"/documents/{env2['doc_id']}?locale=es-ES"
    r = c.post(
        f"/vendors/{env2['vendor_id']}/dispatches/{env2['leg_id']}/return",
        data={"payload": "", "next": nxt},
        follow_redirects=False,
    )
    assert (
        r.headers["location"]
        == f"{nxt}&err="
        + "paste%20the%20return%20contract%20JSON%20%28or%20choose%20a%20file%29%20before%20submitting."  # noqa: E501
    ), r.headers["location"]
    print(
        "  [3] open-redirect guard: //, scheme, backslash all fall back to the vendor page; "
        "a benign path+query composes with &: OK"
    )


def test_return_next_with_fragment_splices_query_before_anchor():
    """Round-2 finding #6: a `next` may carry a #fragment anchor. _with_query must splice the
    ?saved/?err BEFORE the fragment (/runs/1?saved=1#vendor-handoff), never after it
    (/runs/1#vendor-handoff?saved=1 -- there the '?' is part of the fragment and the banner
    never sees the param)."""
    env = _seed_leg(protocol="manual")
    _RESUMED.clear()
    c = _web_admin_client()
    nxt = f"/runs/{env['run_id']}#vendor-handoff"
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _json_payload(env), "next": nxt},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    assert r.headers["location"] == f"/runs/{env['run_id']}?saved=1#vendor-handoff", (
        "the query must splice BEFORE the #fragment",
        r.headers["location"],
    )
    print("  [3] a next carrying a #fragment gets ?saved spliced BEFORE the anchor: OK")


def test_return_default_without_next_is_byte_identical():
    env = _seed_leg(protocol="manual")
    _RESUMED.clear()
    c = _web_admin_client()
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"payload": _json_payload(env)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/vendors/{env['vendor_id']}?saved=1", (
        "with no next the redirect must be byte-identical to the prior vendor-page behaviour",
        r.headers["location"],
    )
    print("  [3] with no next field the redirect is byte-identical to the vendor page: OK")


# ============================================================================
# [4] PERMISSIONS -- a non-admin viewer gets NO controls
# ============================================================================
def test_non_admin_gets_no_handoff_controls():
    env = _seed_leg(protocol="manual")
    with Session(engine) as s:
        vname = s.get(Vendor, env["vendor_id"]).name
    mgr = _web_client_for(Role.MANAGER)
    rr = mgr.get(f"/runs/{env['run_id']}")
    assert rr.status_code == 200, (rr.status_code, rr.text[:200])
    assert "Download XLIFF" not in rr.text and "Upload return" not in rr.text, (
        "a non-admin must NOT get the run-view hand-off controls"
    )
    assert vname not in rr.text, "a non-admin must not see the vendor identity either"
    # still sees the plain external-pause banner (the run isn't silently stuck).
    assert "external vendor" in rr.text.lower()
    rd = mgr.get(f"/documents/{env['doc_id']}?locale=es-ES")
    assert rd.status_code == 200
    assert "Download XLIFF (vendor dispatch" not in rd.text, (
        "a non-admin must NOT get the document-page hand-off link"
    )
    print("  [4] a non-admin viewer gets NONE of the hand-off controls on either page: OK")


# ============================================================================
# [5] END-TO-END: a filled .xlf uploaded through the RUN-VIEW form path
# ============================================================================
def test_end_to_end_xliff_upload_through_run_view_form():
    env = _seed_leg(protocol="manual")
    _RESUMED.clear()
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        xliff = vendor_xliff.render_leg_xliff(s, leg)  # the operator DOWNLOAD
    filled = _fill_xliff_targets(
        xliff, {str(i): t for i, t in zip(env["seg_ids"], GOOD, strict=False)}
    )
    assert vendor_xliff._looks_like_xliff(filled), "the filled return must be an XLIFF body"
    c = _web_admin_client()
    nxt = f"/runs/{env['run_id']}"
    r = c.post(
        f"/vendors/{env['vendor_id']}/dispatches/{env['leg_id']}/return",
        data={"next": nxt},
        files={"file": ("return.xlf", filled, "application/xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    assert r.headers["location"] == f"{nxt}?saved=1", r.headers["location"]
    assert _targets(env["doc_id"]) == GOOD, "the uploaded XLIFF targets must land"
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        assert leg.status == VendorDispatchStatus.RETURNED, leg.status
        assert vendor_return.outstanding_ids(leg) == []
    assert [env["run_id"]] == _RESUMED, "the completing return resumes the run"
    print(
        "  [5] a filled .xlf uploaded through the RUN-VIEW form admits, lands the targets, "
        "closes the leg RETURNED, resumes: OK"
    )


if __name__ == "__main__":
    _install_resume_stub()  # script path: the autouse fixture only runs under pytest
    test_run_view_wireless_leg_shows_download_and_return_form()
    test_run_view_native_leg_keeps_old_copy_no_upload()
    test_run_view_escalated_leg_unchanged()
    test_run_view_banner_surfaces_refusal_then_success()
    test_run_view_banner_escapes_reflected_segment_id()
    test_run_view_quoted_leg_is_quote_gated_not_upload()
    test_browser_empty_file_part_keeps_pasted_payload()
    test_document_page_wireless_leg_shows_download_and_handoff_link()
    test_document_page_native_leg_and_no_leg_absent()
    test_document_page_renders_each_open_wireless_leg()
    test_return_next_honored_on_success_and_error()
    test_return_next_open_redirect_attempts_fall_back_to_vendor_page()
    test_return_next_with_fragment_splices_query_before_anchor()
    test_return_default_without_next_is_byte_identical()
    test_non_admin_gets_no_handoff_controls()
    test_end_to_end_xliff_upload_through_run_view_form()
    print("verify_run_doc_xliff_surfaces: all OK")
