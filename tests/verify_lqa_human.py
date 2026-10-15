"""Wave 3 Train C — reviewer-authored LQA finding SURFACES + the re-score guard on the
web button.

SPEC-wave3-collaboration §6.5 / §11 Train C (items 20, 22-24). The SERVICE layer (the
writer/edit/withdraw RBAC + the score_document re-score guard proven directly) is Train A's
(verify_queries.py::test_rescore_guard / test_human_finding_helpers); the reports human arm
(item 25) is verify_reports.py::test_wave3_human_findings_*; the databricks export columns
(item 27) are verify_databricks_export/transport. THIS suite proves:

  20. the re-score guard holds through the WEB button (POST /documents/{id}/lqa/run), not
      only through a direct score_document call: a human finding survives, a prior machine
      row is replaced.
  22/23. the web + /api surfaces log/edit/withdraw, are REVIEWER+ (a translator is refused
      identically on both, and the refusal writes NO audit row), and edit/withdraw are
      own-row OR manager+.
  24. a withdrawn finding disappears from the LQA page AND the /api list but STAYS in the
      table with withdrawn_at set (a soft delete).
  + the LQA page renders persisted human findings, labels the machine score, and shows a
    gated query link; four-surface parity (web / /api / CLI) stores equivalent rows.

Standalone one-process-per-suite; LOC_TMS_DATA_DIR is a throwaway temp dir, provider keys
cleared -> MockProvider. ASCII-only output.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqahuman_")


from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    AuditLog,
    Customer,
    Document,
    LQAResult,
    Role,
    Segment,
)
from app.services import apikeys, content, lqa, security, settings
from tests._csrf import csrf_for

PW = "Sup3rSecret!pw-1234"
_N = [0]


def _uniq():
    _N[0] += 1
    return _N[0]


def _set(**kw):
    settings.set_many({k: str(v) for k, v in kw.items()})
    settings.invalidate_cache()


def _auth(on):
    _set(
        security_auth_required=("1" if on else "0"),
        multi_org_enabled="0",
        project_scoping="0",
        queries_enabled="1",
    )


def _mk_user(role):
    email = "u%d@x.com" % _uniq()  # noqa: UP031
    return security.create_user(email, "U", role, PW)


def _key(user):
    return apikeys.create_key(user.id, "lqah", scopes="api docs")[1]


def _h(raw):
    return {"X-Loc-Token": raw}


def _web(email):
    c = TestClient(app_)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code in (302, 303), "login failed: %d" % r.status_code  # noqa: UP031
    csrf_for(c)
    return c


from app.main import app as app_


def _seed(translated=True):
    init_db()
    with Session(engine) as s:
        proj = content.project_create(
            s,
            "LqaH %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by="admin@x.com",
        )
        pid = proj.id
        doc = Document(
            project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        segs = []
        for i in range(2):
            seg = Segment(
                document_id=did,
                source_text="hello world %d" % i,  # noqa: UP031
                target_text=("bonjour le monde %d" % i if translated else ""),  # noqa: UP031
                target_locale="fr-FR",
                order_index=i,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            segs.append(seg.id)
    return pid, did, segs


def _audit_rows(prefix="lqa.finding."):
    with Session(engine) as s:
        return [r for r in s.exec(select(AuditLog)).all() if (r.action or "").startswith(prefix)]


# =========================================================================== #
def test_web_log_render_and_api_list():
    _auth(False)
    _pid, did, segs = _seed()
    wc = TestClient(app_)
    r = wc.post(
        "/documents/%d/lqa/findings" % did,  # noqa: UP031
        data={
            "segment_id": segs[0],
            "dimension": "Accuracy",
            "severity": "major",
            "note": "mistranslation here",
            "locale": "fr-FR",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    # the finding persisted with the right shape (human, NULLs, locale, actor).
    with Session(engine) as s:
        rows = s.exec(select(LQAResult).where(LQAResult.document_id == did)).all()
        assert len(rows) == 1, rows
        f = rows[0]
        assert f.finding_source == "human" and f.segment_id == segs[0]
        assert f.score is None and f.scoring_id is None and f.judge is None
        assert f.target_locale == "fr-FR" and f.withdrawn_at is None
        assert f.actor, "an actor must be stamped"
    # the LQA page renders the note + the machine-score label.
    page = wc.get("/documents/%d/lqa?locale=fr-FR" % did).text  # noqa: UP031
    assert "mistranslation here" in page, "the human finding renders on the page"
    assert "machine score" in page.lower(), "the score is labelled machine-only (§6.4)"
    assert "Reviewer findings" in page, "the reviewer-findings section renders"
    # /api list returns it.
    admin = _mk_user(Role.ADMIN)
    body = (
        TestClient(app_).get("/api/documents/%d/lqa/findings" % did, headers=_h(_key(admin))).json()  # noqa: UP031
    )
    assert body["ok"] and len(body["findings"]) == 1
    assert body["findings"][0]["note"] == "mistranslation here"
    print("  20a web log -> persists human row + renders on page + /api lists OK")


def test_rescore_guard_via_web_button():
    _auth(False)
    _pid, did, segs = _seed()
    # a prior MACHINE finding (segment-level) that a re-score must REPLACE...
    with Session(engine) as s:
        s.add(
            LQAResult(
                document_id=did,
                segment_id=segs[0],
                dimension="Fluency",
                severity="minor",
                note="old machine",
                score=88.0,
                target_locale="fr-FR",
                finding_source="llm",
                judge="mock/j",
                scoring_id="oldcard",
            )
        )
        s.commit()
    # ...and a HUMAN finding a re-score must PRESERVE (§6.3, the wave's headline invariant).
    with Session(engine) as s:
        seg = s.get(Segment, segs[0])
        human = lqa.log_human_finding(
            s,
            segment=seg,
            dimension="Accuracy",
            severity="critical",
            note="human bug",
            actor="rev@x",
            trusted_operator=True,
        )
        human_id = human.id
    # click "Re-score & save" via the WEB route (documents.py lqa_run -> score_document).
    r = TestClient(app_).post(
        "/documents/%d/lqa/run" % did,  # noqa: UP031
        data={"locale": "fr-FR"},
        follow_redirects=False,  # noqa: RUF100, UP031
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        alive = {
            (row.finding_source, row.note)
            for row in s.exec(select(LQAResult).where(LQAResult.document_id == did)).all()
        }
        # the human finding SURVIVED by id...
        assert s.get(LQAResult, human_id) is not None, "the human finding was destroyed!"
        assert ("human", "human bug") in alive, alive
        # ...and the prior machine row is GONE (replaced by the re-score).
        assert ("llm", "old machine") not in alive, ("stale machine row survived", alive)
    print("  20 re-score guard via the WEB button: human survives, machine replaced OK")


def test_rbac_translator_refused_web_and_api():
    _auth(True)
    _pid, did, segs = _seed()
    tr = _mk_user(Role.TRANSLATOR)
    n_audit0 = len(_audit_rows("lqa.finding.log"))
    # web: a translator is refused (403); no audit row.
    wc = _web(tr.email)
    rw = wc.post(
        "/documents/%d/lqa/findings" % did,  # noqa: UP031
        data={
            "segment_id": segs[0],
            "dimension": "Accuracy",
            "severity": "major",
            "note": "x",
            "locale": "fr-FR",
        },
        follow_redirects=False,
    )
    assert rw.status_code == 403, ("web translator must be 403", rw.status_code)
    # /api: identical verdict (403).
    ra = TestClient(app_).post(
        "/api/documents/%d/lqa/findings" % did,  # noqa: UP031
        json={"segment_id": segs[0], "dimension": "Accuracy", "severity": "major", "note": "x"},
        headers=_h(_key(tr)),
    )
    assert ra.status_code == 403, ("api translator must be 403", ra.status_code, ra.text)
    # #70: the /api refusal must NAME the LQA-finding action, byte-identical to the web
    # twin — NOT the TM-unlock wording the shared _REVIEW_FORBIDDEN carries.
    assert (
        ra.json().get("error") == "logging an LQA finding requires the reviewer role (or higher)."
    ), (
        "#70: /api finding refusal must carry the LQA-finding message, not TM-unlock",
        ra.json().get("error"),
    )
    # no finding written for THIS doc, no NEW audit row (refused attempts leave no trail).
    with Session(engine) as s:
        assert not s.exec(select(LQAResult).where(LQAResult.document_id == did)).all(), (
            "a refused log wrote a finding!"
        )
    assert len(_audit_rows("lqa.finding.log")) == n_audit0, (
        "a refused attempt wrote an lqa.finding audit row"
    )
    # a REVIEWER succeeds on both.
    rev = _mk_user(Role.REVIEWER)
    wc2 = _web(rev.email)
    ok = wc2.post(
        "/documents/%d/lqa/findings" % did,  # noqa: UP031
        data={
            "segment_id": segs[0],
            "dimension": "Accuracy",
            "severity": "major",
            "note": "reviewer ok",
            "locale": "fr-FR",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303, ok.status_code
    assert len(_audit_rows("lqa.finding.log")) == n_audit0 + 1, (
        "the allowed log wrote exactly one audit row"
    )
    print("  23 RBAC: translator refused 403 on web AND /api (no audit row); reviewer OK")


def test_edit_withdraw_ownership_and_soft_delete():
    _auth(True)
    _pid, did, segs = _seed()
    revA = _mk_user(Role.REVIEWER)
    revB = _mk_user(Role.REVIEWER)
    mgr = _mk_user(Role.MANAGER)
    kA, kB, kM = _key(revA), _key(revB), _key(mgr)
    # revA logs via /api.
    r = TestClient(app_).post(
        "/api/documents/%d/lqa/findings" % did,  # noqa: UP031
        json={
            "segment_id": segs[0],
            "dimension": "Accuracy",
            "severity": "major",
            "note": "A's finding",
        },
        headers=_h(kA),
    )
    assert r.status_code == 200, r.text
    fid = r.json()["finding"]["id"]
    # revB (not the author, not a manager) CANNOT edit it.
    rb = TestClient(app_).post(
        "/api/lqa/findings/%d" % fid,  # noqa: UP031
        json={"dimension": "Fluency", "severity": "minor", "note": "hijack"},
        headers=_h(kB),
    )
    assert rb.status_code == 403, ("non-owner reviewer must be 403", rb.status_code)
    # revA edits their own.
    re = TestClient(app_).post(
        "/api/lqa/findings/%d" % fid,  # noqa: UP031
        json={"dimension": "Fluency", "severity": "minor", "note": "A edited"},
        headers=_h(kA),
    )
    assert re.status_code == 200 and re.json()["finding"]["note"] == "A edited", re.text
    # a MANAGER may withdraw ANY finding (clearing a departed reviewer's noise).
    rw = TestClient(app_).post("/api/lqa/findings/%d/withdraw" % fid, headers=_h(kM))  # noqa: UP031
    assert rw.status_code == 200 and rw.json()["finding"]["withdrawn_at"], rw.text
    # soft delete: the row STAYS in the table with withdrawn_at set...
    with Session(engine) as s:
        row = s.get(LQAResult, fid)
        assert row is not None and row.withdrawn_at is not None, "withdraw hard-deleted!"
    # ...but disappears from the /api list AND the page.
    body = TestClient(app_).get("/api/documents/%d/lqa/findings" % did, headers=_h(kM)).json()  # noqa: UP031
    assert body["findings"] == [], "a withdrawn finding still lists"
    page = _web(mgr.email).get("/documents/%d/lqa?locale=fr-FR" % did).text  # noqa: UP031
    assert "A edited" not in page, "a withdrawn finding still renders on the page"
    print(
        "  23/24 edit own-only (manager any), withdraw is a soft delete hidden from page + /api OK"
    )


def test_four_surface_parity():
    _auth(False)
    _pid, did, segs = _seed()
    seg = segs[0]
    admin = _mk_user(Role.ADMIN)
    key = _key(admin)
    # web (auth off -> SYSTEM admin), /api (admin key), service (in-process trusted op).
    rw = TestClient(app_).post(
        "/documents/%d/lqa/findings" % did,  # noqa: UP031
        data={
            "segment_id": seg,
            "dimension": "Accuracy",
            "severity": "major",
            "note": "web",
            "locale": "fr-FR",
        },
        follow_redirects=False,
    )
    assert rw.status_code == 303
    ra = TestClient(app_).post(
        "/api/documents/%d/lqa/findings" % did,  # noqa: UP031
        json={"segment_id": seg, "dimension": "Accuracy", "severity": "major", "note": "api"},
        headers=_h(key),
    )
    assert ra.status_code == 200, ra.text
    with Session(engine) as s:
        sg = s.get(Segment, seg)
        lqa.log_human_finding(
            s,
            segment=sg,
            dimension="Accuracy",
            severity="major",
            note="cli",
            actor="op@x",
            trusted_operator=True,
        )
    with Session(engine) as s:
        rows = s.exec(
            select(LQAResult).where(LQAResult.segment_id == seg).order_by(LQAResult.id)
        ).all()
        assert len(rows) == 3, len(rows)
        for f in rows:
            assert f.finding_source == "human" and f.document_id == did
            assert f.target_locale == "fr-FR" and f.dimension == "Accuracy"
            assert f.score is None and f.scoring_id is None
    print("  four-surface parity: web / /api / CLI store equivalent human rows OK")


def test_lqa_page_query_link_gated():  # LOW-2 (gate)
    # The LQA page's per-segment query link is a QUERIES-feature affordance (§5.2) and must
    # vanish with the kill switch, while the findings themselves are unaffected (core).
    _auth(False)  # queries_enabled=1
    _pid, did, _segs = _seed()
    on = TestClient(app_).get("/documents/%d/lqa?locale=fr-FR" % did).text  # noqa: UP031
    assert "/queries?segment=" in on, "the query link must render when queries is ON"
    _set(queries_enabled="0")
    off = TestClient(app_).get("/documents/%d/lqa?locale=fr-FR" % did).text  # noqa: UP031
    assert "/queries?segment=" not in off, "the query link must VANISH when queries is OFF"
    # the LQA machine findings table itself still renders (findings are core, not gated).
    assert "Findings" in off, "the findings section must survive the queries kill switch"
    _set(queries_enabled="1")  # restore for later tests
    print("  LOW-2 LQA-page query link gated on queries_enabled (findings survive) OK")


def test_foreign_doc_404_before_body_validation():  # sweep class (H-1 / budget-route)
    # A customer-bound caller hitting a FOREIGN document's log route must get 404
    # BEFORE any body validation — a 422 (missing segment_id) would leak that the doc
    # exists (the validation-before-authorization class). Proven both with an invalid
    # body and a valid one: identical 404, indistinguishable from missing.
    _auth(True)
    with Session(engine) as s:
        home = Customer(name="Home %d" % _uniq())  # noqa: UP031
        other = Customer(name="Other %d" % _uniq())  # noqa: UP031
        s.add(home)
        s.add(other)
        s.commit()
        s.refresh(home)
        s.refresh(other)
        home_id, other_id = home.id, other.id
        proj = content.project_create(
            s,
            "Foreign %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by="admin@x.com",
            customer_id=str(other_id),
        )
        doc = Document(
            project_id=proj.id, filename="f.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
    # a caller bound to `home` (linguist has can_review, but the ring 404s first anyway).
    bound = security.create_user(
        "bnd%d@x.com" % _uniq(),  # noqa: UP031
        "B",
        Role.LINGUIST,
        PW,
        customer_id=home_id,  # noqa: RUF100, UP031
    )
    wc = _web(bound.email)
    # (a) invalid body (no segment_id) -> MUST be 404, never a 422 existence leak.
    r_bad = wc.post(
        "/documents/%d/lqa/findings" % did,  # noqa: UP031
        data={"dimension": "Accuracy", "severity": "major", "note": "x"},
        follow_redirects=False,
    )
    assert r_bad.status_code == 404, (
        "a foreign doc must 404 even with an INVALID body (no 422 leak)",
        r_bad.status_code,
    )
    # (b) a fully valid body -> still 404, indistinguishable from missing.
    r_ok = wc.post(
        "/documents/%d/lqa/findings" % did,  # noqa: UP031
        data={"segment_id": 1, "dimension": "Accuracy", "severity": "major", "note": "x"},
        follow_redirects=False,
    )
    assert r_ok.status_code == 404, (
        "a foreign doc must 404 with a VALID body too",
        r_ok.status_code,
    )
    # #70 /api parity: the /api finding routes must ALSO ring-before-role — a caller who
    # cannot SEE the doc gets 404, NOT a role 403, matching the GET twin + the web route.
    # Use a bound VIEWER (fails the reviewer gate): under the OLD role-before-visibility
    # ordering this returned 403 (leaking that the doc exists to an out-of-scope caller);
    # after the fix the visibility ring 404s first.
    bview = security.create_user("bv%d@x.com" % _uniq(), "V", Role.VIEWER, PW, customer_id=home_id)  # noqa: UP031
    kbv = _h(_key(bview))
    r_get = TestClient(app_).get("/api/documents/%d/lqa/findings" % did, headers=kbv)  # noqa: UP031
    assert r_get.status_code == 404, ("GET twin must 404 for a foreign doc", r_get.status_code)
    r_log = TestClient(app_).post(
        "/api/documents/%d/lqa/findings" % did,  # noqa: UP031
        json={"segment_id": 1, "dimension": "Accuracy", "severity": "major", "note": "x"},
        headers=kbv,
    )
    assert r_log.status_code == 404, (
        "#70: /api finding POST must 404 (visibility before role) for a foreign doc, "
        "not a role 403",
        r_log.status_code,
        r_log.text,
    )
    print(
        "  H-1 foreign doc 404s BEFORE body validation (no 422 existence leak); "
        "/api finding POST 404s BEFORE the role gate (#70) OK"
    )


def main():
    init_db()  # schema before any settings read (tests toggle auth first)
    test_web_log_render_and_api_list()
    test_lqa_page_query_link_gated()
    test_foreign_doc_404_before_body_validation()
    test_rescore_guard_via_web_button()
    test_rbac_translator_refused_web_and_api()
    test_edit_withdraw_ownership_and_soft_delete()
    test_four_surface_parity()
    print("LQA-HUMAN OK")


if __name__ == "__main__":
    main()
