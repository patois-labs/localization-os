"""Pinning tests for the 2026-08-08 full-platform test-campaign hardening train.

Nine input-validation / error-handling fixes, each landed at a SHARED SERVICE SEAM so
web + /api + CLI inherit it. Every test asserts the POST-FIX behaviour (a clean 4xx or a
service-level raise) that the pre-fix code did not produce (a 202/200/500/silent-clamp),
so the whole file is red on `main` and green on the branch.

  1. LOCALE-VALIDATION  — the two translate seams catalog-validate the target BEFORE the
     auto-register/spend loop (junk -> 400/422, NO TargetLocale registered, NO job
     enqueued); the /api project-create list-shaped target_locales fr-FR regression.
  2. STEP-TYPE-WHITELIST — add_step/update_step reject an unknown step_type at the seam.
  3. MISSING-BLOB-500   — the download + export routes map a missing on-disk blob
     (FileNotFoundError/OSError) to a clean 409, never a 500.
  4. EMAIL-VALIDATION   — security.create_user / create_invite reject a malformed email.
  5. NAME-LENGTH-CAP    — the four name seams reject an over-512-char name (422).
  6. NON-STRING-NAME    — the name seams reject a non-string name (422), not str()-coerce.
  7. ROUTING-PRIORITY   — /api/routing/rules 400s a non-int priority (no silent 100).
  8. LQA-THRESHOLD      — an out-of-band lqa_threshold is a 400, not a silent clamp; NULL
     still inherits the default (byte-identical).
  9. ASSIST-ASK-KEYLESS — assist.ask pre-guards a non-live engine with a 409, not a 502.

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider, deterministic + free). Final line: "TEST CAMPAIGN FIXES OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_testcampaign_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Job,
    Role,
    Segment,
    StepType,
    TargetLocale,
    User,
    WorkflowStep,
)
from app.services import (
    apikeys,
    assist,
    content,
    content_types,
    ids,
    security,
    settings,
)
from app.services import workflows as wf_svc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _h(raw):
    return {"X-Loc-Token": raw}


def _counts(project_id):
    """(TargetLocale rows for the project, total Job rows) — the no-spend witnesses."""
    with Session(engine) as s:
        tl = len(s.exec(select(TargetLocale.id).where(TargetLocale.project_id == project_id)).all())
        jobs = len(s.exec(select(Job.id)).all())
    return tl, jobs


def _seed():
    init_db()
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "assist_enabled": "1"}
    )
    settings.invalidate_cache()
    admin = security.create_user("tc-admin@x.com", "TC Admin", Role.ADMIN, "tc-password-12345")
    _row, raw = apikeys.create_key(admin.id, "tc-key", scopes="api docs keys admin")
    with Session(engine) as s:
        cust = content.customer_create(s, "TC Customer", creator=s.get(User, admin.id))
        cust_id = cust.id
        # A document project on the offline mock (so a VALID translate enqueues rather
        # than 409-ing on a keyless real engine). de-DE + fr-FR are both enabled catalog
        # codes.
        proj = content.project_create(
            s,
            "TC Doc Project",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_id,
            creator=s.get(User, admin.id),
            default_provider="mock",
        )
        proj_id = proj.id
        doc = Document(
            project_id=proj_id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        doc_id = doc.id
        # One translated de-DE segment so the download route reaches build_translated_file
        # (it 404s if the locale has no segments).
        s.add(
            Segment(
                document_id=doc_id,
                target_locale="de-DE",
                order_index=0,
                source_text="Hello",
                target_text="Hallo",
                status="translated",
            )
        )
        s.commit()
    return {"token": raw, "cust_id": cust_id, "proj_id": proj_id, "doc_id": doc_id}


def _client():
    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. LOCALE-VALIDATION
# --------------------------------------------------------------------------- #
def test_locale_validation(d, api):
    tok = _h(d["token"])
    doc_id, proj_id = d["doc_id"], d["proj_id"]

    # --- document translate: junk target rejected PRE-SPEND -----------------
    before = _counts(proj_id)
    r = api.post(
        f"/api/documents/{doc_id}/translate", json={"targets": ["xx-NONSENSE"]}, headers=tok
    )
    assert r.status_code == 400, (
        f"junk locale must 400 on doc-translate, got {r.status_code} {r.text}"
    )
    assert _counts(proj_id) == before, "a rejected junk locale registered a TargetLocale or a job"

    # a path-traversal string is also just an unknown catalog code -> 400, pre-spend
    r = api.post(
        f"/api/documents/{doc_id}/translate", json={"targets": ["../etc/passwd"]}, headers=tok
    )
    assert r.status_code == 400, r.text
    assert _counts(proj_id) == before, "'../etc/passwd' polluted the project"

    # a NON-STRING element (nested list / int) is a 422 type error, pre-spend
    for junk in ([["fr"]], [999]):
        r = api.post(f"/api/documents/{doc_id}/translate", json={"targets": junk}, headers=tok)
        assert r.status_code == 422, f"non-string target must 422, got {r.status_code} for {junk!r}"
        assert _counts(proj_id) == before, "a non-string target polluted the project"

    # a VALID enabled locale still translates (202 + a job enqueued)
    r = api.post(f"/api/documents/{doc_id}/translate", json={"targets": ["fr-FR"]}, headers=tok)
    assert r.status_code == 202, (
        f"a valid enabled locale must translate, got {r.status_code} {r.text}"
    )
    after = _counts(proj_id)
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1, (
        "a valid translate must register the locale and enqueue exactly one job"
    )

    # --- translate/text -----------------------------------------------------
    r = api.post(
        "/api/translate/text",
        json={"text": "Hello", "target_locale": "xx-NONSENSE", "provider": "mock"},
        headers=tok,
    )
    assert r.status_code == 400, (
        f"junk target_locale must 400 on translate/text, got {r.status_code} {r.text}"
    )
    r = api.post(
        "/api/translate/text",
        json={"text": "Hello", "target_locale": ["fr"], "provider": "mock"},
        headers=tok,
    )
    assert r.status_code == 422, f"non-string target_locale must 422, got {r.status_code}"
    r = api.post(
        "/api/translate/text",
        json={"text": "Hello", "target_locale": "de-DE", "provider": "mock"},
        headers=tok,
    )
    assert r.status_code == 200 and r.json().get("translation"), (
        f"a valid target_locale must translate, got {r.status_code} {r.text}"
    )

    # --- fr-FR aside: /api project-create accepts a LIST target_locales -----
    # (catalog is correct: fr-FR IS valid; the bug was str([...]) -> "['fr-FR']").
    r = api.post(
        "/api/projects",
        json={"name": "TC FR list", "customer_id": d["cust_id"], "target_locales": ["fr-FR"]},
        headers=tok,
    )
    assert r.status_code == 200, (
        f"list-shaped valid target_locales must succeed, got {r.status_code} {r.text}"
    )
    assert "fr-FR" in r.json().get("target_locales", []), f"fr-FR must be registered, got {r.text}"
    # a genuinely-unknown code in the list still rejects
    r = api.post(
        "/api/projects",
        json={
            "name": "TC bad list",
            "customer_id": d["cust_id"],
            "target_locales": ["xx-NONSENSE"],
        },
        headers=tok,
    )
    assert r.status_code == 400, f"an unknown locale in a list must 400, got {r.status_code}"
    # the comma/space STRING form still works (byte-compat for the form/CLI shape)
    r = api.post(
        "/api/projects",
        json={"name": "TC str form", "customer_id": d["cust_id"], "target_locales": "fr-FR de-DE"},
        headers=tok,
    )
    assert r.status_code == 200 and set(r.json().get("target_locales", [])) >= {"fr-FR", "de-DE"}, (
        f"the string target_locales form must still work, got {r.status_code} {r.text}"
    )
    print("  1. LOCALE-VALIDATION: junk pre-spend rejected, valid translates, fr-FR list OK")


# --------------------------------------------------------------------------- #
# 2. STEP-TYPE-WHITELIST
# --------------------------------------------------------------------------- #
def test_step_type_whitelist(d, api):
    tok = _h(d["token"])
    r = api.post("/api/workflows", json={"name": "TC WF steps"}, headers=tok)
    assert r.status_code == 200, r.text
    wid = r.json()["id"]

    # unknown step_type -> 400 on /api add-step
    r = api.post(
        f"/api/workflows/{wid}/steps", json={"step_type": "banana", "name": "b"}, headers=tok
    )
    assert r.status_code == 400, f"unknown step_type must 400 on add, got {r.status_code} {r.text}"

    # every REAL step type is still accepted at the seam (external needs a vendor — a
    # SEPARATE validation, not the step-type whitelist — so it is exercised below).
    with Session(engine) as s:
        for st in (StepType.LLM_AGENT, StepType.LQA, StepType.HUMAN_REVIEW, StepType.TM_MATCH):
            step = wf_svc.add_step(
                s,
                wid,
                name=f"ok-{st}",
                step_type=st,
                prompt_text=("Translate {text}" if st == StepType.LLM_AGENT else ""),
            )
            assert step.step_type == st
        # the service raises with the WHITELIST message on junk (the shared seam)...
        err = ""
        try:
            wf_svc.add_step(s, wid, name="x", step_type="banana")
        except wf_svc.WorkflowError as exc:
            err = str(exc)
        assert "unknown step type" in err, "add_step must reject 'banana' at the whitelist"
        # ...but 'external' is WHITELISTED — it passes the type gate and only then hits
        # the (separate) vendor requirement, proving it was not type-rejected.
        err = ""
        try:
            wf_svc.add_step(s, wid, name="ext", step_type=StepType.EXTERNAL)
        except wf_svc.WorkflowError as exc:
            err = str(exc)
        assert "unknown step type" not in err and "vendor" in err, (
            f"'external' must pass the type whitelist (fail later on vendor), got {err!r}"
        )

    # edit to an unknown type is refused too
    with Session(engine) as s:
        real = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).first()
        sid = real.id
    r = api.post(
        f"/api/workflows/{wid}/steps/{sid}/edit", json={"step_type": "banana"}, headers=tok
    )
    assert r.status_code == 400, f"edit to unknown step_type must 400, got {r.status_code} {r.text}"
    print("  2. STEP-TYPE-WHITELIST: 'banana' refused on add + edit + service; real types OK")


# --------------------------------------------------------------------------- #
# 3. MISSING-BLOB-500
# --------------------------------------------------------------------------- #
def test_missing_blob(d, api):
    tok = _h(d["token"])
    doc_id, proj_id = d["doc_id"], d["proj_id"]
    from app.services import delivery, export

    # Both arms of the `except (FileNotFoundError, OSError)` tuple are pinned: a wiped
    # file raises FileNotFoundError, an unreadable volume / permission fault raises a
    # broader OSError (a mutant narrowing the except to just FileNotFoundError would
    # then survive on the OSError case). Both must map to 409, not a 500.
    def _raiser(exc):
        def _boom(*a, **k):
            raise exc

        return _boom

    for exc in (FileNotFoundError("no such file"), OSError("input/output error")):
        # download
        orig_build = export.build_translated_file
        export.build_translated_file = _raiser(exc)
        try:
            r = api.get(f"/api/documents/{doc_id}/download?locale=de-DE", headers=tok)
        finally:
            export.build_translated_file = orig_build
        assert r.status_code == 409, (
            f"a missing/unreadable blob ({type(exc).__name__}) must be 409 on download, got {r.status_code} {r.text}"  # noqa: E501
        )
        # export (route currently caught only ValueError/quarantine before the fix)
        orig_export = delivery.export_project
        delivery.export_project = _raiser(exc)
        try:
            r = api.get(f"/api/projects/{proj_id}/export?locales=de-DE", headers=tok)
        finally:
            delivery.export_project = orig_export
        assert r.status_code == 409, (
            f"a missing/unreadable blob ({type(exc).__name__}) must be 409 on export, got {r.status_code} {r.text}"  # noqa: E501
        )
    print(
        "  3. MISSING-BLOB-500: download + export map FileNotFoundError AND OSError to 409, not 500"
    )


# --------------------------------------------------------------------------- #
# 4. EMAIL-VALIDATION
# --------------------------------------------------------------------------- #
def test_email_validation(d, api):
    tok = _h(d["token"])
    bad = [
        "crlf\r\nX-Injected: 1@x.com",  # CR/LF header injection
        "noatdomain@",  # empty domain
        "@nodomain.com",  # empty local
        "has space@x.com",  # embedded whitespace
        "nul\x00byte@x.com",  # embedded NUL
        "a" * 300 + "@x.com",
    ]  # 300-char local part
    for email in bad:
        raised = False
        try:
            security.create_user(email, "X", Role.TRANSLATOR, "")
        except ValueError:
            raised = True
        assert raised, f"create_user must reject {email!r}"
        raised = False
        try:
            security.create_invite(email, Role.TRANSLATOR, "tc-admin@x.com")
        except ValueError:
            raised = True
        assert raised, f"create_invite must reject {email!r}"

    # a valid email still creates; internal-convention logins (local@host, and a bare
    # word with no @) are preserved — the app seeds and uses those, so the gate must not
    # reject them (only the control-char / whitespace / length / malformed-@ shapes above).
    u = security.create_user("good.person@example.com", "Good", Role.TRANSLATOR, "")
    assert u.email == "good.person@example.com"
    u2 = security.create_user("intern@host", "Intern", Role.TRANSLATOR, "")
    assert u2.email == "intern@host", "an internal local@host address must be accepted"
    u3 = security.create_user("bare-login-name", "Bare", Role.TRANSLATOR, "")
    assert u3.email == "bare-login-name", "a bare-word internal login must be accepted"

    # the /api users seam surfaces the rejection as a clean 4xx (not a 500)
    r = api.post(
        "/api/users", json={"email": "bad\r\nInjected: 1@x.com", "role": "translator"}, headers=tok
    )
    assert r.status_code == 400, (
        f"a malformed email must 400 on /api/users, got {r.status_code} {r.text}"
    )
    print("  4. EMAIL-VALIDATION: CRLF/no-domain/NUL/300-char rejected; valid + local@host OK")


# --------------------------------------------------------------------------- #
# 5 + 6. NAME-LENGTH-CAP + NON-STRING-NAME
# --------------------------------------------------------------------------- #
def test_name_cap_and_type(d, api):
    tok = _h(d["token"])

    # over the cap -> 422 (representative /api seam: customers)
    r = api.post("/api/customers", json={"name": "A" * 600}, headers=tok)
    assert r.status_code == 422, f"an over-cap name must 422, got {r.status_code} {r.text}"
    # exactly at the cap is accepted
    r = api.post("/api/customers", json={"name": "B" * ids.NAME_MAX_LEN}, headers=tok)
    assert r.status_code == 200, f"a name at the cap must be accepted, got {r.status_code}"

    # non-string name -> 422 (list and int), never str()-coerced into a repr
    for junk in (["a", "b"], 12345):
        r = api.post("/api/customers", json={"name": junk}, headers=tok)
        assert r.status_code == 422, f"a non-string name must 422, got {r.status_code} for {junk!r}"

    # the shared helper itself (the seam every create funnels through)
    for junk in (["a"], 5, {"x": 1}):
        raised = False
        try:
            ids.clean_name(junk)
        except ids.InvalidNameError:
            raised = True
        assert raised, f"clean_name must reject non-string {junk!r}"
    raised = False
    try:
        ids.clean_name("Z" * (ids.NAME_MAX_LEN + 1))
    except ids.InvalidNameError:
        raised = True
    assert raised, "clean_name must reject an over-cap name"
    assert ids.clean_name(None) == "" and ids.clean_name("  ok  ") == "ok"

    # the other three name seams inherit it (content-type / project / workflow)
    with Session(engine) as s:
        for fn in (
            lambda: content_types.create(s, ["x"], org_id=None),
            lambda: content.project_create(s, 999, customer_id=d["cust_id"]),
            lambda: wf_svc.create_workflow(s, ["w"]),
        ):
            raised = False
            try:
                fn()
            except ids.InvalidNameError:
                raised = True
            assert raised, "a name seam did not reject a non-string name"
    print("  5+6. NAME-CAP + NON-STRING: >512 and non-string rejected (422) across the seams")


# --------------------------------------------------------------------------- #
# 7. ROUTING-PRIORITY-COERCE
# --------------------------------------------------------------------------- #
def test_routing_priority(d, api):
    tok = _h(d["token"])
    # a non-int priority is a clean 400, NOT silently rewritten to 100
    r = api.post("/api/routing/rules", json={"ext": "tc1", "priority": "notanint"}, headers=tok)
    assert r.status_code == 400, f"a non-int priority must 400, got {r.status_code} {r.text}"
    # a real priority is honoured
    r = api.post("/api/routing/rules", json={"ext": "tc2", "priority": 7}, headers=tok)
    assert r.status_code == 200 and r.json().get("priority") == 7, (
        f"a valid priority must be honoured, got {r.status_code} {r.text}"
    )
    # an absent priority still defaults to 100 (byte-compat)
    r = api.post("/api/routing/rules", json={"ext": "tc3"}, headers=tok)
    assert r.status_code == 200 and r.json().get("priority") == 100, (
        f"an absent priority must default to 100, got {r.status_code} {r.text}"
    )
    print("  7. ROUTING-PRIORITY: non-int 400s, real value honoured, absent -> 100")


# --------------------------------------------------------------------------- #
# 8. LQA-THRESHOLD-CLAMP
# --------------------------------------------------------------------------- #
def test_lqa_threshold(d, api):
    tok = _h(d["token"])
    r = api.post("/api/workflows", json={"name": "TC WF lqa"}, headers=tok)
    wid = r.json()["id"]

    for junk in (-5, 150):
        r = api.post(
            f"/api/workflows/{wid}/steps",
            json={"step_type": "lqa", "name": "q", "lqa_threshold": junk},
            headers=tok,
        )
        assert r.status_code == 400, (
            f"out-of-band lqa_threshold {junk} must 400, got {r.status_code} {r.text}"
        )

    # a valid value is accepted and stored verbatim
    r = api.post(
        f"/api/workflows/{wid}/steps",
        json={"step_type": "lqa", "name": "q94", "lqa_threshold": 94},
        headers=tok,
    )
    assert r.status_code == 200, r.text
    sid94 = r.json()["id"]
    with Session(engine) as s:
        assert s.get(WorkflowStep, sid94).lqa_threshold == 94

    # NULL / absent still means "inherit the default" (byte-identical NULL path)
    r = api.post(
        f"/api/workflows/{wid}/steps", json={"step_type": "lqa", "name": "qnull"}, headers=tok
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(WorkflowStep, r.json()["id"]).lqa_threshold is None, (
            "an unset lqa_threshold must stay NULL (inherit the default)"
        )

    # the service seam raises directly, and an in-band edit still works
    with Session(engine) as s:
        raised = False
        try:
            wf_svc.update_step(s, sid94, lqa_threshold=-1)
        except wf_svc.WorkflowError:
            raised = True
        assert raised, "update_step must reject an out-of-band lqa_threshold"
        wf_svc.update_step(s, sid94, lqa_threshold=88)
        assert s.get(WorkflowStep, sid94).lqa_threshold == 88
    print("  8. LQA-THRESHOLD: -5/150 -> 400, 94 stored, NULL inherits, edit guarded")


# --------------------------------------------------------------------------- #
# 9. ASSIST-ASK-KEYLESS
# --------------------------------------------------------------------------- #
def test_assist_ask_keyless(d):
    # A non-mock, non-live engine (keyless real provider) must be pre-guarded with a
    # clean 409, not dialed out to a raw connection 502. Force the non-mock-fallback
    # posture so 'claude' resolves to a real (dead) client rather than the mock.
    prev = config.MOCK_FALLBACK_OK
    config.MOCK_FALLBACK_OK = False
    try:
        status = None
        try:
            assist.ask("", "How do I add a new workflow step in this tool?", provider="claude")
        except assist.AssistError as exc:
            status = exc.status
        assert status == 409, f"a keyless/non-live engine must 409 (pre-guard), got {status}"
    finally:
        config.MOCK_FALLBACK_OK = prev
    print("  9. ASSIST-ASK-KEYLESS: a non-live engine pre-guards with 409, not a 502")


def main():
    d = _seed()
    api = _client()
    test_locale_validation(d, api)
    test_step_type_whitelist(d, api)
    test_missing_blob(d, api)
    test_email_validation(d, api)
    test_name_cap_and_type(d, api)
    test_routing_priority(d, api)
    test_lqa_threshold(d, api)
    test_assist_ask_keyless(d)
    print("TEST CAMPAIGN FIXES OK")


if __name__ == "__main__":
    main()
