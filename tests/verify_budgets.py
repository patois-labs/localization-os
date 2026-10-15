"""Per-department budgets + inbound API rate limits (MCP Phase B chunk 4, offline).

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys cleared (forces MockProvider),
ASCII-only prints, FastAPI TestClient.

Covers:
  * spend_this_month aggregates tokens_in+tokens_out across a department's
    translate_document jobs this month; FAILED jobs count with real tokens,
    QUEUED/RUNNING contribute their stamped est_tokens (in-flight spend, Finding 2),
    other departments' jobs and last-month jobs are excluded.
  * in-flight: a burst of QUEUED jobs whose est_tokens exceed the cap blocks the
    next submit before any finishes (Finding 2).
  * dept-change: spend stays with the SUBMIT-time stamped department, not the
    actor's current one (Finding 4).
  * budgets.check(): unset/limit-0 department = unlimited; under cap = allowed with
    remaining; at/over cap = blocked with the exhausted message.
  * API: a department at its cap gets 429 on POST translate and enqueues ZERO jobs;
    a department with no budget translates normally.
  * rate limiter: 429 after N requests in the window, then recovers once older hits
    age out; rate_limit=0 disables it entirely.
"""

import os
import sys
from datetime import datetime
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_budgets_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    DepartmentBudget,
    Job,
    JobStatus,
    Role,
    User,
)
from app.services import apikeys, budgets, ratelimit, security, settings
from app.services.providers import Provider, register_provider


class LiveStubProvider(Provider):
    """A 'configured' engine so provider_is_live() is True (no key needed) — flips
    the mock-hard-fail off so we reach the budget/enqueue path."""

    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "translate":
            payload = payload or {}
            return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip()


def dx(paras):
    d = Docx()
    for p in paras:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _seed_job(
    created_by,
    tin,
    tout,
    status=JobStatus.COMPLETED,
    created_at=None,
    department=None,
    est_tokens=None,
):
    """Insert a translate_document Job with known token stats in its result.

    department/est_tokens, when given, are stamped into the payload (mirroring the
    submit-time stamp the API now writes)."""
    payload = {"document_id": 0, "locale": "fr-FR"}
    if department is not None:
        payload["department"] = department
    if est_tokens is not None:
        payload["est_tokens"] = est_tokens
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload=payload,
            status=status,
            result={"tokens_in": tin, "tokens_out": tout},
            created_by=created_by,
            created_at=created_at or datetime.utcnow(),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _jobs_for_doc(doc_id):
    with Session(engine) as s:
        return s.exec(select(Job).where(Job.kind == "translate_document")).all()


def test_spend_aggregation():
    """spend_this_month sums the right jobs and excludes the wrong ones."""
    # Two users in dept 'mktg', one in 'eng', one with no dept.
    m1 = security.create_user("m1@x.co", "M1", Role.TRANSLATOR, "pw1-fixture-a1")
    m2 = security.create_user("m2@x.co", "M2", Role.TRANSLATOR, "pw2-fixture-b2")
    e1 = security.create_user("e1@x.co", "E1", Role.TRANSLATOR, "pw3-fixture-c3")
    with Session(engine) as s:
        for u, dept in ((m1, "mktg"), (m2, "mktg"), (e1, "eng")):
            row = s.get(User, u.id)
            row.department = dept
            s.add(row)
        s.commit()

    # mktg: two COMPLETED (100+50, 200+100) + one FAILED (10+5, still counts)
    _seed_job("m1@x.co", 100, 50)
    _seed_job("m2@x.co", 200, 100)
    _seed_job("m1@x.co", 10, 5, status=JobStatus.FAILED)
    # a QUEUED job (no final stats) must NOT count
    _seed_job("m2@x.co", 999, 999, status=JobStatus.QUEUED)
    # last month must NOT count
    now = datetime.utcnow()
    last_month = datetime(
        now.year - (1 if now.month == 1 else 0), 12 if now.month == 1 else now.month - 1, 15
    )
    _seed_job("m1@x.co", 500, 500, created_at=last_month)
    # eng dept — separate budget
    _seed_job("e1@x.co", 40, 10)

    with Session(engine) as s:
        mktg = budgets.spend_this_month(s, "mktg")
        eng = budgets.spend_this_month(s, "eng")
        none = budgets.spend_this_month(s, "")
    # 150 + 300 + 15 (failed) = 465; QUEUED + last-month excluded
    assert mktg == 465, f"mktg spend expected 465, got {mktg}"
    assert eng == 50, f"eng spend expected 50, got {eng}"
    assert none == 0, f"blank dept spend must be 0, got {none}"
    print(
        "  spend: sums COMPLETED+FAILED this-month per dept; excludes QUEUED/last-month/other-dept"
    )


def test_check():
    """check(): unlimited when unset/0; allowed under cap; blocked at cap."""
    # No budget row for mktg yet -> unlimited.
    allowed, remaining, msg = budgets.check("mktg")
    assert allowed and remaining == -1 and msg == "", (allowed, remaining, msg)

    # Set a generous cap well above the 465 already spent -> allowed, remaining>0.
    with Session(engine) as s:
        s.add(DepartmentBudget(department="mktg", monthly_token_limit=1000))
        s.commit()
    allowed, remaining, msg = budgets.check("mktg")
    assert allowed and remaining == 535, (allowed, remaining)  # 1000 - 465
    print("  check: unset dept unlimited; under-cap allowed with remaining")

    # A limit of 0 is unlimited even with a row present.
    with Session(engine) as s:
        row = budgets.get_budget(s, "mktg")
        row.monthly_token_limit = 0
        s.add(row)
        s.commit()
    allowed, remaining, msg = budgets.check("mktg")
    assert allowed and remaining == -1, (allowed, remaining)

    # Tighten below current spend -> blocked with the exhausted message.
    with Session(engine) as s:
        row = budgets.get_budget(s, "mktg")
        row.monthly_token_limit = 100  # spent 465 >> 100
        s.add(row)
        s.commit()
    allowed, remaining, msg = budgets.check("mktg")
    assert not allowed and remaining == 0 and "budget" in msg.lower(), (allowed, remaining, msg)
    assert msg == budgets.EXHAUSTED_MESSAGE, msg
    print("  check: limit 0 = unlimited; spend past cap -> blocked + exhausted message")


def test_inflight_est_tokens():
    """Finding 2: QUEUED/RUNNING jobs' stamped est_tokens count toward spend, so a
    burst of pending jobs can exhaust the budget before any finishes."""
    u = security.create_user("inflight@x.co", "In", Role.TRANSLATOR, "pw-fixture-4417")
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.department = "inflight"
        s.add(row)
        s.commit()
    # A budget of 300 tokens for the department.
    with Session(engine) as s:
        s.add(DepartmentBudget(department="inflight", monthly_token_limit=300))
        s.commit()
    # No jobs yet -> allowed.
    allowed, remaining, _ = budgets.check("inflight")
    assert allowed and remaining == 300, (allowed, remaining)
    # Three QUEUED jobs, each estimated 120 tokens (no final stats), stamped with
    # the department. 3*120 = 360 in-flight > 300 cap.
    for _ in range(3):
        _seed_job(
            "inflight@x.co", 0, 0, status=JobStatus.QUEUED, department="inflight", est_tokens=120
        )
    with Session(engine) as s:
        spent = budgets.spend_this_month(s, "inflight")
    assert spent == 360, f"in-flight estimate should sum to 360, got {spent}"
    allowed, remaining, msg = budgets.check("inflight")
    assert not allowed and msg == budgets.EXHAUSTED_MESSAGE, (allowed, remaining, msg)
    print("  in-flight: QUEUED jobs' est_tokens count -> next submit blocked before any finishes")


def test_dept_change_spend_stays():
    """Finding 4: spend stays with the department a job was SUBMITTED under, even
    after the user later changes departments."""
    u = security.create_user("mover@x.co", "Mover", Role.TRANSLATOR, "pw-fixture-4417")
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.department = "dept-a"
        s.add(row)
        s.commit()
    # Submit a completed job stamped dept-a (200 real tokens).
    _seed_job("mover@x.co", 120, 80, status=JobStatus.COMPLETED, department="dept-a")
    with Session(engine) as s:
        assert budgets.spend_this_month(s, "dept-a") == 200
        assert budgets.spend_this_month(s, "dept-b") == 0
    # The user moves to dept-b.
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.department = "dept-b"
        s.add(row)
        s.commit()
    # Spend stays with dept-a (the stamp), NOT dept-b (current membership).
    with Session(engine) as s:
        assert budgets.spend_this_month(s, "dept-a") == 200, "stamped spend must not migrate"
        assert budgets.spend_this_month(s, "dept-b") == 0, "spend must not follow the user"
    print("  dept-change: spend stays with the submit-time department (stamp), not current")


def test_api_budget_429():
    """The document API refuses translation for an over-budget department (429,
    zero jobs enqueued) and allows a department with no budget."""
    c = TestClient(app)
    # An over-budget dept user with a docs key. Reuse mktg's cap (100) from above,
    # which is already blown by 465 spent.
    m1 = security.get_by_email("m1@x.co")
    _k, m1_docs = apikeys.create_key(m1.id, "m1 docs", scopes="api docs")
    H_M1 = {"X-Loc-Token": m1_docs}

    # Upload a doc as m1 (lands in the mktg inbox).
    register_provider("claude", LiveStubProvider())
    try:
        files = {
            "file": (
                "b.docx",
                dx(["One line."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        }
        r = c.post("/api/documents", headers=H_M1, files=files)
        assert r.status_code == 200, (r.status_code, r.text)
        doc_id = r.json()["document_id"]

        before = len(_jobs_for_doc(doc_id))
        r = c.post(
            f"/api/documents/{doc_id}/translate", headers=H_M1, json={"targets": ["fr-FR", "de-DE"]}
        )
        assert r.status_code == 429, (r.status_code, r.text)
        body = r.json()
        assert body["ok"] is False and body.get("remaining") == 0, body
        assert body["error"] == budgets.EXHAUSTED_MESSAGE, body
        after = len(_jobs_for_doc(doc_id))
        assert after == before, (
            f"over-budget translate must enqueue ZERO jobs (before={before} after={after})"
        )
        print("  api: over-budget dept -> 429 {remaining:0}, zero jobs enqueued")

        # A user with NO department (unlimited) translates normally (202).
        u = security.create_user("free@x.co", "Free", Role.TRANSLATOR, "pw-fixture-4417")
        _k, u_docs = apikeys.create_key(u.id, "free docs", scopes="api docs")
        H_U = {"X-Loc-Token": u_docs}
        r = c.post(
            "/api/documents",
            headers=H_U,
            files={
                "file": (
                    "f.docx",
                    dx(["Free line."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, r.text
        fdoc = r.json()["document_id"]
        r = c.post(f"/api/documents/{fdoc}/translate", headers=H_U, json={"targets": ["fr-FR"]})
        assert r.status_code == 202, (r.status_code, r.text)
        assert len(r.json()["jobs"]) == 1, r.json()
        print("  api: no-budget (no dept) user -> 202, one job enqueued")
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)


def test_rate_limit():
    """Rate limiter 429s after N in the window, recovers as hits age out, and is
    disabled by rate_limit=0."""
    ratelimit.reset()
    # Deterministic clock via the now= arg (avoids sleeping a real minute).
    settings.set_many({"api_rate_limit_per_minute": "3"})
    t0 = 1000.0
    for i in range(3):
        allowed, retry = ratelimit.check("user:x", now=t0 + i)
        assert allowed and retry == 0, (i, allowed, retry)
    # 4th within the window -> blocked with a positive Retry-After.
    allowed, retry = ratelimit.check("user:x", now=t0 + 3)
    assert not allowed and retry > 0, (allowed, retry)
    # A DIFFERENT identity is not affected (per-identity, not global).
    allowed, _ = ratelimit.check("user:y", now=t0 + 3)
    assert allowed, "per-identity limiter must not leak across callers"
    # Once the window has fully passed, the same identity is allowed again.
    allowed, retry = ratelimit.check("user:x", now=t0 + 61)
    assert allowed and retry == 0, (allowed, retry)
    print("  ratelimit: 429 after N in window; per-identity; recovers after the window")

    # rate_limit=0 disables entirely.
    ratelimit.reset()
    settings.set_many({"api_rate_limit_per_minute": "0"})
    for i in range(50):
        allowed, retry = ratelimit.check("user:z", now=t0 + i * 0.001)
        assert allowed and retry == 0, (i, allowed, retry)
    print("  ratelimit: api_rate_limit_per_minute=0 disables the limiter")

    # End-to-end through the API: a tiny cap 429s the SECOND request.
    settings.set_many({"api_rate_limit_per_minute": "1"})
    ratelimit.reset()
    c = TestClient(app)
    u = security.get_by_email("free@x.co")
    _k, u_docs = apikeys.create_key(u.id, "rl docs", scopes="api docs")
    H = {"X-Loc-Token": u_docs}
    files = {
        "file": (
            "r.docx",
            dx(["Line."]),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    r1 = c.post(
        "/api/documents",
        headers=H,
        files={
            "file": (
                "r1.docx",
                dx(["Line."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r1.status_code == 200, r1.text
    r2 = c.post("/api/documents", headers=H, files=files)
    assert r2.status_code == 429, (r2.status_code, r2.text)
    assert "Retry-After" in r2.headers, r2.headers
    print("  ratelimit: enforced on POST /api/documents (2nd call -> 429 + Retry-After)")
    # Leave the limiter disabled so nothing leaks into other suites/state.
    settings.set_many({"api_rate_limit_per_minute": "0"})
    ratelimit.reset()


def main():
    init_db()
    security.seed_initial()  # admin + auth ON
    settings.set_many({"audit_enabled": "1"})
    test_spend_aggregation()
    test_check()
    test_inflight_est_tokens()
    test_dept_change_spend_stays()
    test_api_budget_429()
    test_rate_limit()
    settings.set_many({"security_auth_required": "0"})
    print("BUDGETS OK")


if __name__ == "__main__":
    main()
