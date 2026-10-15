"""Background authoring jobs (app/web/author_jobs.py), offline + hermetic.

THE PROBLEM: with a 27B local model at about 7 tokens a second, "Describe a workflow" took
194 to 222 s per draft. A proxy such as Cloudflare cuts a browser request off at 100 s (524),
so the person saw a blank error page, clicked again, and started three paid drafts. The
drafting call now runs as a background job: a fast engine still answers inline, a slow one
gets a progress page or a pending handle, and repeat clicks join the running draft.

A SLOW mock provider (the real OpenAICompatibleProvider over an httpx.MockTransport whose
handler blocks on a threading.Event gate) stands in for the slow local model; INLINE_WAIT_S
is cut to a fraction of a second so "slow" is "still gated after the inline wait".

Covers:
  1. fast engine: POST /workflows/author renders the review inline, exactly as before.
  2. slow engine: POST answers 303 to /workflows/draft-jobs/N at once; the page shows progress and
     the .json poll says running; repeat clicks (same text and different text) JOIN the
     running job (no second provider call); once the gate opens the SAME URL renders the
     review and the poll says completed.
  3. a failed draft shows the clean error page (502) and the poll says failed.
  4. only the person who started a job can read it (another actor: the same 404).
  5. step prompt author.json: 202 pending + poll_url, join on repeat, poll returns the draft;
     the no-JS /workflows/{id}/author redirects to the progress page that renders the builder.
  7. Fix this for me (/workflows/author/repair) runs as a job and renders the repaired review.
  8. timeout message after GIVE_UP_S; restart (fail_interrupted) shows a clean failure.
  9. every slot busy: a clean 503 message, nothing started.
ASCII-only.
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_authorbg_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine as db_engine
from app.db import init_db
from app.models import Job, JobStatus, Workflow
from app.services.engines import create_profile, get_profile
from app.services.providers import OpenAICompatibleProvider, register_provider
from app.web import author_jobs, background_calls

init_db()

author_jobs.INLINE_WAIT_S = 0.4

GOOD_WF = (
    '{"name": "Slow Draft WF", "steps": [{"name": "Translate", "step_type": "llm_agent", '
    '"prompt_text": "Translate {text} to {target_locale}."}]}'
)
GOOD_PROJECT = '{"name": "Slow Project", "source_locale": "en-US", "target_locales": ["fr-FR"]}'


class SlowEngine:
    """An engine whose every call waits on `gate` (unless it is already open)."""

    _seq = 0

    def __init__(self, content: str, *, status: int = 200, open_gate: bool = False):
        SlowEngine._seq += 1
        self.content = content
        self.status = status
        self.calls = 0
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()

        def handler(req):
            self.calls += 1
            assert self.gate.wait(30), "test gate never opened"
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"message": "engine down"}})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": self.content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 20},
                },
            )

        p = create_profile(
            f"SlowBg{SlowEngine._seq}",
            "openai_compatible",
            base_url="http://localhost:1234/v1",
            api_key="sk-local",
            model="local-model",
        )
        self.ref = f"engine:{p.id}"
        register_provider(
            self.ref,
            OpenAICompatibleProvider(
                profile=get_profile(p.id), transport=httpx.MockTransport(handler)
            ),
        )


_APP = None


def client() -> TestClient:
    global _APP
    if _APP is None:
        from app.main import app

        _APP = app
    return TestClient(_APP)


def _job_id(location: str) -> int:
    assert location.startswith("/workflows/draft-jobs/"), location
    return int(location.split("/workflows/draft-jobs/")[1].split("?")[0])


def _wait_done(jid: int, timeout: float = 20.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(db_engine) as s:
            job = s.get(Job, jid)
            if job.status != JobStatus.RUNNING:
                return job
        time.sleep(0.05)
    raise AssertionError(f"author job {jid} never finished")


def check_fast_engine_inline():
    eng = SlowEngine(GOOD_WF, open_gate=True)
    r = client().post("/workflows/author", data={"description": "fast", "provider": eng.ref})
    assert r.status_code == 200, r.status_code
    assert "Review the drafted workflow" in r.text and "Slow Draft WF" in r.text
    assert eng.calls == 1, eng.calls
    print("  1. fast engine: review renders inline in the POST, one provider call")


def check_slow_engine_job_and_join():
    eng = SlowEngine(GOOD_WF)
    c = client()
    t0 = time.monotonic()
    r = c.post(
        "/workflows/author",
        data={"description": "slow one", "provider": eng.ref},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert time.monotonic() - t0 < 10, "the POST must not hold the request for the draft"
    jid = _job_id(r.headers["location"])
    assert "joined" not in r.headers["location"], r.headers["location"]

    page = c.get(f"/workflows/draft-jobs/{jid}")
    assert page.status_code == 200 and "Drafting your workflow" in page.text, page.status_code
    assert "slow one" in page.text, "the progress page shows what was asked for"
    poll = c.get(f"/workflows/draft-jobs/{jid}.json").json()
    assert poll["status"] == "running" and poll["pending"] is True and poll["job_id"] == jid, poll

    # Repeat clicks while it runs: same text, then different text. Neither starts a draft.
    r2 = c.post(
        "/workflows/author",
        data={"description": "slow one", "provider": eng.ref},
        follow_redirects=False,
    )
    assert r2.status_code == 303 and _job_id(r2.headers["location"]) == jid, r2.headers
    assert "joined=same" in r2.headers["location"], r2.headers["location"]
    r3 = c.post(
        "/workflows/author",
        data={"description": "something else", "provider": eng.ref},
        follow_redirects=False,
    )
    assert _job_id(r3.headers["location"]) == jid and "joined=other" in r3.headers["location"]
    joined_page = c.get(r3.headers["location"])
    assert "did not start another one" in joined_page.text, "the joined click is explained"
    assert eng.calls == 1, ("repeat clicks must not start a second provider call", eng.calls)

    eng.gate.set()
    job = _wait_done(jid)
    assert job.status == JobStatus.COMPLETED, (job.status, job.error)
    done = c.get(f"/workflows/draft-jobs/{jid}")
    assert done.status_code == 200 and "Review the drafted workflow" in done.text
    assert "Slow Draft WF" in done.text and 'name="s0_prompt_text"' in done.text
    assert 'value="slow one"' in done.text or "slow one" in done.text, "description carried"
    poll = c.get(f"/workflows/draft-jobs/{jid}.json").json()
    assert poll["status"] == "completed" and poll["ok"] is True, poll
    assert poll["fields"]["name"] == "Slow Draft WF", poll
    assert eng.calls == 1, eng.calls
    # A RUNNING row with no live worker (its thread died without recording an outcome) is
    # never joined: a new click starts a fresh draft.
    with Session(db_engine) as s:
        dead = Job(
            kind=background_calls.KIND,
            payload={"author_kind": "workflow_author", "org_id": None, "fp": "x"},
            created_by="",
            status=JobStatus.RUNNING,
        )
        s.add(dead)
        s.commit()
        dead_id = dead.id
    eng3 = SlowEngine(GOOD_WF, open_gate=True)
    r5 = c.post("/workflows/author", data={"description": "after a dead row", "provider": eng3.ref})
    assert r5.status_code == 200 and eng3.calls == 1, (r5.status_code, eng3.calls, dead_id)
    with Session(db_engine) as s:
        s.get(Job, dead_id).status = JobStatus.FAILED
        s.commit()
    # A new click after the draft finished starts a fresh one (no stale join).
    eng2 = SlowEngine(GOOD_WF, open_gate=True)
    r4 = c.post("/workflows/author", data={"description": "again", "provider": eng2.ref})
    assert r4.status_code == 200 and eng2.calls == 1
    print(
        "  2. slow engine: 303 to the progress page at once, poll running, two repeat "
        "clicks joined (1 provider call), then the same URL renders the review"
    )


def check_failed_draft():
    eng = SlowEngine(GOOD_WF, status=500)
    c = client()
    r = c.post(
        "/workflows/author",
        data={"description": "will fail", "provider": eng.ref},
        follow_redirects=False,
    )
    jid = _job_id(r.headers["location"])
    eng.gate.set()
    job = _wait_done(jid)
    assert job.status == JobStatus.FAILED and job.error, (job.status, job.error)
    page = c.get(f"/workflows/draft-jobs/{jid}")
    assert page.status_code in (400, 502), page.status_code
    assert "Traceback" not in page.text
    poll = c.get(f"/workflows/draft-jobs/{jid}.json").json()
    assert poll["status"] == "failed" and poll["ok"] is False and poll["error"], poll
    print(f"  3. failed draft: clean error page ({page.status_code}), poll says failed")


def check_owner_only():
    eng = SlowEngine(GOOD_WF, open_gate=True)
    c = client()
    c.post("/workflows/author", data={"description": "mine", "provider": eng.ref})
    with Session(db_engine) as s:
        job = s.exec(
            select(Job).where(Job.kind == background_calls.KIND).order_by(Job.id.desc())
        ).first()
        job.created_by = "someone-else@example.com"
        s.add(job)
        s.commit()
        jid = job.id
    a = c.get(f"/workflows/draft-jobs/{jid}.json")
    b = c.get("/workflows/draft-jobs/999999.json")
    assert a.status_code == b.status_code == 404 and a.json() == b.json(), (a.text, b.text)
    assert c.get(f"/workflows/draft-jobs/{jid}").status_code == 404
    print("  4. another person's job answers the same 404 as a missing id")


def _new_workflow() -> int:
    with Session(db_engine) as s:
        wf = Workflow(name="BG step host")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        return wf.id


def check_step_prompt():
    wid = _new_workflow()
    eng = SlowEngine("You are a strict reviewer. Fix {prior} for {target_locale}.")
    c = client()
    r = c.post(f"/workflows/{wid}/author.json", data={"description": "strict", "provider": eng.ref})
    assert r.status_code == 202, (r.status_code, r.text)
    d = r.json()
    assert d["pending"] is True and d["poll_url"] == f"/workflows/draft-jobs/{d['job_id']}.json", d
    r2 = c.post(
        f"/workflows/{wid}/author.json", data={"description": "strict", "provider": eng.ref}
    )
    assert r2.status_code == 202 and r2.json()["job_id"] == d["job_id"]
    assert r2.json().get("existing") is True and r2.json().get("notice"), r2.json()
    # Gate finding 2: a DIFFERENT request while one runs (another workflow's step, other
    # text) is refused with 409, never handed the running draft as its own.
    other = c.post(
        f"/workflows/{_new_workflow()}/author.json",
        data={"description": "something else", "provider": eng.ref},
    )
    assert other.status_code == 409, (other.status_code, other.text)
    assert other.json()["code"] == "draft_in_progress" and other.json()["ok"] is False
    eng.gate.set()
    _wait_done(d["job_id"])
    p = c.get(d["poll_url"]).json()
    assert p["status"] == "completed" and "strict reviewer" in p["draft"], p
    assert eng.calls == 1, eng.calls

    # No-JS fallback: redirect to the progress page, which renders the builder + draft.
    eng2 = SlowEngine("Polish {prior} for {target_locale}.")
    r = c.post(
        f"/workflows/{wid}/author",
        data={"description": "polish", "provider": eng2.ref},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    jid = _job_id(r.headers["location"])
    wait_page = c.get(f"/workflows/draft-jobs/{jid}").text
    assert "Drafting your step prompt" in wait_page
    assert f'href="/workflows/{wid}"' in wait_page, (
        "the step prompt wait links back to its workflow"
    )
    eng2.gate.set()
    _wait_done(jid)
    page = c.get(f"/workflows/draft-jobs/{jid}")
    assert page.status_code == 200 and "Polish {prior}" in page.text, page.status_code
    print("  5. step prompt: 202 + poll_url, repeat joined, poll returns the draft; no-JS builder")


def check_repair():
    fixed = (
        '{"name": "Repaired WF", "steps": [{"name": "T", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to {target_locale}."}]}'
    )
    eng = SlowEngine(fixed)
    c = client()
    form = {
        "step_count": "1",
        "name": "Needs Repair",
        "customer_id": "",
        "s0_step_type": "llm_agent",
        "s0_prompt_text": "Translate to Spanish.",
        "notes_for_reviewer": "",
        "description": "repair me",
        "provider": eng.ref,
        "model": "",
    }
    r = c.post("/workflows/author/repair", data=form, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text[:300])
    jid = _job_id(r.headers["location"])
    assert "Fixing your workflow draft" in c.get(f"/workflows/draft-jobs/{jid}").text
    eng.gate.set()
    job = _wait_done(jid)
    assert job.status == JobStatus.COMPLETED, job.error
    page = c.get(f"/workflows/draft-jobs/{jid}")
    assert page.status_code == 200 and "Review the drafted workflow" in page.text
    assert eng.calls >= 1, "the repair called the engine"
    assert "Translate {text} to {target_locale}." in page.text, "repaired prompt on the form"
    print("  7. Fix this for me: runs as a job, the progress URL renders the repaired review")


def check_timeout_and_restart():
    eng = SlowEngine(GOOD_WF)
    c = client()
    r = c.post(
        "/workflows/author",
        data={"description": "stuck", "provider": eng.ref},
        follow_redirects=False,
    )
    jid = _job_id(r.headers["location"])
    old = author_jobs.GIVE_UP_S
    author_jobs.GIVE_UP_S = 0
    try:
        page = c.get(f"/workflows/draft-jobs/{jid}")
        assert "much longer than expected" in page.text, page.text[:500]
        assert c.get(f"/workflows/draft-jobs/{jid}.json").json()["timed_out"] is True
        # Gate finding 1: a draft past GIVE_UP_S is never joined; a new click starts fresh.
        fresh = SlowEngine(GOOD_WF, open_gate=True)
        r2 = c.post(
            "/workflows/author",
            data={"description": "stuck", "provider": fresh.ref},
            follow_redirects=False,
        )
        assert r2.status_code == 200 and fresh.calls == 1, (r2.status_code, fresh.calls)
    finally:
        author_jobs.GIVE_UP_S = old
    # A restart while it runs: boot fails the row; the page says so cleanly.
    background_calls.fail_interrupted()
    page = c.get(f"/workflows/draft-jobs/{jid}")
    assert page.status_code >= 400 and "restart" in page.text.lower(), page.status_code
    eng.gate.set()  # let the orphaned worker thread finish
    time.sleep(0.3)
    print("  8. timeout message after GIVE_UP_S; a restart shows a clean failure")


def check_busy():
    taken = 0
    while background_calls._SLOTS.acquire(blocking=False):
        taken += 1
    try:
        eng = SlowEngine(GOOD_WF, open_gate=True)
        c = client()
        r = c.post("/workflows/author", data={"description": "busy", "provider": eng.ref})
        assert r.status_code == 503 and "Too many AI drafts" in r.text, r.status_code
        j = c.post(
            f"/workflows/{_new_workflow()}/author.json",
            data={"description": "busy", "provider": eng.ref},
        )
        assert j.status_code == 503 and "Too many AI drafts" in j.json()["error"]
        assert eng.calls == 0
    finally:
        for _ in range(taken):
            background_calls._SLOTS.release()
    print("  9. every slot busy: clean 503, nothing started")


def _login(email: str, password: str) -> TestClient:
    from tests._csrf import session_data

    c = client()
    c.get("/auth/login")  # mints the pre-login CSRF token
    r = c.post(
        "/auth/login",
        data={
            "email": email,
            "password": password,
            "next": "/",
            "csrf_token": session_data(c).get("csrf"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and session_data(c).get("uid"), (r.status_code, r.text[:300])
    return c


def _resign(c: TestClient, data: dict) -> None:
    """Re-sign the session cookie with changed contents (the app's own codec)."""
    import json
    from base64 import b64encode

    import itsdangerous

    from app import config

    signer = itsdangerous.TimestampSigner(str(config.SESSION_SECRET))
    jar = [ck for ck in c.cookies.jar if ck.name == "session"]
    where = {"domain": jar[0].domain, "path": jar[0].path} if jar else {}
    signed = signer.sign(b64encode(json.dumps(data).encode("utf-8"))).decode("utf-8")
    c.cookies.delete("session")
    c.cookies.set("session", signed, **where)


def check_login_wall():
    """The job URLs once started with "/auth" ("/author-jobs/...") and so matched the
    login wall's "/auth" allow-prefix: the full sign-in check never ran on them. They now
    live under /workflows/draft-jobs/ and get the WHOLE wall: signed out -> sign-in page,
    idle past the limit -> the expired answer, a password that must change -> the change
    page. LAST: turns the login wall on for the rest of the process."""
    from datetime import datetime

    from app.main import _ALLOW_EXACT, _ALLOW_PREFIX
    from app.models import Role
    from app.services import security, settings
    from app.services import security_client_ip_one_authority_ as sec_mod
    from tests._csrf import session_data

    probe = "/workflows/draft-jobs/1"
    assert not probe.startswith(_ALLOW_PREFIX) and probe not in _ALLOW_EXACT, "wall exempt"
    # The pre-fix path was exempt: this is exactly the bug the rename closes.
    assert "/author-jobs/1".startswith(_ALLOW_PREFIX), "the old path matched the /auth prefix"

    security.seed_initial()  # turns the login wall on
    email, pw = "wall-admin@x.com", "wall-suite-pass-2026"  # pregate: allow-secret fixture login
    security.create_user(email, "Wall Admin", Role.ADMIN, pw, enforce_policy=False)
    settings.set_many({"session_idle_minutes": "10"})
    with Session(db_engine) as s:
        job = Job(
            kind=background_calls.KIND,
            payload={"author_kind": "workflow_author", "org_id": None, "fp": "w", "params": {}},
            created_by=email,
            status=JobStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        s.add(job)
        s.commit()
        jid = job.id
    paths = (f"/workflows/draft-jobs/{jid}", f"/workflows/draft-jobs/{jid}.json")

    # Signed out: the sign-in page, never the route's own 403.
    anon = client()
    for path in paths:
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303, (path, r.status_code, r.text[:200])
        assert r.headers["location"].startswith("/auth/login?next="), r.headers["location"]

    clock = [1_900_000_000]
    real_now = sec_mod._now
    sec_mod._now = lambda: clock[0]
    try:
        c = _login(email, pw)
        assert c.get(paths[0]).status_code == 200, "the owner sees the progress page"
        assert c.get(paths[1]).json()["status"] == "running"
        # Idle past the limit: the expired answer (navigation -> sign-in with expired=1,
        # a fetch with the CSRF header -> 401 session_expired).
        clock[0] += 11 * 60
        r = c.get(paths[0], follow_redirects=False)
        assert r.status_code == 303 and "expired=1" in r.headers["location"], r.headers
        c2 = _login(email, pw)
        tok = session_data(c2).get("csrf") or "t"
        clock[0] += 11 * 60
        r = c2.get(paths[1], headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 401 and r.json()["error"] == "session_expired", r.text[:200]
    finally:
        sec_mod._now = real_now

    # A password that must change: only the change page.
    c3 = _login(email, pw)
    data = session_data(c3)
    data["pwc"] = 1
    _resign(c3, data)
    for path in paths:
        r = c3.get(path, follow_redirects=False)
        assert r.status_code == 303, (path, r.status_code)
        assert r.headers["location"].startswith("/auth/password-change"), r.headers["location"]
    with Session(db_engine) as s:
        s.get(Job, jid).status = JobStatus.FAILED
        s.commit()
    print(" 10. the job URLs get the full login wall (signed out, idle, pwc)")


def main() -> None:
    check_fast_engine_inline()
    check_slow_engine_job_and_join()
    check_failed_draft()
    check_owner_only()
    check_step_prompt()
    check_repair()
    check_timeout_and_restart()
    check_busy()
    check_login_wall()  # LAST: turns the login wall on
    print("AUTHOR BACKGROUND OK")


if __name__ == "__main__":
    main()
