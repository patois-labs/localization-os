"""Department budgets: spend is per org, and the queued path reserves atomically (offline).

Issue #503 (found by the PR 472 review). Under an ACTIVE multi-org gate:

  [1] spend is scoped by org: two orgs share the department name "marketing"; org B's jobs
      (stamped, legacy-unstamped and a pseudo-actor's) never count against org A's cap, org
      A's /api/budgets and budgets.check see only org A's usage, and the legacy actor-membership
      email set is per org. Gate OFF the sum is install-wide exactly as before.
  [2] check-then-insert is atomic on the queued path: concurrent POST /api/documents/{id}/
      translate requests against a cap with a small remainder enqueue ONE job (the rest get
      429), where the unlocked check used to let every request through. The web translate
      door and jobs.submit_budgeted share the same reservation.

Multi-org stays OFF on every real install; this suite only turns it on in a throwaway data dir.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_budget_org_")

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import DepartmentBudget, Job, JobStatus, Organization, Role, Segment, User
from app.services import access, apikeys, budgets, jobs, security, settings
from app.services.providers import Provider, register_provider
from tests._csrf import csrf_for, session_data

PW = "BudgetOrg-Pass-2026-x"  # pregate: allow-secret throwaway fixture login
LIMIT_A = 10_000

FIX = {}


class LiveStubProvider(Provider):
    """A 'configured' engine so provider_is_live() is True (no key needed)."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return (prompt or "").strip()


def _dx(chars):
    d = Docx()
    d.add_paragraph("word " * (chars // 5))
    b = BytesIO()
    d.save(b)
    return b.getvalue()


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _seed_job(created_by, tin, tout, *, department=None, status=JobStatus.COMPLETED, est=None):
    payload = {"document_id": 0, "locale": "fr-FR"}
    if department is not None:
        payload["department"] = department
    if est is not None:
        payload["est_tokens"] = est
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload=payload,
            status=status,
            result={"tokens_in": tin, "tokens_out": tout},
            created_by=created_by,
            created_at=datetime.utcnow(),
        )
        s.add(j)
        s.commit()


def _fixture():
    init_db()
    with Session(engine) as s:
        for key in ("a", "b"):
            org = Organization(name=f"Org {key} budgets")
            s.add(org)
            s.commit()
            s.refresh(org)
            FIX[f"org_{key}"] = org.id
    for key, role in (("a", Role.ADMIN), ("b", Role.TRANSLATOR)):
        u = security.create_user(
            f"bud-{key}@example.com", f"User {key}", role, PW, org_id=FIX[f"org_{key}"]
        )
        FIX[f"uid_{key}"] = u.id
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.department = "Marketing"
            s.add(row)
            s.commit()
    with Session(engine) as s:
        s.add(
            DepartmentBudget(
                department="marketing", monthly_token_limit=LIMIT_A, org_id=FIX["org_a"]
            )
        )
        s.add(
            DepartmentBudget(department="marketing", monthly_token_limit=10**9, org_id=FIX["org_b"])
        )
        s.commit()
    _r, raw = apikeys.create_key(FIX["uid_a"], "bud-a", scopes="api docs admin")
    FIX["hdr_a"] = {"X-Loc-Token": raw}
    settings.set_many({"security_auth_required": "1"})


def _set_gate(on: bool):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_budget_org_" in os.environ["LOC_TMS_DATA_DIR"]
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    assert access.multi_org_active() is on


def _spend(dept, org_id=None):
    with Session(engine) as s:
        return budgets.spend_this_month(s, dept, org_id=org_id)


def test_1_spend_is_per_org():
    _set_gate(True)
    # Org B burns 50,000 tokens: stamped, unstamped (legacy membership fallback), and a
    # webhook pseudo-actor whose job references org B's content is not resolvable here, so it
    # stays foreign to both.
    _seed_job("bud-b@example.com", 30_000, 10_000, department="marketing")
    _seed_job("bud-b@example.com", 7_000, 3_000)  # legacy: no department stamp
    _seed_job("bud-b@example.com", 0, 0, department="marketing", status=JobStatus.QUEUED, est=99)
    assert _spend("marketing", FIX["org_a"]) == 0, _spend("marketing", FIX["org_a"])
    assert _spend("marketing", FIX["org_b"]) == 50_000 + 99, _spend("marketing", FIX["org_b"])
    allowed, remaining, msg = budgets.check("marketing", org_id=FIX["org_a"])
    assert allowed and remaining == LIMIT_A and msg == "", (allowed, remaining, msg)
    # Org A's own usage still counts against org A, and only org A.
    _seed_job("bud-a@example.com", 6_000, 5_000, department="marketing")
    assert _spend("marketing", FIX["org_a"]) == 11_000
    allowed, remaining, _msg = budgets.check("marketing", org_id=FIX["org_a"])
    assert not allowed and remaining == 0
    allowed, remaining, _msg = budgets.check("marketing", org_id=FIX["org_b"])
    assert allowed, "org A's usage capped org B"
    # The /api listing shows each org only its own number.
    api = TestClient(app)
    r = api.get("/api/budgets", headers=FIX["hdr_a"])
    assert r.status_code == 200, (r.status_code, r.text[:200])
    rows = r.json()["budgets"]
    assert len(rows) == 1 and rows[0]["spent_this_month"] == 11_000, rows
    # The block lifts for org A without touching the org B total (nothing is shared).
    print("  [1] spend, check and /api/budgets are per org under multi-org")


def test_1b_gate_off_is_install_wide():
    _set_gate(False)
    total = _spend("marketing", FIX["org_a"])
    assert total == _spend("marketing") == 50_000 + 99 + 11_000, total
    api = TestClient(app)
    r = api.get("/api/budgets", headers=FIX["hdr_a"])
    assert r.status_code == 200, r.text[:200]
    print("  [1b] gate off: the department sum is install-wide, byte-identical")


def test_2_queued_path_reserves_atomically():
    _set_gate(True)
    # Org A's cap is already used up by [1]; give the race its own department with a
    # 10,000-token cap and nothing spent.
    with Session(engine) as s:
        s.add(
            DepartmentBudget(department="racers", monthly_token_limit=LIMIT_A, org_id=FIX["org_a"])
        )
        row = s.get(User, FIX["uid_a"])
        row.department = "racers"
        s.add(row)
        s.commit()
    register_provider("claude", LiveStubProvider())
    real_submit = jobs.submit
    inserted = []

    def slow_submit(kind, payload, actor="", *, session=None):
        # Stand in for DB latency between the check and the insert, and never start a real
        # worker: just the Job row the next check counts as in-flight spend. #622: the
        # budgeted doors pass the session that holds the reservation lock; insert on it.
        time.sleep(0.25)
        if session is not None:
            j = Job(kind=kind, payload=payload or {}, created_by=actor)
            session.add(j)
            session.commit()
            session.refresh(j)
            inserted.append(j.id)
            return j.id
        with Session(engine) as s:
            j = Job(kind=kind, payload=payload or {}, created_by=actor)
            s.add(j)
            s.commit()
            s.refresh(j)
            inserted.append(j.id)
            return j.id

    try:
        api = TestClient(app)
        doc_ids = []
        for n in range(6):
            r = api.post(
                "/api/documents",
                headers=FIX["hdr_a"],
                files={"file": (f"race{n}.docx", _dx(80_000), _DOCX)},
            )
            assert r.status_code == 200, (r.status_code, r.text[:200])
            doc_ids.append(r.json()["document_id"])
        FIX["race_docs"] = doc_ids
        jobs.submit = slow_submit
        results = []
        barrier = threading.Barrier(len(doc_ids))

        def worker(doc_id):
            c = TestClient(app)
            barrier.wait()
            r = c.post(
                f"/api/documents/{doc_id}/translate",
                headers=FIX["hdr_a"],
                json={"targets": ["fr-FR"]},
            )
            results.append(r.status_code)

        threads = [threading.Thread(target=worker, args=(d,)) for d in doc_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert len(results) == len(doc_ids), results
        assert results.count(202) == 1, ("expected exactly one admitted job", sorted(results))
        assert results.count(429) == len(doc_ids) - 1, sorted(results)
        assert len(inserted) == 1, inserted
    finally:
        jobs.submit = real_submit
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)
    # jobs.submit_budgeted: the shared seam refuses once the in-flight estimate uses the cap up.
    try:
        jobs.submit_budgeted(
            "translate_document",
            {"document_id": 0, "locale": "fr-FR", "est_tokens": 1, "department": "racers"},
            actor="bud-a@example.com",
            department="racers",
            org_id=FIX["org_a"],
        )
    except jobs.LiveBudgetRefused:
        pass
    else:
        raise AssertionError("submit_budgeted ignored a used-up cap")
    print("  [2] six concurrent translate requests against a 10k cap enqueue exactly one job")


def _login(email) -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": PW, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    csrf_for(c)
    return c


def _translate_jobs(doc_id):
    with Session(engine) as s:
        return [
            j
            for j in s.exec(select(Job).where(Job.kind == "translate_document")).all()
            if (j.payload or {}).get("document_id") == doc_id
        ]


def test_2b_translate_all_web_door_is_budgeted():
    """Gate merge (#563 + #503): the web "Translate all languages" door goes through the same
    budget lock and stamps the department and estimate, and a re-post hands back the live job."""
    _set_gate(True)
    register_provider("claude", LiveStubProvider())
    try:
        # "racers" is used up by [2]'s in-flight job: translate-all is refused, nothing queued.
        # Any race doc can win [2]'s race (thread order), so use one that did NOT get the
        # admitted job; a fixed index made the "nothing queued" check fail whenever it won.
        losers = [d for d in FIX["race_docs"] if not _translate_jobs(d)]
        assert len(losers) == len(FIX["race_docs"]) - 1, losers
        busy_doc = losers[0]
        c = _login("bud-a@example.com")
        r = c.post(f"/documents/{busy_doc}/translate", data={"locale": "all"})
        assert r.status_code == 429, (r.status_code, r.text[:200])
        assert _translate_jobs(busy_doc) == []
        # A department with room: one stamped job per locale, and a re-post is deduplicated.
        with Session(engine) as s:
            s.add(
                DepartmentBudget(department="roomy", monthly_token_limit=10**9, org_id=FIX["org_a"])
            )
            row = s.get(User, FIX["uid_a"])
            row.department = "roomy"
            s.add(row)
            s.commit()
        real_enqueue = jobs.ensure_workers
        jobs.ensure_workers = lambda: None
        from app.ports import jobqueue

        q = jobqueue.get()
        real_q_enqueue = q.enqueue
        q.enqueue = lambda jid: None
        try:
            r = c.post(
                f"/documents/{busy_doc}/translate", data={"locale": "all"}, follow_redirects=False
            )
            assert r.status_code == 303, (r.status_code, r.text[:200])
            got = _translate_jobs(busy_doc)
            with Session(engine) as s:
                want = {
                    seg.target_locale
                    for seg in s.exec(select(Segment).where(Segment.document_id == busy_doc))
                }
            assert sorted(j.payload.get("locale") for j in got) == sorted(want), got
            for j in got:
                assert j.payload.get("department") == "roomy", j.payload
                assert j.payload.get("est_tokens", 0) >= 1, j.payload
            r = c.post(
                f"/documents/{busy_doc}/translate", data={"locale": "all"}, follow_redirects=False
            )
            assert r.status_code == 303, r.status_code
            assert len(_translate_jobs(busy_doc)) == len(want), "a re-post stacked a paid job"
        finally:
            jobs.ensure_workers = real_enqueue
            q.enqueue = real_q_enqueue
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)
    print("  [2b] web translate-all: refused at a used-up cap, stamped and deduplicated otherwise")


def main():
    _fixture()
    try:
        test_1_spend_is_per_org()
        test_2_queued_path_reserves_atomically()
        test_2b_translate_all_web_door_is_budgeted()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    test_1b_gate_off_is_install_wide()
    print("verify_budget_org_scope OK")


if __name__ == "__main__":
    main()
