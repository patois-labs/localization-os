"""Live translate stream: fail-closed document scope + a state-changing start.

Security review 2026-09, issues #282 (HIGH) and #283 (the reviewer's probe
r2-webapp/probe_translate_stream.py, turned into regressions here):

  #282  `GET /documents/{id}/translate-stream` ran its visibility check only inside
        `if doc:`; `_visible_doc` returns None for a key project's internal document,
        so the check was skipped and the stream ran: a NON-MEMBER viewer read the key
        project's stored translated values and could start paid MT that wrote into it.
  #283  the stream start was a GET, so the middleware's Origin check, CSRF token and
        write-role gate (mutating methods only) never applied: a viewer MEMBER ran MT
        into segments, and a cross-site link could start it for a signed-in user.

Proves, with the login wall ON, key projects and project scoping enabled:
  * a non-member viewer: the internal key document id is a 404 (no body, no MT call);
    the old GET is refused (405) and starts nothing.
  * a non-member translator on a normal document: 404 (no existence oracle).
  * a viewer member: 403, no MT call, the segment is untouched.
  * a cross-site POST (foreign Origin), a POST without the CSRF token and a plain GET
    start nothing.
  * a translator member streams normally (segment + summary + done, segment written).
  * the translate service's own role rule (`can_start_mt` / `require_mt_role`), and the
    /api translate twin refuses a viewer's docs key (403) and accepts a translator's.
  * the Document page starts the stream with a POST carrying the CSRF header.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). ASCII-only prints.
Final line: LIVE TRANSLATE STREAM OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_livestream_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Job,
    Project,
    ProjectMember,
    ProjectType,
    Role,
    Segment,
    User,
)
from app.services import apikeys, content, security, settings, translate
from app.services import keys as keys_svc
from tests._csrf import csrf_for

PW = "livestream-pw-123456789"
CSRF_HEADER = "X-CSRF-Token"
HIDDEN_KEY_VALUE = "GEHEIM: Starttermin ist der 4. Mai"

# Count every call into the direct-MT generator. The route calls it through the module
# attribute (app.kernel.translate IS app.services.translate), so this wrapper sees it.
_CALLS = []
_real_stream = translate.pretranslate_stream


def _counting_stream(*args, **kwargs):
    _CALLS.append(args[1] if len(args) > 1 else kwargs.get("doc_id"))
    yield from _real_stream(*args, **kwargs)


translate.pretranslate_stream = _counting_stream


def _setup():
    init_db()
    settings.set_many(
        {"security_auth_required": "1", "key_projects_enabled": "1", "project_scoping": "1"}
    )
    settings.invalidate_cache()
    security.create_user("adm@x.com", "adm", Role.ADMIN, PW)
    with Session(engine) as s:
        cust = Customer(name="Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        kp = content.project_create(
            s,
            "SecretKeys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cust.id),
            project_type=ProjectType.KEY_BASED,
        )
        key = keys_svc.create_key(
            s, kp, "secret.greeting", namespace="", base_value="Launch is May 4", actor="fx"
        )
        keys_svc.set_value(
            s, key, "de-DE", HIDDEN_KEY_VALUE, origin=keys_svc.ORIGIN_HUMAN, actor="fx"
        )
        internal_ids = [
            d.id for d in s.exec(select(Document).where(Document.project_id == kp.id)).all()
        ]
        dp = content.project_create(
            s, "DocProj", source_locale="en-US", target_locales="de-DE", customer_id=str(cust.id)
        )
        doc = Document(project_id=dp.id, filename="a.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id, order_index=0, source_text="Hello world", target_locale="de-DE"
            )
        )
        s.commit()
        return internal_ids, dp.id, doc.id


def _login(email, role, member_of=None):
    security.create_user(email, email, role, PW)
    if member_of is not None:
        with Session(engine) as s:
            uid = s.exec(select(User).where(User.email == email)).first().id
            s.add(ProjectMember(project_id=member_of, user_id=uid))
            s.commit()
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def _target(doc_id):
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).first()
        return seg.target_text


def _no_stream(r, what):
    assert "event-stream" not in r.headers.get("content-type", ""), f"{what}: streamed"
    assert HIDDEN_KEY_VALUE not in r.text, f"{what}: leaked a stored key value"


def _post(c, doc_id, **kw):
    return c.post(
        f"/documents/{doc_id}/translate-stream",
        data={"locale": "de-DE"},
        follow_redirects=False,
        **kw,
    )


def _member(email, role, pid, department=""):
    """A signed-in client for a new user who is a member of project `pid`."""
    c = _login(email, role, member_of=pid)
    if department:
        with Session(engine) as s:
            u = s.exec(select(User).where(User.email == email)).first()
            u.department = department
            s.add(u)
            s.commit()
    return c


def _new_doc(pid, name, text="Budgeted hello world sentence."):
    with Session(engine) as s:
        doc = Document(project_id=pid, filename=name, fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(Segment(document_id=doc.id, order_index=0, source_text=text, target_locale="de-DE"))
        s.commit()
        return doc.id


def _new_project(name, provider=""):
    with Session(engine) as s:
        p = content.project_create(s, name, source_locale="en-US", target_locales="de-DE")
        if provider:
            p.default_provider = provider
            s.add(p)
            s.commit()
        return p.id


def _live_jobs(doc_id):
    with Session(engine) as s:
        rows = s.exec(select(Job).where(Job.kind == "translate_document")).all()
        return [
            j
            for j in rows
            if (j.payload or {}).get("document_id") == doc_id
            and (j.payload or {}).get("live_stream")
        ]


def _wait_jobs_idle(timeout=60.0):
    """Block until no queued (enqueued, non-live) job is QUEUED or RUNNING."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(engine) as s:
            busy = [
                j
                for j in s.exec(select(Job).where(Job.status.in_(["queued", "running"]))).all()
                if not (j.payload or {}).get("live_stream")
            ]
        if not busy:
            return
        time.sleep(0.1)
    raise AssertionError(f"background jobs still busy after {timeout}s: {[j.id for j in busy]}")


def _raw_stream(client, doc_id, stop_after):
    """POST the stream over raw ASGI and disconnect once `stop_after` segment events have
    arrived (the client closing the connection mid-stream, as a script could)."""
    import asyncio

    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    body = b"locale=de-DE"
    st = {"sent": False, "n": 0}

    async def run():
        cut = asyncio.Event()

        async def receive():
            if not st["sent"]:
                st["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await cut.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.body" and msg.get("body"):
                st["n"] += msg["body"].count(b'"type": "segment"')
                if st["n"] >= stop_after:
                    cut.set()
                    await asyncio.sleep(0.3)

        path = f"/documents/{doc_id}/translate-stream"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 5555),
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
                (b"cookie", cookie.encode()),
                (CSRF_HEADER.lower().encode(), client.headers[CSRF_HEADER].encode()),
                (b"origin", b"http://testserver"),
            ],
        }
        await app(scope, receive, send)

    asyncio.run(run())
    return st["n"]


def check_cutoff_charge():
    """Gate round 2 M1-R: a stream the client cuts off before its summary is charged the
    LARGER of its reservation and what it really spent up to the cut, and the reservation
    itself counts the prompt the stream sends plus expected output."""
    import gc
    import time

    from app.services import jobs, prompts
    from app.services.segment import est_tokens

    pid = _new_project("CutOff")
    cu = _member("cut@x.com", Role.TRANSLATOR, pid)

    def ten(name):
        did = _new_doc(pid, name, text="Segment zero of ten.")
        with Session(engine) as s:
            for i in range(1, 10):
                s.add(
                    Segment(
                        document_id=did,
                        order_index=i,
                        source_text=f"Segment {i} of ten, a short line.",
                        target_locale="de-DE",
                    )
                )
            s.commit()
        return did

    # Honest reservation: at least the base system prompt per untranslated segment.
    est_doc = ten("est.txt")
    system, prompt, _p = prompts.translation_prompt("", "en-US", "de-DE", [])
    floor = 10 * est_tokens(system + prompt)
    jid = jobs.reserve_live_translate(est_doc, "de-DE")
    with Session(engine) as s:
        row = s.get(Job, jid)
        assert row.result["tokens_in"] >= floor, (row.result, floor)
        assert row.result["tokens_out"] > 0 and row.payload["est_tokens"] >= floor
    jobs.settle_live_translate(jid, None)

    # Measured > estimate must win: shrink the reservation so only the running totals can
    # carry the charge, then compare cut-offs with a completed twin.
    real_est = jobs._live_estimate
    jobs._live_estimate = lambda *_a, **_k: (1, 0)
    try:
        full, late, mid = ten("full.txt"), ten("late.txt"), ten("mid.txt")
        r = _post(cu, full)
        assert r.status_code == 200 and '"type": "done"' in r.text
        (twin,) = _live_jobs(full)
        spent = twin.result["tokens_in"] + twin.result["tokens_out"]
        assert twin.status == "completed" and spent > 1
        # #364: the settled row stores the stream's booked USD (mock = an exact 0.0, not
        # NULL), and the translator's SSE never carries the figure (manager+ data).
        assert twin.cost_usd == 0.0, twin.cost_usd
        assert "cost_usd" not in r.text, "the live SSE leaked the AI cost to a translator"
        # Cut as the last segments arrive, before the summary (9 of 10 segment events
        # seen: the tenth is already in flight, as in the gate's probe).
        seen = _raw_stream(cu, late, 9)
        assert seen >= 9, f"late cut-off saw {seen} segments"
        seen_mid = _raw_stream(cu, mid, 3)
        assert 3 <= seen_mid < 10, f"mid cut-off saw {seen_mid} segments"
        gc.collect()
        time.sleep(0.3)
    finally:
        jobs._live_estimate = real_est
    (late_row,) = _live_jobs(late)
    (mid_row,) = _live_jobs(mid)
    late_charge = late_row.result["tokens_in"] + late_row.result["tokens_out"]
    mid_charge = mid_row.result["tokens_in"] + mid_row.result["tokens_out"]
    assert late_row.status == "failed", late_row.status
    assert late_row.cost_usd is None and mid_row.cost_usd is None, "#364: a cut-off stays NULL"
    # At least what the nine segments it saw cost the completed twin (per-segment equal).
    assert late_charge * 10 >= spent * 9, f"late cut-off charged {late_charge}, twin {spent}"
    assert mid_row.status == "failed" and mid_charge > 1, (mid_row.status, mid_row.result)
    print(
        f"  M1-R: cut-off charged max(estimate, measured): late {late_charge} (twin"
        f" {spent}); mid-stream {mid_charge}; reservation >= prompt floor {floor}"
    )


def check_orphan_recovery():
    """Gate round 2 L2-R: a RUNNING live row left by a crash has no queue entry, so the
    durable "db" queue's recovery never saw it and it blocked /api translate for that
    document and locale forever. Boot recovery now fails it on that backend too, keeping
    its charge."""
    from app.ports import jobqueue
    from app.services import jobs
    from app.web.api_discovery import _active_translate_locales

    pid = _new_project("Crash")
    did = _new_doc(pid, "crash.txt")
    jid = jobs.reserve_live_translate(did, "de-DE", department="Crashers")
    assert "de-DE" in _active_translate_locales(did)
    real_get = jobqueue.get
    jobqueue.get = lambda: jobqueue.DbJobQueue()
    try:
        touched = jobs.recover_interrupted(at_boot=True)
    finally:
        jobqueue.get = real_get
    with Session(engine) as s:
        row = s.get(Job, jid)
        assert row.status == "failed", row.status
        assert (row.result or {}).get("tokens_in", 0) > 0, "recovery dropped the charge"
    assert touched >= 1 and "de-DE" not in _active_translate_locales(did)
    print("  L2-R: db-queue boot recovery fails an orphaned live row, charge kept, /api unblocked")


def check_route_role_rule(tr, doc_id):
    """Gate L2: the ROUTE applies the translate service's role rule itself. The middleware
    stops a viewer before the route, so pin the route by making the service rule refuse
    a translator (who passes the middleware): both MT start routes must answer 403 and
    start nothing. Deleting either route's `require_mt_role` block turns this red."""
    real = translate.can_start_mt
    translate.can_start_mt = lambda _user: False
    try:
        before, jobs_before = len(_CALLS), _count_jobs()
        r = _post(tr, doc_id)
        assert r.status_code == 403, f"route role rule (stream): {r.status_code}"
        assert "does not allow starting machine translation" in r.text
        _no_stream(r, "route role rule (stream)")
        r = tr.post(
            f"/documents/{doc_id}/translate", data={"locale": "de-DE"}, follow_redirects=False
        )
        assert r.status_code == 403, f"route role rule (background): {r.status_code}"
        assert len(_CALLS) == before and _count_jobs() == jobs_before, "a refused start ran"
    finally:
        translate.can_start_mt = real
    print("  L2: both web MT routes apply the service role rule themselves (403, nothing run)")


def _count_jobs():
    with Session(engine) as s:
        return len(s.exec(select(Job)).all())


def check_stream_budgets(_dpid):
    """Gate M1: the live stream's own spend is metered (a translate_document Job row,
    reserved before the first provider call and settled after), so a department or
    project cap refuses the NEXT stream after the first one's spend, and two concurrent
    starts cannot both pass a cap that only one fits."""
    import threading
    import time

    from app.services import budgets, jobs, pricing, project_budgets

    # 1. Department cap, sequential: the first stream spends, the second is 429.
    pid = _new_project("DeptCapped")
    legal = _member("legal@x.com", Role.TRANSLATOR, pid, department="Legal")
    with Session(engine) as s:
        budgets.upsert_budget(s, "Legal", 1)
    d1, d2 = _new_doc(pid, "d1.txt"), _new_doc(pid, "d2.txt")
    r = _post(legal, d1)
    assert r.status_code == 200 and '"type": "done"' in r.text, (r.status_code, r.text[:200])
    (job,) = _live_jobs(d1)
    assert job.status == "completed", job.status
    assert (job.payload or {}).get("department") == "legal"
    with Session(engine) as s:
        spent = budgets.spend_this_month(s, "Legal")
    assert spent >= 1, f"the stream's spend was not metered: {spent}"
    before = len(_CALLS)
    r = _post(legal, d2)
    assert r.status_code == 429, f"second stream under a used-up department cap: {r.status_code}"
    assert "department" in r.text and len(_CALLS) == before and _target(d2) in (None, "")
    assert not _live_jobs(d2), "a refused stream opened a Job row"
    print("  M1: department cap: stream 1 metered as a Job, stream 2 refused 429, no MT call")

    # 2. Concurrency: two starts race one department cap; the check and the reservation
    #    are one step, so exactly one wins even with a slow check in between.
    pid = _new_project("DeptRace")
    ops = _member("ops@x.com", Role.TRANSLATOR, pid, department="Ops")
    with Session(engine) as s:
        budgets.upsert_budget(s, "Ops", 1)
    d3 = _new_doc(pid, "d3.txt")
    real_check = budgets.check_locked  # #622: the reservation seam

    def slow_check(*a, **k):
        out = real_check(*a, **k)
        time.sleep(0.3)
        return out

    budgets.check_locked = slow_check
    results, gate = [], threading.Barrier(2)

    def start():
        gate.wait()
        try:
            results.append(("ok", jobs.reserve_live_translate(d3, "de-DE", department="Ops")))
        except jobs.LiveBudgetRefused as exc:
            results.append(("refused", str(exc)))

    try:
        threads = [threading.Thread(target=start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        budgets.check_locked = real_check
    kinds = sorted(k for k, _v in results)
    assert kinds == ["ok", "refused"], f"concurrent starts under one cap: {results}"
    # While that reservation is still open (a stream in flight), the route refuses too.
    before = len(_CALLS)
    r = _post(ops, d3)
    assert r.status_code == 429 and len(_CALLS) == before, f"in-flight not counted: {r.status_code}"
    # A stream cut off before its summary settles FAILED and keeps its estimate as charge.
    jid = next(v for k, v in results if k == "ok")
    jobs.settle_live_translate(jid, None)
    with Session(engine) as s:
        cut = s.get(Job, jid)
        assert cut.status == "failed" and budgets.spend_this_month(s, "Ops") >= 1
    print("  M1: concurrent starts under one cap: exactly one reserved; in-flight counted")

    # 3. Project USD cap, sequential and concurrent.
    #    #361 one free-engine rule: a stream on the offline mock engine is FREE (its recorded
    #    spend is $0), so it can never trip a USD cap. 3a proves that is still the rule;
    #    3b proves the cap trips off a PAID engine's stream spend, priced by the real table
    #    (no pricing monkeypatch): a registered stub named `claude` is a non-exempt engine.
    from app.services import providers_hub
    from app.services.providers import Provider

    class _PaidStub(Provider):
        name = "claude"

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            self.last_usage = {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
            return "[de] " + str((payload or {}).get("text", "x"))

    pid = _new_project("ProjFree")
    pf = _member("pf@x.com", Role.TRANSLATOR, pid)
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, pid, 0.0001)
    f1, f2 = _new_doc(pid, "f1.txt"), _new_doc(pid, "f2.txt")
    for fid in (f1, f2):
        r = _post(pf, fid)
        assert r.status_code == 200 and '"type": "done"' in r.text, r.status_code
    with Session(engine) as s:
        free_spent = project_budgets.project_spend_this_month(s, pid)["enforced_usd"]
    assert free_spent == 0.0 and project_budgets.project_check(pid)[0] is True, (
        f"a mock-engine stream must be free and never trip a cap: {free_spent}"
    )

    providers_hub.register_provider("claude", _PaidStub())
    try:
        pid = _new_project("ProjCapped", provider="claude")
        pc = _member("pc@x.com", Role.TRANSLATOR, pid)
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, pid, 0.0001)
        p1, p2 = _new_doc(pid, "p1.txt"), _new_doc(pid, "p2.txt")
        r = _post(pc, p1)
        assert r.status_code == 200 and '"type": "done"' in r.text, r.status_code
        with Session(engine) as s:
            paid_spent = project_budgets.project_spend_this_month(s, pid)["enforced_usd"]
        assert paid_spent > 0.0, f"paid-engine stream spend not priced: {paid_spent}"
        assert project_budgets.project_check(pid)[0] is False, "stream spend not priced"
        before = len(_CALLS)
        r = _post(pc, p2)
        assert r.status_code == 429, f"second stream under a used-up project cap: {r.status_code}"
        assert "project" in r.text and len(_CALLS) == before and _target(p2) in (None, "")
    finally:
        providers_hub._OVERRIDES.pop("claude", None)

    # The concurrent race below prices every call at USD 1 (the meter's own pricing seam).
    real_cost = pricing.cost_usd
    pricing.cost_usd = lambda *_a, **_k: 1.0
    try:
        pid = _new_project("ProjRace", provider="claude")
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, pid, 0.5)
        p3 = _new_doc(pid, "p3.txt")
        results.clear()
        gate = threading.Barrier(2)

        def start_p():
            gate.wait()
            try:
                results.append(("ok", jobs.reserve_live_translate(p3, "de-DE")))
            except jobs.LiveBudgetRefused as exc:
                results.append(("refused", str(exc)))

        threads = [threading.Thread(target=start_p) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        kinds = sorted(k for k, _v in results)
        assert kinds == ["ok", "refused"], f"concurrent starts under one project cap: {results}"
    finally:
        pricing.cost_usd = real_cost
    print("  M1: project cap: stream spend priced, stream 2 refused 429; concurrent: one wins")


def main():
    internal_ids, dpid, doc_id = _setup()
    assert internal_ids, "fixture: the key project has no internal document"

    # --- #282: non-member viewer on a key project's internal document -------------
    # A viewer's POST is refused by the middleware's write-role gate before the route runs
    # (403, the same answer for ANY id, so still no existence oracle: compared below with a
    # missing id). The route's own fail-closed 404 is proven with the translator next.
    viewer = _login("viewer@x.com", Role.VIEWER)
    missing = _post(viewer, 987654)

    # Fresh response nonces differ even when the refusal page is identical.
    def body_without_nonce(response):
        import re

        return re.sub(r'nonce="[^"]+"', 'nonce=""', response.text)

    for did in internal_ids:
        before = len(_CALLS)
        r = _post(viewer, did)
        assert r.status_code in (403, 404), f"POST internal key doc {did}: {r.status_code}"
        assert (r.status_code, body_without_nonce(r)) == (
            missing.status_code,
            body_without_nonce(missing),
        ), "an internal key document answers differently from a missing one"
        _no_stream(r, "POST internal key doc")
        r = viewer.get(f"/documents/{did}/translate-stream?locale=de-DE", follow_redirects=False)
        assert r.status_code in (404, 405), f"GET internal key doc {did}: {r.status_code}"
        _no_stream(r, "GET internal key doc")
        assert len(_CALLS) == before, "an MT stream ran for a non-member on a key document"
    print("  #282: non-member viewer -> refused like a missing id, no MT call, no key value")

    # Non-member TRANSLATOR (passes the middleware role gate, so the ROUTE decides): the
    # internal key document and a normal non-member document are both 404, like missing.
    outsider = _login("outsider@x.com", Role.TRANSLATOR)
    before = len(_CALLS)
    for did in internal_ids:
        r = _post(outsider, did)
        assert r.status_code == 404, f"translator on internal key doc {did}: {r.status_code}"
        _no_stream(r, "translator on internal key doc")
    r = _post(outsider, doc_id)
    assert r.status_code == 404, f"non-member translator: {r.status_code}"
    _no_stream(r, "non-member translator")
    r = _post(outsider, 987654)
    assert r.status_code == 404, f"missing document: {r.status_code}"
    assert len(_CALLS) == before and _target(doc_id) in (None, "")
    print("  #282: non-member translator -> 404 (no existence oracle), no MT call")

    # --- #283: viewer MEMBER may not start MT ---------------------------------------
    vmember = _login("vmember@x.com", Role.VIEWER, member_of=dpid)
    before = len(_CALLS)
    r = _post(vmember, doc_id)
    assert r.status_code == 403, f"viewer member POST: {r.status_code}"
    _no_stream(r, "viewer member POST")
    r = vmember.get(f"/documents/{doc_id}/translate-stream?locale=de-DE", follow_redirects=False)
    assert r.status_code == 405, f"viewer member GET: {r.status_code}"
    _no_stream(r, "viewer member GET")
    assert len(_CALLS) == before, "a viewer member started MT"
    assert _target(doc_id) in (None, ""), "a viewer member wrote a segment"
    print("  #283: viewer member -> 403 (POST) / 405 (GET), no MT call, segment untouched")

    # --- #283: cross-site starts nothing --------------------------------------------
    tr = _login("tr@x.com", Role.TRANSLATOR, member_of=dpid)
    before = len(_CALLS)
    r = _post(tr, doc_id, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403, f"cross-origin POST: {r.status_code}"
    _no_stream(r, "cross-origin POST")
    r = tr.get(f"/documents/{doc_id}/translate-stream?locale=de-DE", follow_redirects=False)
    assert r.status_code == 405, f"cross-site style GET: {r.status_code}"
    _no_stream(r, "GET")
    saved_csrf = tr.headers.pop(CSRF_HEADER, None)
    try:
        r = _post(tr, doc_id)
        assert r.status_code in (303, 403), f"POST without the CSRF header, got {r.status_code}"
        _no_stream(r, "POST without the CSRF header")
    finally:
        if saved_csrf is not None:
            tr.headers.update({CSRF_HEADER: saved_csrf})
    assert len(_CALLS) == before and _target(doc_id) in (None, ""), "cross-site started MT"
    print("  #283: foreign Origin 403, missing CSRF refused, GET 405: nothing started")

    # --- the translator member streams normally -------------------------------------
    r = _post(tr, doc_id)
    assert r.status_code == 200, f"translator member: {r.status_code} {r.text[:200]}"
    assert "event-stream" in r.headers.get("content-type", "")
    body = r.text
    for kind in ('"type": "segment"', '"type": "summary"', '"type": "done"'):
        assert kind in body, f"stream missing {kind}: {body[:300]}"
    assert len(_CALLS) == before + 1
    assert _target(doc_id), "the translator's stream wrote no segment"
    print("  translator member streams segment + summary + done and writes the segment")

    # --- the service rule and the /api twin ------------------------------------------
    with Session(engine) as s:
        users = {u.email: u for u in s.exec(select(User)).all()}
    assert not translate.can_start_mt(users["viewer@x.com"])
    assert not translate.can_start_mt(None)
    assert translate.can_start_mt(users["tr@x.com"])
    try:
        translate.require_mt_role(users["vmember@x.com"])
        raise AssertionError("require_mt_role let a viewer through")
    except translate.MTRoleRefused:
        pass
    _row, vkey = apikeys.create_key(users["vmember@x.com"].id, "v", scopes="api docs")
    _row, tkey = apikeys.create_key(users["tr@x.com"].id, "t", scopes="api docs")
    api = TestClient(app)
    payload = {"targets": ["de-DE"], "provider": "mock"}
    r = api.post(f"/api/documents/{doc_id}/translate", json=payload, headers={"X-Loc-Token": vkey})
    assert r.status_code == 403, f"/api viewer docs key: {r.status_code} {r.text[:200]}"
    r = api.post(f"/api/documents/{doc_id}/translate", json=payload, headers={"X-Loc-Token": tkey})
    assert r.status_code == 202, f"/api translator docs key: {r.status_code} {r.text[:200]}"
    # Gate round 2 L1-R: that 202 queued a real job on the worker thread; let it finish so
    # its provider call cannot land inside a later check's before/after call count.
    _wait_jobs_idle()
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    assert translate.can_start_mt(None), "auth off (single operator) must allow MT"
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    print("  service rule: viewer refused, translator allowed; /api viewer 403, translator 202")

    # --- the Document page starts the stream with a POST + CSRF header ---------------
    with open(os.path.join(_ROOT, "app", "templates", "document.html"), encoding="utf-8") as f:
        page = f.read()
    start = page.index("function startTranslate(")
    js = page[start : page.index("</script>", start)]
    assert "translate-stream?locale" not in js, "the page still builds a GET stream URL"
    assert "method: 'POST'" in js and "X-CSRF-Token" in js, "the stream start is not a POST"
    assert "EventSource" not in js and "connectSSE(" not in js
    for said in ("announcer.say('Translation started.')", "announcer.flush(", "announcer.alert("):
        assert said in js, f"announcement lost: {said}"
    print("  Document page: POST + X-CSRF-Token, announcements intact")
    # Gate L1: a refused start shows the server's own sentence (read as text), with a
    # per-status fallback; never one blanket "role" sentence for every 403.
    assert "Your role does not allow starting machine translation." not in js
    assert "querySelector('p.error')" in js and ".textContent" in js
    for status in ("401", "403", "404", "409", "429"):
        assert f"res.status === {status}" in js, f"no fallback sentence for {status}"
    print("  Document page: refusals show the server message (L1)")

    check_route_role_rule(tr, doc_id)
    check_stream_budgets(dpid)
    check_cutoff_charge()
    check_orphan_recovery()

    # Sanity: the key project really is key-based (the fixture is what the probe used).
    with Session(engine) as s:
        kinds = {p.name: p.project_type for p in s.exec(select(Project)).all()}
    assert kinds["SecretKeys"] == ProjectType.KEY_BASED

    print("LIVE TRANSLATE STREAM OK")


if __name__ == "__main__":
    main()
