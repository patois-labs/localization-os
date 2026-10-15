"""#617 product decisions from the 10-07 pressure test (the maintainer approved items 1, 2, 4, 5).

Sections (run one with `python tests/verify_decisions_617.py 1`; no argument runs all):
  [1] duplicate customer names: refused (409) on create AND rename over web, /api and CLI;
      pickers show " (#id)" for duplicates that already exist
  [2] download audit rows: prefetch requests are not audited, repeats within 60 s collapse,
      a real download is always audited, and no header suppresses any other audit row
  [4] human_review: the editor shows the pending MT read-only with a "Waiting for review on run N"
      banner linking the run
  [5] TM drift: segments whose locked TM entry changed after the fill carry a
      "TM changed since fill" badge (editor + segment API); re-running tm_match refreshes it
  [6] while a run waits on human review, every other segment writer is refused with the
      same 409 (apply-TM, plain MT, TM unlock, tag repair, image regions, FULL_TEXT
      paragraphs, key values) on web, /api (= MCP), CLI and the service seam

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). Final line: "DECISIONS 617 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_decisions617_")

from io import BytesIO

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    AuditLog,
    Customer,
    Document,
    Project,
    ProjectType,
    Role,
    Segment,
    SegmentTM,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    customers,
    orchestrator,
    prompt_tap,
    security,
    settings,
    tm,
    tm_module,
)
from tests._csrf import csrf_for

PW = "decisions617-pw-123456"


def _client():
    from app.main import app

    return TestClient(app)


def _auth_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _admin_headers():
    u = security.create_user("admin617@x.com", "admin617", Role.ADMIN, PW)
    _row, raw = apikeys.create_key(u.id, "t617", scopes="api docs")
    return {"X-Loc-Token": raw}


def _run_cli(argv):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue() + err.getvalue()


def _new_customer(name):
    with Session(engine) as s:
        return content.customer_create(s, name).id


def _raw_customer(name):
    """A customer inserted straight into the table: an 'already existing duplicate'."""
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id
    customers.invalidate_duplicate_labels()
    return cid


def _name_of(cid):
    with Session(engine) as s:
        return s.get(Customer, cid).name


# --------------------------------------------------------------------------- #
# [1] duplicate customer names
# --------------------------------------------------------------------------- #
def test_1_duplicate_customers(api, h):
    _auth_on()
    alpha = _new_customer("Rename Alpha")
    beta = _new_customer("Rename Beta")

    # /api rename onto another customer's name (case and padding ignored): 409 + existing_id
    r = api.post(f"/api/customers/{beta}/rename", json={"name": "  RENAME alpha "}, headers=h)
    assert r.status_code == 409, ("api rename duplicate", r.status_code, r.text)
    assert r.json()["existing_id"] == alpha, r.text
    assert _name_of(beta) == "Rename Beta", "a refused rename must not change the name"
    # a rename to a different casing of its OWN name is fine, and so is a fresh name
    r = api.post(f"/api/customers/{beta}/rename", json={"name": "RENAME BETA"}, headers=h)
    assert r.status_code == 200, r.text
    r = api.post(f"/api/customers/{beta}/rename", json={"name": "Rename Gamma"}, headers=h)
    assert r.status_code == 200, r.text

    # web + CLI (auth off so the form posts and the in-process CLI need no session)
    _auth_off()
    # web rename: 409 page, name unchanged
    wr = api.post(
        f"/customers/{beta}/rename", data={"name": "rename alpha"}, follow_redirects=False
    )
    assert wr.status_code == 409, ("web rename duplicate", wr.status_code, wr.text[:200])
    assert _name_of(beta) == "Rename Gamma"

    # CLI rename: usage exit, name unchanged
    rc, text = _run_cli(["customer", "rename", str(beta), "Rename ALPHA"])
    assert rc == 2 and "already" in text.lower(), ("CLI rename duplicate", rc, text)
    assert _name_of(beta) == "Rename Gamma"
    rc, text = _run_cli(["customer", "rename", str(beta), "Rename Delta"])
    assert rc == 0, (rc, text)

    # create stays refused on every surface (regression lock for the #580 behaviour)
    _auth_on()
    r = api.post("/api/customers", json={"name": "rename ALPHA"}, headers=h)
    assert r.status_code == 409 and r.json()["existing_id"] == alpha, r.text
    _auth_off()
    wr = api.post("/customers", data={"name": "Rename Alpha"}, follow_redirects=False)
    assert wr.status_code == 409, wr.status_code
    rc, text = _run_cli(["customer", "create", "Rename Alpha"])
    assert rc == 2 and "already" in text.lower(), (rc, text)

    # MCP create_project resolves the customer by name through GET /api/customers?name= (the
    # same casefold+trim match the 409 uses) before it would ever POST, so it never mints a twin.
    _auth_on()
    r = api.get("/api/customers", params={"name": "  RENAME alpha", "archived": "all"}, headers=h)
    assert [c["id"] for c in r.json()["customers"]] == [alpha], r.text
    _auth_off()

    # pickers: a duplicate that already exists shows " (#id)"; unique names stay bare
    d1 = _raw_customer("Twin Co")
    d2 = _raw_customer("twin co ")
    uniq = _new_customer("Solo Co")
    customers.invalidate_duplicate_labels()
    for path in ("/projects", "/tm", "/customers"):
        page = api.get(path).text
        assert f"(#{d1})" in page and f"(#{d2})" in page, (path, "duplicate ids shown")
        assert f"Solo Co (#{uniq})" not in page, (path, "a unique name stays bare")
    assert customers.customer_label(Customer(id=uniq, name="Solo Co")) == "Solo Co"
    assert customers.customer_label(None) == ""
    assert customers.customer_label({"id": d1, "name": "Twin Co"}) == f"Twin Co (#{d1})"
    print(
        "  [1] duplicate customers: 409 on create + rename (web, /api, CLI); pickers show (#id) OK"
    )


# --------------------------------------------------------------------------- #
# [2] download audit rows
# --------------------------------------------------------------------------- #
def _tap_rows():
    from sqlmodel import select

    with Session(engine) as s:
        return [
            (a.actor, a.detail)
            for a in s.exec(select(AuditLog).where(AuditLog.action == "prompt_tap.download"))
        ]


def _capture_tap(n, doc):
    for i in range(n):
        prompt_tap.capture(
            prompt_tap.build_record(
                provider="mock",
                provider_kind="mock",
                model="m",
                task="translate",
                system="sys",
                prompt=f"p{doc}-{i}",
                max_tokens=64,
                payload={"text": f"p{doc}-{i}"},
                outcome="ok",
                response_text=f"out-{i}",
                usage={"input_tokens": 1},
                tap_meta={"document_id": doc},
            )
        )


def _login(email, pw):
    c = _client()
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def test_2_download_audit(api, h):
    from app.web import admin_common

    admin_common.reset_download_audit_memory()
    _auth_on()
    prompt_tap.clear()
    settings.set_many({"prompt_tap_enabled": "1"})
    _capture_tap(3, 71)
    _capture_tap(2, 72)
    security.create_user("tapadmin1@x.com", "tapadmin1", Role.ADMIN, PW)
    security.create_user("tapadmin2@x.com", "tapadmin2", Role.ADMIN, PW)
    c1 = _login("tapadmin1@x.com", PW)
    c2 = _login("tapadmin2@x.com", PW)
    base = len(_tap_rows())

    # a prefetch of the link (any of the four header spellings) delivers nothing and audits nothing
    for hdr in (
        {"Sec-Purpose": "prefetch"},
        {"Sec-Purpose": "prefetch;prerender"},
        {"Purpose": "Prefetch"},
        {"X-Moz": "prefetch"},
        {"X-Purpose": "prefetch"},
    ):
        r = c1.get("/prompt-tap/download?document_id=71", headers=hdr)
        assert r.status_code == 503, (hdr, r.status_code)
        assert r.text == "" and "no-store" in r.headers.get("cache-control", ""), hdr
    assert len(_tap_rows()) == base, "prefetch requests must write no audit row"

    # a header with another value is not a prefetch: that is a real download and is audited
    r = c1.get("/prompt-tap/download?document_id=71", headers={"Purpose": "navigate"})
    assert r.status_code == 200 and len(_tap_rows()) == base + 1, (r.status_code, _tap_rows())

    # the same actor repeating the same selection within the window collapses
    for _ in range(3):
        assert c1.get("/prompt-tap/download?document_id=71").status_code == 200
    assert len(_tap_rows()) == base + 1, "repeats within 60 s write no extra row"
    # a different selection, a different format and a different actor each get their own row
    assert c1.get("/prompt-tap/download?document_id=72").status_code == 200
    assert c1.get("/prompt-tap/download?document_id=71&format=json").status_code == 200
    assert c2.get("/prompt-tap/download?document_id=71").status_code == 200
    assert len(_tap_rows()) == base + 4, _tap_rows()

    # after the window the same download is audited again (clock moved, not slept)
    t0 = 1000.0
    admin_common.reset_download_audit_memory()
    assert admin_common.download_audit_due("a", "t", now=t0) is True
    assert admin_common.download_audit_due("a", "t", now=t0 + 59.9) is False
    assert admin_common.download_audit_due("a", "t", now=t0 + 60.1) is True
    assert admin_common.download_audit_due("b", "t", now=t0 + 61) is True
    # a partial transfer always writes its row and never opens the window
    admin_common.reset_download_audit_memory()
    assert admin_common.download_audit_due("a", "t", now=t0, complete=False) is True
    assert admin_common.download_audit_due("a", "t", now=t0 + 1, complete=False) is True
    assert admin_common.download_audit_due("a", "t", now=t0 + 2) is True
    # the prefetch test is exact about its headers
    assert admin_common.is_prefetch_request({"sec-purpose": "prefetch;anonymous-client-ip"})
    assert not admin_common.is_prefetch_request({"purpose": "download"})
    assert not admin_common.is_prefetch_request({})

    # a client header never suppresses any OTHER audit row (rename audits as always)
    admin_common.reset_download_audit_memory()
    _auth_off()
    cid = _new_customer("Audit Header Co")
    r = api.post(
        f"/customers/{cid}/rename",
        data={"name": "Audit Header Co 2"},
        headers={"Sec-Purpose": "prefetch", "Purpose": "prefetch"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    from sqlmodel import select

    with Session(engine) as s:
        got = s.exec(
            select(AuditLog).where(
                AuditLog.action == "customer.rename", AuditLog.target == f"customer {cid}"
            )
        ).all()
    assert len(got) == 1, "a prefetch header must not suppress a rename audit row"
    print(
        "  [2] download audit: prefetch gets 503 and no row, repeats collapse in 60 s, real kept OK"
    )


# --------------------------------------------------------------------------- #
# shared builders for [4] and [5]
# --------------------------------------------------------------------------- #
def _docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def _new_project(client, name, targets="fr-FR"):
    r = client.post(
        "/projects",
        data={
            "customer_id": str(_new_customer(name + " customer")),
            "name": name,
            "source_locale": "en-US",
            "target_locales": targets,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(client, pid, paragraphs):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                _docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _workflow(name, steps):
    """steps: [(name, StepType, prompt_text)] in order."""
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, (sname, stype, prompt) in enumerate(steps):
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    name=sname,
                    step_type=stype,
                    provider="mock",
                    prompt_text=prompt,
                    chunk_policy="auto",
                    retry_max=1,
                    backoff_base=0.0,
                )
            )
        s.commit()
        return wf.id


def _segments(doc_id, locale="fr-FR"):
    from sqlmodel import select

    with Session(engine) as s:
        return sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all(),
            key=lambda x: x.order_index,
        )


# --------------------------------------------------------------------------- #
# [4] human review step: pending MT visible, read-only
# --------------------------------------------------------------------------- #
def test_4_human_review_pending(api, h):
    _auth_off()
    wf = _workflow(
        "P617 review",
        [
            ("Translator", StepType.LLM_AGENT, "Translate: {text}"),
            ("Human", StepType.HUMAN_REVIEW, ""),
        ],
    )
    pid = _new_project(api, "P617 review project")
    doc = _upload(api, pid, ["Alpha sentence.", "Beta sentence."])
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf, doc, "fr-FR")
    assert r["status"] == "paused", r
    run_id = r["run_id"]
    with Session(engine) as s:
        view = orchestrator.paused_review(s, run_id)
        waiting = orchestrator.waiting_human_review(s, doc, "fr-FR")
        assert orchestrator.waiting_human_review(s, doc, "de-DE") is None
    assert waiting and waiting["run_id"] == run_id and waiting["segments"] == view["segments"]
    pending = view["segments"]
    assert len(pending) == 2 and all(p.strip() for p in pending), pending

    # the editor: banner naming the run, linking it, the pending text read-only, no edit controls
    page = api.get(f"/documents/{doc}/editor?locale=fr-FR").text
    assert f'Waiting for review on run <a href="/runs/{run_id}">{run_id}</a>' in page, "banner"
    assert "data-waiting-review" in page
    for text in pending:
        assert text in page, ("pending MT is shown", text)
    assert page.count("data-pending-mt data-number") == 2, "one read-only block per segment"
    assert 'class="tgt-edit"' not in page, "no editable textarea while the run waits"
    assert 'data-action="approve"' not in page, "no approve button while the run waits"

    # headless surfaces: same facts on /api and the CLI
    _auth_on()
    r = api.get(f"/api/documents/{doc}/segments?detail=segments", headers=h)
    assert r.status_code == 200, r.text
    loc = r.json()["locales"]["fr-FR"]
    assert loc["awaiting_review"] == {"run_id": run_id, "step": "Human"}, loc
    assert [d["pending_mt"] for d in loc["segment_details"]] == pending, loc
    r = api.get(f"/api/documents/{doc}/segments", headers=h)
    assert r.json()["locales"]["fr-FR"]["awaiting_review"]["run_id"] == run_id
    _auth_off()
    rc, text = _run_cli(["doc", "show", str(doc)])
    assert rc == 0 and f"waiting for review on run {run_id}" in text, (rc, text)

    # no surface can edit or approve while the run waits (a stale tab, a script, the CLI)
    seg0 = _segments(doc)[0].id
    r = api.post(f"/segments/{seg0}/save", data={"target_text": "Sneaky edit."})
    assert r.status_code == 409, (r.status_code, r.text)
    r = api.post(f"/segments/{seg0}/approve")
    assert r.status_code == 409, (r.status_code, r.text)
    _auth_on()
    r = api.post(f"/api/segments/{seg0}/approve", headers=h)
    assert r.status_code == 409, (r.status_code, r.text)
    _auth_off()
    rc, text = _run_cli(["doc", "approve-segment", str(seg0)])
    assert rc != 0 and "waiting for a human review" in text, (rc, text)
    assert (_segments(doc)[0].target_text or "") != "Sneaky edit."
    assert _segments(doc)[0].status != SegStatus.APPROVED

    # approve and resume behave as today, and the editor goes back to normal
    edited = orchestrator.SEG_JOINER.join(["Premiere phrase.", "Seconde phrase."])
    with Session(engine) as s:
        res = orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
    assert res["status"] == "completed", res
    assert [x.target_text for x in _segments(doc)] == ["Premiere phrase.", "Seconde phrase."]
    page = api.get(f"/documents/{doc}/editor?locale=fr-FR").text
    assert "data-waiting-review" not in page and "Waiting for review" not in page
    assert page.count('class="tgt-edit"') == 2 and "data-pending-mt data-number" not in page
    _auth_on()
    r = api.get(f"/api/documents/{doc}/segments?detail=segments", headers=h)
    loc = r.json()["locales"]["fr-FR"]
    assert "awaiting_review" not in loc and "pending_mt" not in loc["segment_details"][0], loc
    _auth_off()
    print(
        "  [4] human review: editor shows pending MT read-only + run banner; resume restores it OK"
    )


# --------------------------------------------------------------------------- #
# [6] the review hold refuses EVERY other segment writer (Review gate)
# --------------------------------------------------------------------------- #
def _mcp_routes():
    import importlib.util

    path = os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
    spec = importlib.util.spec_from_file_location("p617_mcp_registry", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve the module by name
    spec.loader.exec_module(mod)
    return {t.name: set(t.routes) for t in mod.TOOLS}


def _is_409(r, what):
    assert r.status_code == 409, (what, r.status_code, r.text[:300])
    if r.headers.get("content-type", "").startswith("application/json"):
        body = r.json()
        assert body.get("code") == "awaiting_review" or body.get("reason") == "awaiting_review", (
            what,
            body,
        )


def _pause(api, name, *, mode="segmented", paragraphs=("Alpha sentence.", "Beta sentence.")):
    wf = _workflow(
        f"{name} wf",
        [
            ("Translator", StepType.LLM_AGENT, "Translate: {text}"),
            ("Human", StepType.HUMAN_REVIEW, ""),
        ],
    )
    pid = _new_project(api, name)
    if mode == "segmented":
        doc = _upload(api, pid, list(paragraphs))
    else:
        r = api.post(
            f"/projects/{pid}/documents",
            data={"mode": mode},
            files={
                "file": (
                    "f.docx",
                    _docx_from(list(paragraphs)),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            follow_redirects=False,
        )
        doc = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf, doc, "fr-FR")
    assert r["status"] == "paused", r
    return wf, pid, doc, r["run_id"]


def _snapshot(doc_id):
    return [(x.target_text, x.status, x.lock_band, dict(x.meta or {})) for x in _segments(doc_id)]


def test_6_hold_covers_every_writer(api, h):
    from sqlmodel import select

    from app.services import fulltext_paragraphs as ftp
    from app.services import image_localization as il
    from app.services import keys as keys_svc
    from app.services import segment_review, tag_llm_repair, tm_lock, translate

    _auth_off()
    wf, pid, doc, run_id = _pause(api, "P617 hold")
    segs = _segments(doc)
    with Session(engine) as s:
        # a 100% TM match for the first source, so an unguarded apply-tm WOULD fill it
        tm.write_back(s, pid, "en-US", "fr-FR", segs[0].source_text, "Phrase TM alpha.")
        s.commit()
    before = _snapshot(doc)

    # MCP rides the same /api routes this section drives (no MCP-only writer exists)
    routes = _mcp_routes()
    assert ("POST", "/api/documents/{doc_id}/apply-tm") in routes["apply_tm"]
    assert ("POST", "/api/segments/{sid}/approve") in routes["approve_segment"]
    assert ("POST", "/api/documents/{doc_id}/translate") in routes["submit_translation"]
    assert ("POST", "/api/images/{doc_id}/translate") in routes["translate_image"]
    assert ("PUT", "/api/keys/{key_id}/values/{locale}") in routes["upsert_keys"]

    # apply-TM: web, /api, CLI, service
    _is_409(api.post(f"/documents/{doc}/apply-tm", data={"locale": "fr-FR"}), "web apply-tm")
    _auth_on()
    _is_409(
        api.post(f"/api/documents/{doc}/apply-tm", json={"locale": "fr-FR"}, headers=h),
        "api apply-tm",
    )
    _auth_off()
    rc, text = _run_cli(["doc", "apply-tm", str(doc), "fr-FR"])
    assert rc != 0 and "waiting for a human review" in text, (rc, text)
    with Session(engine) as s:
        d, p = s.get(Document, doc), s.get(Project, pid)
        try:
            content.apply_tm_to_document(s, d, p, "fr-FR")
            raise AssertionError("apply_tm_to_document wrote while a run waits on review")
        except segment_review.AwaitingReviewError:
            pass

    # plain MT: web stream, web job, /api translate (+ generic job worker), CLI, service
    _is_409(api.post(f"/documents/{doc}/translate-stream", data={"locale": "fr-FR"}), "stream")
    _is_409(api.post(f"/documents/{doc}/translate", data={"locale": "fr-FR"}), "web translate")
    _auth_on()
    _is_409(
        api.post(
            f"/api/documents/{doc}/translate",
            json={"targets": ["fr-FR"], "provider": "mock"},
            headers=h,
        ),
        "api translate",
    )
    _auth_off()
    rc, text = _run_cli(["doc", "translate", str(doc), "--locale", "fr-FR"])
    assert rc != 0 and "waiting for a human review" in text, (rc, text)
    with Session(engine) as s:
        try:
            list(translate.pretranslate_stream(s, doc, "fr-FR"))
            raise AssertionError("pretranslate_stream wrote while a run waits on review")
        except segment_review.AwaitingReviewError:
            pass
    from app.services import jobs

    try:
        jobs._run_translate_document({"document_id": doc, "locale": "fr-FR"}, lambda *a: None)
        raise AssertionError("a queued translate job wrote while a run waits on review")
    except jobs.JobFailed as exc:
        assert "waiting for a human review" in str(exc), exc

    # TM unlock: web, /api, CLI (a locked row stays locked)
    with Session(engine) as s:
        seg = s.get(Segment, segs[0].id)
        seg.lock_band, seg.lock_hard = "exact", False
        s.add(seg)
        s.commit()
    _is_409(api.post(f"/segments/{segs[0].id}/unlock"), "web unlock")
    _auth_on()
    _is_409(api.post(f"/api/segments/{segs[0].id}/unlock", headers=h), "api unlock")
    _auth_off()
    rc, text = _run_cli(["doc", "unlock-segment", str(segs[0].id)])
    assert rc != 0 and "waiting for a human review" in text, (rc, text)
    assert _segments(doc)[0].lock_band == "exact", "the lock is untouched"
    with Session(engine) as s:
        seg = s.get(Segment, segs[0].id)
        tm_lock.clear_lock(seg)
        s.add(seg)
        s.commit()

    # inline-tag repair: web route 409, the sweep skips the held document
    with Session(engine) as s:
        seg = s.get(Segment, segs[1].id)
        prev = seg.target_text
        seg.target_text = "Beta ⟦r1⟧ phrase."
        s.add(seg)
        s.commit()
    _is_409(api.post(f"/segments/{segs[1].id}/tag-repair"), "web tag-repair")
    settings.set_many({"tag_llm_repair_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        rep = tag_llm_repair.repair_stored_segments(s, doc_id=doc, plan_only=True)
    notes = [r["note"] for r in rep["segments"] if r["segment_id"] == segs[1].id]
    assert notes == ["skipped: document is waiting for a human review step"], rep["segments"]
    with Session(engine) as s:
        seg = s.get(Segment, segs[1].id)
        seg.target_text = prev
        s.add(seg)
        s.commit()

    # image region save + image translate (web, /api, service) on a held image document
    with Session(engine) as s:
        ip = s.get(Project, pid)
        svg = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">'
            b'<text x="16" y="40">Summer Sale</text></svg>\n'
        )
        img = il.ingest_image(s, ip, "p617.svg", svg, actor="verify").id
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf, img, "fr-FR")
    assert r["status"] == "paused", r
    img_seg = _segments(img)[0]
    img_before = _snapshot(img)
    _is_409(
        api.post(
            f"/images/{img}/regions/{img_seg.id}",
            data={"target_text": "Sneaky.", "locale": "fr-FR"},
        ),
        "web image region save",
    )
    r = api.post(f"/images/{img}/translate", data={"locale": "fr-FR"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"], (r.status_code, r.headers)
    _auth_on()
    _is_409(
        api.post(f"/api/images/{img}/translate", json={"locale": "fr-FR"}, headers=h),
        "api image translate",
    )
    _auth_off()
    with Session(engine) as s:
        try:
            il.translate_image(s, s.get(Document, img), "fr-FR", actor="verify")
            raise AssertionError("translate_image wrote while a run waits on review")
        except il.ImageLocalizationError as exc:
            assert "waiting for a human review" in str(exc), exc
    assert _snapshot(img) == img_before, "image segments unchanged"

    # FULL_TEXT paragraph save / revert: web, /api, CLI, service
    _fwf, _fpid, fdoc, _frun = _pause(api, "P617 hold ft", mode="full_text")
    f_before = _snapshot(fdoc)
    _is_409(
        api.post(
            f"/documents/{fdoc}/paragraphs/0/save", data={"locale": "fr-FR", "text": "Sneaky."}
        ),
        "web paragraph save",
    )
    _is_409(
        api.post(f"/documents/{fdoc}/paragraphs/0/revert", data={"locale": "fr-FR"}),
        "web paragraph revert",
    )
    _is_409(
        api.post(f"/documents/{fdoc}/paragraphs/revert-all-damaged", data={"locale": "fr-FR"}),
        "web revert all",
    )
    _auth_on()
    _is_409(
        api.post(
            f"/api/documents/{fdoc}/paragraphs/0/save?locale=fr-FR",
            json={"text": "Sneaky."},
            headers=h,
        ),
        "api paragraph save",
    )
    _auth_off()
    rc, text = _run_cli(
        [
            *("doc", "set-paragraph", "--doc", str(fdoc), "--locale", "fr-FR"),
            *("--index", "0", "--text", "Sneaky."),
        ]
    )
    assert rc != 0 and "waiting for a human review" in text, (rc, text)
    with Session(engine) as s:
        res = ftp.save_paragraph(s, s.get(Document, fdoc), "fr-FR", 0, "Sneaky.", actor="t")
    assert res["reason"] == "awaiting_review", res
    assert _snapshot(fdoc) == f_before, "full-text segment unchanged"

    # key projects: grid (web), /api PUT (MCP upsert_keys), CLI keys set, service
    with Session(engine) as s:
        kp = content.project_create(
            s,
            "P617 keys",
            source_locale="en-US",
            target_locales="fr-FR",
            project_type=ProjectType.KEY_BASED,
            customer_id=_new_customer("P617 keys customer"),
        )
        kpid = kp.id
        key = keys_svc.create_key(s, kp, "greet", base_value="Hello")
        key_id = key.id
        key_doc = keys_svc.values_for(s, key, "fr-FR")[0].document_id
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf, key_doc, "fr-FR")
    assert r["status"] == "paused", r
    k_before = _snapshot(key_doc)
    r = api.post(
        f"/projects/{kpid}/keys/{key_id}/value",
        data={"locale": "fr-FR", "text": "Sneaky."},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303) and "err=" in r.headers.get("location", ""), (
        r.status_code,
        r.headers,
    )
    ku = security.create_user("keys617@x.com", "keys617", Role.ADMIN, PW)
    _row, kraw = apikeys.create_key(ku.id, "t617k", scopes="api keys")
    _auth_on()
    _is_409(
        api.put(
            f"/api/keys/{key_id}/values/fr-FR",
            json={"text": "Sneaky."},
            headers={"X-Loc-Token": kraw},
        ),
        "api key value",
    )
    _auth_off()
    rc, text = _run_cli(
        [
            *("keys", "set", "--project", str(kpid), "--name", "greet"),
            *("--locale", "fr-FR", "--text", "Sneaky."),
        ]
    )
    assert rc != 0 and "waiting for a human review" in text, (rc, text)
    with Session(engine) as s:
        try:
            keys_svc.set_value(
                s, s.get(type(key), key_id), "fr-FR", "Sneaky.", origin=keys_svc.ORIGIN_HUMAN
            )
            raise AssertionError("keys.set_value wrote while a run waits on review")
        except keys_svc.KeyServiceError as exc:
            assert getattr(exc, "status", None) == 409, exc
    assert _snapshot(key_doc) == k_before, "key values unchanged"

    # nothing above changed the held document; once resumed, every writer works again
    assert _snapshot(doc) == before, "held document unchanged"
    edited = orchestrator.SEG_JOINER.join(["Premiere phrase.", "Seconde phrase."])
    with Session(engine) as s:
        assert orchestrator.resume_workflow(s, run_id, "fr-FR", edited)["status"] == "completed"
    r = api.post(f"/documents/{doc}/apply-tm", data={"locale": "fr-FR"}, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text[:200])
    with Session(engine) as s:
        assert s.exec(select(Segment.id).where(Segment.document_id == doc)).first() is not None
    print("  [6] review hold: apply-TM, MT, unlock, tag repair, images, paragraphs, keys refused")


# --------------------------------------------------------------------------- #
# [5] TM drift badge
# --------------------------------------------------------------------------- #
def test_5_tm_drift(api, h):
    from sqlmodel import select

    _auth_off()
    settings.set_many({"tm_hundreds_enter_lqa": "1"})
    with Session(engine) as s:
        p = Project(name="P617 drift", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        tm.write_back(s, p.id, "Save changes.", "Enregistrer.", "en-US", "fr-FR")
        tm.write_back(s, p.id, "Close window.", "Fermer.", "en-US", "fr-FR")
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(["Save changes.", "Close window.", "Brand new line."]):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=src,
                    target_text="",
                    status=SegStatus.NEW,
                )
            )
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        doc_id, pid = d.id, p.id
        entry_save = s.exec(select(SegmentTM).where(SegmentTM.source_text == "Save changes.")).one()
        entry_close = s.exec(
            select(SegmentTM).where(SegmentTM.source_text == "Close window.")
        ).one()
        save_id, close_id = entry_save.id, entry_close.id
    wf = _workflow(
        "P617 tm",
        [("TM match", StepType.TM_MATCH, ""), ("MT", StepType.LLM_AGENT, "Translate: {text}")],
    )
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf, doc_id, "fr-FR")
    assert r["status"] == "completed", r
    segs = _segments(doc_id)
    assert [x.target_text for x in segs[:2]] == ["Enregistrer.", "Fermer."], segs
    assert segs[0].lock_band and segs[1].lock_band

    def page():
        return api.get(f"/documents/{doc_id}/editor?locale=fr-FR").text

    def detail():
        _auth_on()
        try:
            rr = api.get(f"/api/documents/{doc_id}/segments?detail=segments", headers=h)
        finally:
            _auth_off()
        return rr.json()["locales"]["fr-FR"]["segment_details"]

    # nothing edited yet: no badge anywhere (a fresh fill, and a pure usage bump, are not drift)
    assert 'id="drift-' not in page(), "no badge before any edit"
    with Session(engine) as s:
        tm_module.bump_usage(s, [close_id], project_id=pid)
    assert all("tm_changed" not in (row.get("lock") or {}) for row in detail())

    # edit one TM entry after the fill: only that segment is flagged (editor + API + CLI)
    with Session(engine) as s:
        tm_module.edit_entry(s, save_id, "Sauvegarder.")
    html = page()
    assert f'id="drift-{segs[0].id}"' in html and "TM changed since fill" in html, "badge shown"
    assert f'id="drift-{segs[1].id}"' not in html and f'id="drift-{segs[2].id}"' not in html
    rows = detail()
    assert rows[0]["lock"]["tm_changed"] is True and rows[0]["lock"]["tm_changed_at"], rows[0]
    assert "tm_changed" not in rows[1]["lock"], rows[1]
    assert "lock" not in rows[2], rows[2]
    rc, text = _run_cli(["--json", "doc", "show", str(doc_id), "--detail"])
    assert rc == 0 and '"tm_changed": true' in text.replace('":true', '": true'), (rc, text[:300])
    rc, text = _run_cli(["doc", "show", str(doc_id), "--detail"])
    assert rc == 0 and "TM changed since fill" in text, (rc, text)

    # the badge is translated
    c = _client()
    csrf_for(c)
    c.post("/account/locale", data={"ui_locale": "es"}, follow_redirects=False)
    es_page = c.get(f"/documents/{doc_id}/editor?locale=fr-FR").text
    assert "TM modificada desde el relleno" in es_page, "es badge"

    # re-running tm_match refreshes the fill and clears the badge
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf, doc_id, "fr-FR")
    assert r["status"] == "completed", r
    segs = _segments(doc_id)
    assert [x.target_text for x in segs[:2]] == ["Sauvegarder.", "Fermer."], segs
    assert segs[0].lock_band, "the refreshed fill is locked again"
    assert f'id="drift-{segs[0].id}"' not in page(), "badge cleared after the refresh"
    assert all("tm_changed" not in (row.get("lock") or {}) for row in detail())

    # a human-final (approved) segment keeps its text and its lock; it only shows the badge
    with Session(engine) as s:
        seg = s.get(Segment, segs[1].id)
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
        tm_module.edit_entry(s, close_id, "Clore.")
    assert f'id="drift-{segs[1].id}"' in page(), "approved row still flags the drift"
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf, doc_id, "fr-FR")
    segs = _segments(doc_id)
    assert segs[1].target_text == "Fermer." and segs[1].lock_band, (
        "approved text is never refreshed"
    )
    print("  [5] TM drift: badge on editor, API and CLI; tm_match re-run refreshes OK")


def test_7_hold_covers_repeat_propagation(api, h):
    """#617 item 6 x item 4 (gate): repeat propagation, its undo and link/unlink are segment
    writers too. Under a human_review hold every surface answers 409 awaiting_review (web, /api
    and so MCP, CLI) and the seam itself writes nothing."""
    from app.services import segment_review

    _auth_off()
    _wf, _pid, doc, _run_id = _pause(
        api, "P617 hold repeats", paragraphs=("Gamma sentence.", "Gamma sentence.", "Other.")
    )
    segs = _segments(doc)
    with Session(engine) as s:
        src = s.get(Segment, segs[0].id)
        src.target_text = "Phrase gamma."
        src.meta = {
            "last_propagation": {
                "ids": [segs[1].id],
                "text": "Phrase gamma.",
                "prev": {str(segs[1].id): {"t": "", "s": "new", "q": None, "f": None}},
            }
        }
        s.add(src)
        s.commit()
    before = _snapshot(doc)

    routes = _mcp_routes()
    assert ("POST", "/api/segments/{sid}/propagation") in routes["approve_segment"]
    sid = segs[0].id
    for action in ("undo", "unlink", "link"):
        _is_409(api.post(f"/segments/{sid}/propagation", data={"action": action}), f"web {action}")
    _auth_on()
    for body in ({"undo": True}, {"linked": False}, {"linked": True}):
        _is_409(api.post(f"/api/segments/{sid}/propagation", json=body, headers=h), f"api {body}")
    _auth_off()
    for flag in ("--undo", "--unlink", "--link"):
        rc, text = _run_cli(["doc", "segment-propagation", str(sid), flag])
        assert rc != 0 and "waiting for a human review" in text, (flag, rc, text)
    with Session(engine) as s:
        seg, d = s.get(Segment, sid), s.get(Document, doc)
        out = segment_review.propagate_repeats(s, seg, d)
        assert out["propagated"] == [] and "human review" in (out["skipped"] or ""), out
        for fn, kw in (
            (segment_review.undo_propagation, {}),
            (segment_review.set_propagation, {"linked": False}),
        ):
            try:
                fn(s, seg, d, **kw)
                raise AssertionError(f"{fn.__name__} wrote while a run waits on review")
            except segment_review.AwaitingReviewError:
                pass
    assert _snapshot(doc) == before, "nothing written under the hold"
    print("  [7] hold: repeat propagation, undo, link/unlink refused on web, /api, CLI, seam OK")


SECTIONS = {
    "1": test_1_duplicate_customers,
    "2": test_2_download_audit,
    "4": test_4_human_review_pending,
    "5": test_5_tm_drift,
    "6": test_6_hold_covers_every_writer,
    "7": test_7_hold_covers_repeat_propagation,
}


def main():
    which = set(sys.argv[1:])
    init_db()
    _auth_off()
    h = _admin_headers()
    api = _client()
    for key, fn in SECTIONS.items():
        if not which or key in which:
            fn(api, h)
    print("DECISIONS 617 OK")


if __name__ == "__main__":
    main()
