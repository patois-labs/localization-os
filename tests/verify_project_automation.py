"""Project automation (MVP part A) verification (offline).

A project binds to a connection (pick, create or clone) as its content source and push target
and may run on a schedule. Proves:

- the UTC schedule math (times of day, days of week, interval, validation)
- binding: direct adoption, a private lane when the connection already feeds another project,
  clone, unbind and re-bind reuse, refusals (inbox project, invisible connection, unknown or
  admin-only scope keys, identical scope on a kind with no per-item claims)
- configure: validation, enabling never fires, legacy interval schedule moved on request,
  full pull refused for kinds outside the flow engine
- the scheduler tick: due fire once, consume-slot-first, paused / disabled / kill switch skip,
  budget gate, overlap dedupe, one schedule per connection
- the job path: settle, auto-pause after N failures, partial runs do not count, reset on success
- one connection feeding two projects: the second project's lane cannot write over the first
  project's items (claims), the conflict is logged; full pull ignores the stored hashes
- the per-run spend cap
- /api routes (ring before role, isolation 404, role gates, cost only for managers), the web
  page and its forms, the CLI verbs and the MCP tool
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_projauto_")

import _flow_parity_fixtures as fx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Connection,
    Job,
    JobStatus,
    Project,
    ProjectAutomation,
    ProjectBindingClaim,
    Role,
)
from app.services import (
    apikeys,
    content,
    content_projects,
    jobs,
    project_budgets,
    scheduler,
    security,
    settings,
    spend,
)
from app.services import (
    automation_schedule as sch,
)
from app.services import (
    project_automation as pa,
)
from app.services.connectors import run_ctx
from app.services.connectors import store as cstore
from app.services.connectors.store_common_leaf import is_lane


def utcnow() -> datetime:
    return datetime.utcnow()


def mkproj(name, customer_id=None, locales="de, fr"):
    with Session(engine) as s:
        p = content_projects.project_create(
            s, name, "en-US", locales, customer_id=customer_id, created_by="t@x.co"
        )
        return p.id


def wait_job(jid, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            j = s.get(Job, jid)
            if j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish")


def audit_count(action):
    with Session(engine) as s:
        return len(s.exec(select(AuditLog).where(AuditLog.action == action)).all())


def expect_error(fn, status, needle=""):
    try:
        fn()
    except pa.AutomationError as exc:
        assert exc.status == status, (exc.status, status, str(exc))
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return exc
    raise AssertionError("expected AutomationError")


def zd(name="ZD shared", articles=None, **cfg):
    fz = fx.FakeZendesk(
        articles=articles
        or [
            fx._zd_article("100", edited_at="2026-07-01T00:00:00Z"),
            fx._zd_article("101", edited_at="2026-07-02T00:00:00Z"),
        ]
    )
    conn = fx._zd_conn(name, cfg)
    return conn, fz


# ---------------------------------------------------------------------------------------
def test_schedule_math():
    base = datetime(2026, 10, 13, 10, 0)  # a Tuesday
    nf = sch.next_fire
    assert nf(["09:00", "14:30"], [], 0, base) == datetime(2026, 10, 13, 14, 30)
    assert nf(["09:00", "14:30"], [], 0, datetime(2026, 10, 13, 14, 30)) == datetime(
        2026, 10, 14, 9, 0
    ), "strictly after: the slot that just fired is never returned again"
    assert nf(["09:00"], [0, 2], 0, base) == datetime(2026, 10, 14, 9, 0)  # Mon, Wed -> Wed
    assert nf(["09:00"], [0], 0, base) == datetime(2026, 10, 19, 9, 0)  # next Monday
    assert nf([], [], 60, base) == base + timedelta(minutes=60)
    assert nf([], [], 0, base) is None
    assert nf(["23:59"], [], 0, datetime(2026, 10, 13, 23, 59)) == datetime(2026, 10, 14, 23, 59)
    # month / year rollover and a leap day (UTC has no DST, so there is no skipped hour)
    assert nf(["00:00"], [], 0, datetime(2026, 12, 31, 12, 0)) == datetime(2027, 1, 1, 0, 0)
    assert nf(["06:00"], [], 0, datetime(2028, 2, 28, 7, 0)) == datetime(2028, 2, 29, 6, 0)
    for bad, msg in [
        ({"times": ["9am"], "days": [], "interval_minutes": 0}, "time of day"),
        ({"times": ["25:00"], "days": [], "interval_minutes": 0}, "time of day"),
        ({"times": ["09:00"], "days": [], "interval_minutes": 30}, "not both"),
        ({"times": [], "days": [1], "interval_minutes": 0}, "go with times"),
        ({"times": [], "days": [], "interval_minutes": 5}, "shortest"),
        ({"times": [], "days": [], "interval_minutes": 10081}, "longest"),
        ({"times": ["09:00"], "days": ["noday"], "interval_minutes": 0}, "weekday"),
        ({"times": ["09:00"], "days": [7], "interval_minutes": 0}, "weekday"),
        ({"times": [], "days": [], "interval_minutes": -1}, "zero or more"),
    ]:
        try:
            sch.validate(**bad)
        except sch.ScheduleError as exc:
            assert msg in str(exc).lower(), (bad, str(exc))
        else:
            raise AssertionError(f"accepted {bad}")
    assert sch.validate("9:00, 14:30 09:00", "mon wed", 0) == (["09:00", "14:30"], [0, 2], 0)
    assert sch.validate(["09:00"], list(range(7)), 0)[1] == [], "all seven days = every day"
    assert sch.describe(["09:00", "14:30"], [0, 2], 0) == "9:00 AM, 2:30 PM UTC on Mon, Wed"
    assert (
        sch.describe([], [], 1440) == "every day" and sch.describe([], [], 90) == "every 90 minutes"
    )
    print("  schedule math: times, days, interval, rollover, strictly-after, validation, describe")


# ---------------------------------------------------------------------------------------
def test_bind_direct_lane_clone():
    conn, _fz = zd("ZD bind")
    p1, p2, p3 = mkproj("PA one"), mkproj("PA two"), mkproj("PA three")
    v1 = pa.bind(p1, conn.id, actor="m@x.co", is_admin=True)
    assert v1["bound"] and v1["run_connection_id"] == conn.id and not v1["own_copy"], v1
    assert cstore.get(conn.id).project_id == p1, "a free connection is adopted by the first project"

    v2 = pa.bind(p2, conn.id, actor="m@x.co", is_admin=True)
    assert v2["own_copy"] and v2["run_connection_id"] != conn.id, v2
    lane = cstore.get(v2["run_connection_id"])
    assert is_lane(lane) and lane.config["lane_of"] == conn.id and lane.project_id == p2
    assert lane.kind == "zendesk" and lane.config["subdomain"] == "acme"
    assert not any(k.endswith("_hashes") or k.startswith("sync_") for k in lane.config), lane.config
    assert cstore.dec_secret(lane.secret_enc) == cstore.dec_secret(
        cstore.get(conn.id).secret_enc
    ), "the lane holds the same credential"
    assert lane.id not in [c.id for c in cstore.list_connections()], "a lane is never listed"
    assert v2["shared_with_projects"] == [p1], v2

    # clone = an own copy even when the connection is free
    free, _ = zd("ZD free")
    v3 = pa.bind(p3, free.id, actor="m@x.co", is_admin=True, clone=True)
    assert v3["own_copy"] and cstore.get(free.id).project_id is None, (
        "the picked connection stays free"
    )

    # a project already bound cannot bind again; unbind then re-bind reuses the lane
    expect_error(lambda: pa.bind(p2, conn.id, actor="x"), 409, "already")
    old_lane = v2["run_connection_id"]
    pa.unbind(p2, actor="m@x.co")
    with Session(engine) as s:
        assert pa.get(s, p2) is None
        assert s.get(Connection, old_lane) is not None, "unbind keeps the lane and its state"
    v2b = pa.bind(p2, conn.id, actor="m@x.co", is_admin=True)
    assert v2b["run_connection_id"] == old_lane, "re-binding the same project reuses its lane"
    assert audit_count("automation.bind") >= 1 and audit_count("automation.clone") >= 1
    assert audit_count("automation.unbind") == 1

    # an existing sync project (connection already feeds it) binds directly, no lane
    c4, _ = zd("ZD existing")
    p4 = mkproj("PA existing")
    cstore.patch_config(c4.id, {})
    with Session(engine) as s:
        row = s.get(Connection, c4.id)
        row.project_id = p4
        s.add(row)
        s.commit()
    v4 = pa.bind(p4, c4.id, actor="m@x.co", is_admin=True)
    assert not v4["own_copy"] and v4["run_connection_id"] == c4.id
    print("  bind: direct adoption, private lane, clone, unbind keeps the lane, existing project")
    return conn, p1, p2


def test_bind_refusals_and_scope():
    conn, _ = zd("ZD refusals")
    p = mkproj("PA refuse")
    # inbox projects, missing ids, disabled kinds
    with Session(engine) as s:
        ib = Project(name="Inbox PA", inbox_key="inbox-pa", source_locale="en-US")
        s.add(ib)
        s.commit()
        s.refresh(ib)
        inbox_id = ib.id
    expect_error(lambda: pa.bind(inbox_id, conn.id), 400, "inbox")
    expect_error(lambda: pa.bind(p, 999999), 404)
    expect_error(lambda: pa.bind(999999, conn.id), 404)
    # a customer-scoped connection of another customer is invisible
    # a connection scoped to another customer is invisible, like a missing one
    with Session(engine) as s:
        cust_a = content.customer_create(s, "PA Customer A")
        cust_b = content.customer_create(s, "PA Customer B")
        cust_a_id, cust_b_id = cust_a.id, cust_b.id
    owned = cstore.create(
        "zendesk",
        "ZD customer A",
        {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de"},
        {"email": "a@acme.com", "api_token": "tok"},
        actor="t",
        customer_id=cust_a_id,
    )
    pb = mkproj("PA cust B proj", customer_id=cust_b_id)
    pa_cust = mkproj("PA cust A proj", customer_id=cust_a_id)
    expect_error(lambda: pa.bind(pb, owned.id), 404)
    assert pa.bind(pa_cust, owned.id, is_admin=True)["bound"]
    global_conn, _ = zd("ZD global for customers")
    assert pa.bind(pb, global_conn.id, is_admin=True)["bound"], "a global connection serves anyone"
    pa.unbind(pa_cust)
    pa.unbind(pb)
    # scope: unknown key, run-state key, manager vs admin
    expect_error(lambda: pa.bind(p, conn.id, scope={"nonsense": "1"}), 403, "not a setting")
    expect_error(lambda: pa.bind(p, conn.id, scope={"zendesk_hashes": {}}), 403, "run state")
    expect_error(
        lambda: pa.bind(p, conn.id, scope={"subdomain": "evil"}, is_admin=False), 403, "admin"
    )
    v = pa.bind(p, conn.id, scope={"max_articles": "50"}, is_admin=False, actor="m@x.co")
    assert v["scope"] == {"max_articles": "50"}, v
    # round 2: a manager binding an organization-wide connection works on a private copy; the
    # shared connection is not narrowed (an admin's direct adoption is covered elsewhere)
    assert "max_articles" not in cstore.get(conn.id).config and v["own_copy"], v
    assert cstore.get(v["run_connection_id"]).config["max_articles"] == "50"
    pa.unbind(p)
    # admin may set any declared non-secret setting
    p2 = mkproj("PA refuse2")
    v = pa.bind(p2, conn.id, scope={"label_names": "x"}, is_admin=True, clone=True)
    assert cstore.get(v["run_connection_id"]).config["label_names"] == "x"

    # a kind with no per-item claims refuses an identical scope on a second project
    at = cstore.create(
        "airtable",
        "AT shared",
        {"base_id": "appX", "table": "T", "source_field": "Notes"},
        {"api_key": "pat_x"},
        actor="t",
    )
    a1, a2, a3 = mkproj("PA at1"), mkproj("PA at2"), mkproj("PA at3")
    pa.bind(a1, at.id, is_admin=True)
    expect_error(lambda: pa.bind(a2, at.id, is_admin=True), 409, "only one project")
    expect_error(
        lambda: pa.bind(a3, at.id, is_admin=True, scope={"view": "Viewer A"}), 409, "only one"
    )
    print("  bind refusals: inbox, missing, scope rules, one project per airtable connection")


def test_configure_rules():
    conn, _ = zd("ZD cfg")
    p = mkproj("PA cfg")
    expect_error(lambda: pa.configure(p, enabled=True), 404, "no content source")
    pa.bind(p, conn.id, is_admin=True)
    expect_error(lambda: pa.configure(p, enabled=True), 400, "times of day or an interval")
    now = datetime(2026, 10, 13, 10, 0)
    v = pa.configure(p, times=["09:00", "14:30"], days=["mon", "wed"], enabled=True, now=now)
    assert v["enabled"] and v["times"] == ["09:00", "14:30"] and v["days"] == [0, 2], v
    assert v["next_at"] == "2026-10-14T09:00:00Z", v["next_at"]
    with Session(engine) as s:
        assert pa.get(s, p).last_job_id is None, "enabling never submits a run"
    assert pa.tick(now) == [], "nothing is due yet"
    # switching to an interval clears times and days
    v = pa.configure(p, interval_minutes=60, now=now)
    assert v["times"] == [] and v["days"] == [] and v["interval_minutes"] == 60, v
    expect_error(lambda: pa.configure(p, interval_minutes=5), 400, "shortest")
    expect_error(lambda: pa.configure(p, times=["09:00"], interval_minutes=60), 400, "not both")
    expect_error(lambda: pa.configure(p, mode="weekly"), 400, "delta")
    expect_error(lambda: pa.configure(p, auto_pause_after=0), 400, "between")
    expect_error(lambda: pa.configure(p, run_cap_usd="abc"), 400, "number")
    expect_error(lambda: pa.configure(p, run_cap_usd=-1), 400, "more than 0")
    v = pa.configure(p, run_cap_usd="2.5", auto_pause_after=2, mode="full")
    assert v["run_cap_usd"] == 2.5 and v["auto_pause_after"] == 2 and v["mode"] == "full"
    v = pa.configure(p, run_cap_usd=None)
    assert v["run_cap_usd"] is None
    assert audit_count("automation.set") >= 3

    # one schedule per connection: a legacy interval schedule must be moved explicitly
    cstore.patch_config(conn.id, {"sync_every_minutes": 60, "sync_next_at": "2026-10-13T11:00:00"})
    pa.configure(p, enabled=False)
    expect_error(lambda: pa.configure(p, enabled=True), 409, "own interval schedule")
    pa.configure(p, enabled=True, move_schedule=True)
    assert "sync_every_minutes" not in cstore.get(conn.id).config
    cstore.patch_config(conn.id, {"sync_every_minutes": 60, "sync_next_at": "2000-01-01T00:00:00"})
    assert pa.owns_schedule(conn.id)
    assert scheduler.tick() == [] or all(
        (jobs.get(j).payload or {}).get("automation_id") is not None for j in scheduler.tick()
    ), "the connection's own schedule must not fire when a project automation owns it"
    cstore.patch_config(conn.id, remove=("sync_every_minutes", "sync_next_at"))

    # a full pull is refused for a kind outside the flow engine
    at = cstore.create(
        "airtable",
        "AT full",
        {"base_id": "appX", "table": "T", "source_field": "Notes"},
        {"api_key": "pat_x"},
        actor="t",
    )
    pa2 = mkproj("PA cfg at")
    pa.bind(pa2, at.id, is_admin=True)
    expect_error(lambda: pa.configure(pa2, mode="full"), 400, "not available")
    print("  configure: validation, no fire on enable, legacy schedule moved, no full for airtable")


# ---------------------------------------------------------------------------------------
def _stub_translate(ok=True, partial=False):
    calls = []

    def fn(cid, actor="", transport=None):
        calls.append(cid)
        if not ok:
            return False, "boom", {}
        if partial:
            return (
                True,
                "partial",
                {"failures": [{"article_id": "1", "locale": "de", "error": "x"}]},
            )
        return True, "ok: 3 items", {"created": 3, "updated": 0}

    fn.calls = calls
    return fn


def _quiesce_other_automations():
    """Earlier tests left enabled automations whose slots are relative to the REAL clock; the
    tick tests below move a fake clock, so switch every other schedule off first."""
    with Session(engine) as s:
        for a in s.exec(select(ProjectAutomation)).all():
            a.enabled = False
            s.add(a)
        s.commit()


def test_tick_and_job_settle():
    real = cstore.translate_now
    stub = _stub_translate()
    set_pieces("app.services.connectors.store", "translate_now", stub)
    _quiesce_other_automations()
    try:
        conn, _ = zd("ZD tick")
        p = mkproj("PA tick")
        pa.bind(p, conn.id, is_admin=True)
        t0 = datetime(2026, 10, 13, 8, 0)
        pa.configure(p, times=["09:00"], enabled=True, now=t0)
        assert pa.tick(datetime(2026, 10, 13, 8, 59)) == []
        fired = pa.tick(datetime(2026, 10, 13, 9, 0, 5))
        assert len(fired) == 1, fired
        j = wait_job(fired[0])
        assert j.status == JobStatus.COMPLETED and j.created_by == "scheduler"
        pl = j.payload
        assert (
            pl["automation_id"] and pl["trigger"] == "schedule" and pl["connection_id"] == conn.id
        )
        assert j.result["automation_id"] == pl["automation_id"] and j.result["created"] == 3
        with Session(engine) as s:
            a = pa.get(s, p)
            assert (
                a.last_status == "ok"
                and a.consecutive_failures == 0
                and a.next_at == datetime(2026, 10, 14, 9, 0)
            ), (a.last_status, a.next_at)
        assert pa.tick(datetime(2026, 10, 13, 9, 0, 40)) == [], (
            "the slot was consumed: no double fire"
        )
        assert audit_count("automation.run") >= 1

        # consume-slot-first: a due slot moves forward even when the run is skipped
        with Session(engine) as s:
            stuck = Job(
                kind="connector_translate",
                status=JobStatus.RUNNING,
                started_at=utcnow(),
                payload={"connection_id": conn.id, "actor": "x"},
            )
            s.add(stuck)
            s.commit()
            s.refresh(stuck)
            stuck_id = stuck.id
        assert pa.tick(datetime(2026, 10, 14, 9, 1)) == [], "a run in flight defers the slot"
        with Session(engine) as s:
            a = pa.get(s, p)
            assert a.next_at == datetime(2026, 10, 15, 9, 0) and a.last_status == "skipped", (
                a.next_at,
                a.last_status,
            )
            s.get(Job, stuck_id).status = JobStatus.COMPLETED
            s.commit()

        # paused / disabled / kill switch
        pa.pause(p, reason="holiday", actor="m@x.co")
        assert pa.tick(datetime(2026, 10, 15, 9, 1)) == [], "paused automations do not fire"
        v = pa.resume(p, actor="m@x.co", now=datetime(2026, 10, 15, 9, 2))
        assert not v["paused"] and v["next_at"] == "2026-10-16T09:00:00Z", v
        settings.set_many({"sync_scheduler_enabled": "0"})
        try:
            assert scheduler.tick(now=datetime(2026, 10, 16, 9, 1)) == [], "kill switch"
        finally:
            settings.set_many({"sync_scheduler_enabled": "1"})
        # through the real scheduler tick (below the kill switch) with the time moved forward
        with Session(engine) as s:
            a = pa.get(s, p)
            a.next_at = utcnow() - timedelta(seconds=5)
            s.add(a)
            s.commit()
        fired = scheduler.tick()
        autos = [jid for jid in fired if (jobs.get(jid).payload or {}).get("automation_id")]
        assert len(autos) == 1, fired
        wait_job(autos[0])

        # archived project and a disabled kind are skipped with a reason, not failures
        with Session(engine) as s:
            a = pa.get(s, p)
            a.next_at = utcnow() - timedelta(seconds=5)
            s.add(a)
            pr = s.get(Project, p)
            pr.archived_at = utcnow()
            s.add(pr)
            s.commit()
        assert pa.tick() == []
        with Session(engine) as s:
            a = pa.get(s, p)
            assert a.last_status == "skipped" and "archived" in a.last_message, a.last_message
            assert a.consecutive_failures == 0
            pr = s.get(Project, p)
            pr.archived_at = None
            s.add(pr)
            s.commit()

        # the project budget gates the run before any job exists
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, p, 1.0)
            s.add(
                Job(
                    kind="connector_translate",
                    status=JobStatus.COMPLETED,
                    payload={"connection_id": conn.id},
                    result={},
                    cost_usd=5.0,
                    created_by="x",
                    finished_at=utcnow(),
                )
            )
            s.commit()
        expect_error(lambda: pa.run_now(p, actor="m@x.co"), 409, "budget")
        with Session(engine) as s:
            b = project_budgets.get_project_budget(s, p)
            project_budgets.delete_project_budget(s, b.id)
        jid, created = pa.run_now(p, actor="m@x.co")
        assert created
        wait_job(jid)
        jid2, _created2 = pa.run_now(p, actor="m@x.co")  # finished: a new run
        wait_job(jid2)

        # job settle: failures count, 3 in a row auto-pause, success resets, partial is fine
        pa.configure(p, enabled=False)
        pa.resume(p)
        set_pieces("app.services.connectors.store", "translate_now", _stub_translate(ok=False))
        for n in (1, 2):
            jid, _ = pa.run_now(p, actor="m@x.co")
            assert wait_job(jid).status == JobStatus.FAILED
            with Session(engine) as s:
                a = pa.get(s, p)
                assert a.consecutive_failures == n and a.paused_at is None, (
                    n,
                    a.consecutive_failures,
                )
        jid, _ = pa.run_now(p, actor="m@x.co")
        wait_job(jid)
        with Session(engine) as s:
            a = pa.get(s, p)
            assert a.paused_at is not None and "3 failed runs" in a.paused_reason, a.paused_reason
            assert a.last_status == "failed", (a.last_status, a.last_message)
        assert audit_count("automation.pause") >= 1
        pa.resume(p)
        set_pieces("app.services.connectors.store", "translate_now", _stub_translate(partial=True))
        jid, _ = pa.run_now(p, actor="m@x.co")
        j = wait_job(jid)
        assert j.status == JobStatus.COMPLETED
        with Session(engine) as s:
            a = pa.get(s, p)
            assert a.last_status == "ok" and a.consecutive_failures == 0
        set_pieces("app.services.connectors.store", "translate_now", _stub_translate(ok=False))
        jid, _ = pa.run_now(p, actor="m@x.co")
        wait_job(jid)
        set_pieces("app.services.connectors.store", "translate_now", stub)
        jid, _ = pa.run_now(p, actor="m@x.co")
        wait_job(jid)
        with Session(engine) as s:
            assert pa.get(s, p).consecutive_failures == 0, "a success resets the counter"

        # a reconnect-needed connection pauses at once
        from app.models import ConnStatus

        with Session(engine) as s:
            c = s.get(Connection, conn.id)
            c.status = ConnStatus.REAUTH
            s.add(c)
            s.commit()
        expect_error(lambda: pa.run_now(p, actor="m@x.co"), 409, "reconnected")
        with Session(engine) as s:
            c = s.get(Connection, conn.id)
            c.status = ConnStatus.CONNECTED
            s.add(c)
            s.commit()

        # history: the newest runs, tagged, cost only when allowed
        with Session(engine) as s:
            h = pa.history(s, p, limit=50, can_see_cost=False)
            assert (
                h
                and all("cost_usd" not in r for r in h)
                and {"trigger", "mode", "status"} <= set(h[0])
            )
            h2 = pa.history(s, p, limit=3, can_see_cost=True)
            assert len(h2) == 3 and all("cost_usd" in r for r in h2)
        # an automation removed while a job waits: the job finishes cleanly, runs nothing
        calls_before = len(stub.calls)
        with Session(engine) as s:
            ghost = Job(
                kind="connector_translate",
                status=JobStatus.QUEUED,
                payload={"connection_id": conn.id, "automation_id": 424242, "actor": "x"},
            )
            s.add(ghost)
            s.commit()
            s.refresh(ghost)
        out = pa.execute_job(ghost.payload, lambda *a, **k: None, lambda p, pr: {"ok": True})
        assert out.get("skipped_automation_gone") and len(stub.calls) == calls_before
        print(
            "  tick+job: due once, slot consumed, deferred, paused, kill switch, archived, budget"
        )
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


# ---------------------------------------------------------------------------------------
def test_flow_full_claims():
    """Two projects on ONE Zendesk connection, over the real flow engine."""
    real = cstore.translate_now
    conn, fz = zd("ZD claims")
    holder = {"fz": fz}

    def run_flow(cid, actor="", transport=None):
        return cstore.zendesk_translate(cid, actor=actor, transport=holder["fz"].transport())

    set_pieces("app.services.connectors.store", "translate_now", run_flow)
    try:
        p1, p2 = mkproj("PA claim one"), mkproj("PA claim two")
        pa.bind(p1, conn.id, is_admin=True)
        v2 = pa.bind(p2, conn.id, is_admin=True)
        lane = v2["run_connection_id"]
        assert lane != conn.id

        j1 = wait_job(pa.run_now(p1, actor="m@x.co")[0])
        assert j1.status == JobStatus.COMPLETED, (j1.status, j1.error, j1.result)
        assert j1.result.get("created", 0) + j1.result.get("updated", 0) > 0, j1.result
        with Session(engine) as s:
            owned = s.exec(
                select(ProjectBindingClaim).where(ProjectBindingClaim.connection_id == conn.id)
            ).all()
            assert {c.external_id for c in owned} == {"100", "101"} and {
                c.project_id for c in owned
            } == {p1}
        writes_after_first = len(fz.creates) + len(fz.updates)

        # delta again: nothing changed, nothing written
        j = wait_job(pa.run_now(p1, actor="m@x.co")[0])
        assert len(fz.creates) + len(fz.updates) == writes_after_first, (
            "delta skips unchanged pairs"
        )
        assert j.result.get("skipped", 0) > 0, j.result

        # the SECOND project's lane reaches the same articles: all owned by project 1
        before = len(fz.creates) + len(fz.updates)
        j2 = wait_job(pa.run_now(p2, actor="m@x.co")[0])
        assert j2.status == JobStatus.COMPLETED, (j2.status, j2.error)
        assert j2.result["claim_conflicts"] == 4 and sorted(j2.result["claim_conflict_ids"]) == [
            "100:de",
            "100:fr",
            "101:de",
            "101:fr",
        ], j2.result
        # H3: the blocked run is reported as blocked (not "ok") and names the other owner
        assert j2.result["blocked_by_claims"] is True, j2.result
        with Session(engine) as s:
            a2 = pa.get(s, p2)
            assert a2.last_status == "blocked", a2.last_status
            v = pa.view(s, a2)
            assert v["blocked"] and v["owned_elsewhere"][0]["project_id"] == p1, v
            assert v["owned_elsewhere"][0]["pairs"] == 4 and any(
                "blocked" in w for w in v["warnings"]
            )
            h = pa.history(s, p2, limit=1)[0]
            assert h["blocked"] is True, h
        assert len(fz.creates) + len(fz.updates) == before, (
            "project 2 must not write over project 1"
        )
        assert audit_count("automation.conflict") >= 1
        with Session(engine) as s:
            owned = s.exec(
                select(ProjectBindingClaim).where(ProjectBindingClaim.connection_id == conn.id)
            ).all()
            assert {c.project_id for c in owned} == {p1}, (
                "ownership never moves to the later project"
            )

        # FULL pull ignores the stored hashes: every pair is written again
        pa.configure(p1, mode="full")
        before = len(fz.creates) + len(fz.updates)
        j = wait_job(pa.run_now(p1, actor="m@x.co")[0])
        assert j.status == JobStatus.COMPLETED and j.result["mode"] == "full"
        assert len(fz.creates) + len(fz.updates) > before, "full re-writes unchanged pairs"
        # a one-off full from delta settings
        pa.configure(p1, mode="delta")
        before = len(fz.creates) + len(fz.updates)
        wait_job(pa.run_now(p1, actor="m@x.co", mode="full")[0])
        assert len(fz.creates) + len(fz.updates) > before

        # unbinding project 1 releases its claims; project 2 now owns and writes the items
        pa.unbind(p1, actor="m@x.co")
        with Session(engine) as s:
            assert not s.exec(
                select(ProjectBindingClaim).where(ProjectBindingClaim.connection_id == conn.id)
            ).all()
        j2 = wait_job(pa.run_now(p2, actor="m@x.co")[0])
        assert (
            j2.result.get("claim_conflicts", 0) == 0
            and j2.result.get("created", 0) + j2.result.get("updated", 0) > 0
        ), j2.result
        # each project keeps its own state: the lane's hashes are not the picked connection's
        assert any(k.endswith("_hashes") for k in cstore.get(lane).config)
        print(
            "  flow: first project owns items, second refused, full ignores hashes, unbind releases"
        )
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)

    # the switches are inert outside an automation run
    assert run_ctx.full_pull() is False and run_ctx.claim() is None
    with run_ctx.scope(full=True, claim_obj=object()):
        assert run_ctx.full_pull() is True and run_ctx.claim() is not None
    assert run_ctx.full_pull() is False and run_ctx.claim() is None


def test_delete_connection_cleans_up():
    conn, _ = zd("ZD delete")
    p1, p2 = mkproj("PA del one"), mkproj("PA del two")
    pa.bind(p1, conn.id, is_admin=True)
    v2 = pa.bind(p2, conn.id, is_admin=True)
    lane = v2["run_connection_id"]
    pa.ClaimTaker(conn.id, p1).take("x-1")
    cstore.delete(conn.id)
    with Session(engine) as s:
        assert pa.get(s, p1) is None and pa.get(s, p2) is None, "bindings go with the connection"
        assert s.get(Connection, conn.id) is None and s.get(Connection, lane) is None
        assert not s.exec(
            select(ProjectBindingClaim).where(ProjectBindingClaim.connection_id == conn.id)
        ).all()
    # deleting only a lane unbinds just its project
    c2, _ = zd("ZD delete lane")
    q1, q2 = mkproj("PA del lane one"), mkproj("PA del lane two")
    pa.bind(q1, c2.id, is_admin=True)
    lane2 = pa.bind(q2, c2.id, is_admin=True)["run_connection_id"]
    cstore.delete(lane2)
    with Session(engine) as s:
        assert pa.get(s, q2) is None and pa.get(s, q1) is not None
        assert s.get(Connection, c2.id) is not None
    print("  delete: bindings, claims and lanes are cleaned up with the connection")


def test_lane_credential_sync():
    conn, _ = zd("ZD cred")
    pa_a, pa_b = mkproj("PA cred a"), mkproj("PA cred b")
    pa.bind(pa_a, conn.id, is_admin=True)
    v = pa.bind(pa_b, conn.id, is_admin=True)
    lane = v["run_connection_id"]
    # the picked connection's credential is rotated: the lane follows before its next run
    cstore.rotate_secret  # noqa: B018  (exists)
    with Session(engine) as s:
        c = s.get(Connection, conn.id)
        c.secret_enc = cstore.enc_secret({"email": "a@acme.com", "api_token": "rotated"})
        s.add(c)
        s.commit()
    pa._sync_credential_to_lane(conn.id, lane)
    assert cstore.dec_secret(cstore.get(lane).secret_enc)["api_token"] == "rotated"
    # a token refreshed on the lane flows back, but only onto the credential it started from
    with Session(engine) as s:
        ln = s.get(Connection, lane)
        ln.secret_enc = cstore.enc_secret({"email": "a@acme.com", "api_token": "refreshed"})
        s.add(ln)
        s.commit()
    pa._sync_credential_from_lane(conn.id, lane)
    assert cstore.dec_secret(cstore.get(conn.id).secret_enc)["api_token"] == "refreshed"
    with Session(engine) as s:  # a rotation made meanwhile on the picked connection wins
        c = s.get(Connection, conn.id)
        c.secret_enc = cstore.enc_secret({"email": "a@acme.com", "api_token": "person-rotated"})
        ln = s.get(Connection, lane)
        ln.secret_enc = cstore.enc_secret({"email": "a@acme.com", "api_token": "stale-refresh"})
        s.add(c)
        s.add(ln)
        s.commit()
    pa._sync_credential_from_lane(conn.id, lane)
    assert cstore.dec_secret(cstore.get(conn.id).secret_enc)["api_token"] == "person-rotated"
    print(
        "  lane credential: follows the original, hands a refresh back, never overwrites a rotation"
    )


def test_run_cap():
    assert spend._run_cap_hit()[0] is False
    with spend.run_cap(1.0) as sink:
        assert spend.would_block()[0] is False
        sink.add(0.4)
        assert spend.would_block()[0] is False
        sink.add(0.7)
        blocked, spent, cap = spend.would_block()
        assert blocked and cap == 1.0 and round(spent, 2) == 1.1
        assert "Run spend cap reached" in spend.cap_message(spent, cap)
        assert audit_count("spend.cap_tripped") == 0, "a run cap is not the install-wide cap trip"
        spend.note_trip()
        assert audit_count("spend.cap_tripped") == 0
    assert spend.would_block()[0] is False, "the cap ends with the run"
    with spend.run_cap(0) as sink:  # zero = no cap, still collects
        sink.add(50)
        assert spend.would_block()[0] is False
    print("  run cap: blocks at the cap, wording, not an install-wide trip, ends with the run")


# ---------------------------------------------------------------------------------------
def test_api_surface():
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1"})
    try:
        admin = security.create_user("pa-admin@x.co", "PA Admin", Role.ADMIN, "pw-fixture-5521")
        mgr = security.create_user("pa-mgr@x.co", "PA Mgr", Role.MANAGER, "pw-fixture-5521")
        viewer = security.create_user("pa-view@x.co", "PA View", Role.VIEWER, "pw-fixture-5521")
        _k, ak = apikeys.create_key(admin.id, "pa admin", scopes="api docs")
        _k, mk = apikeys.create_key(mgr.id, "pa mgr", scopes="api docs")
        _k, vk = apikeys.create_key(viewer.id, "pa view", scopes="api docs")
        A, M, V = ({"X-Loc-Token": k} for k in (ak, mk, vk))
        c = TestClient(app)
        conn, _fz = zd("ZD api")
        p = mkproj("PA api")

        # project scoping ON (the default): a viewer who is not a member sees nothing, and a
        # write answers 404 (ring) before it can answer 403 (role)
        assert settings.get("project_scoping") == "1"
        assert c.get(f"/api/projects/{p}/automation", headers=V).status_code == 404
        assert c.put(f"/api/projects/{p}/automation", headers=V, json={}).status_code == 404
        assert c.post(f"/api/projects/{p}/runs", headers=V, json={}).status_code == 404
        settings.set_many({"project_scoping": "0"})

        r = c.get(f"/api/projects/{p}/automation", headers=V)
        assert r.status_code == 200 and r.json()["bound"] is False, r.text
        assert c.get("/api/projects/999999/automation", headers=V).status_code == 404
        # ring before role: a viewer writing to a missing project is 404, not 403
        assert c.put("/api/projects/999999/automation", headers=V, json={}).status_code == 404
        assert c.put(f"/api/projects/{p}/automation", headers=V, json={}).status_code == 403
        assert (
            c.post(
                f"/api/projects/{p}/source", headers=V, json={"connection_id": conn.id}
            ).status_code
            == 403
        )
        assert c.post(f"/api/projects/{p}/runs", headers=V, json={}).status_code == 403

        r = c.post(f"/api/projects/{p}/source", headers=M, json={"connection_id": 999999})
        assert r.status_code == 404, r.text
        r = c.post(f"/api/projects/{p}/source", headers=M, json={})
        assert r.status_code == 400
        r = c.post(
            f"/api/projects/{p}/source",
            headers=M,
            json={"connection_id": conn.id, "scope": {"subdomain": "x"}},
        )
        assert r.status_code == 403, r.text  # a manager cannot retarget
        r = c.post(
            f"/api/projects/{p}/source",
            headers=M,
            json={"connection_id": conn.id, "scope": {"max_articles": "20"}},
        )
        assert r.status_code == 201 and r.json()["bound"], r.text
        r = c.post(f"/api/projects/{p}/source", headers=M, json={"connection_id": conn.id})
        assert r.status_code == 409, r.text

        r = c.put(
            f"/api/projects/{p}/automation",
            headers=M,
            json={"times": ["09:00"], "days": ["Mon"], "enabled": True, "run_cap_usd": 3},
        )
        assert r.status_code == 200 and r.json()["enabled"] and r.json()["run_cap_usd"] == 3.0, (
            r.text
        )
        r = c.put(f"/api/projects/{p}/automation", headers=M, json={"times": ["99:00"]})
        assert r.status_code == 400
        r = c.get(f"/api/projects/{p}/automation", headers=V)
        assert r.status_code == 200 and r.json()["run_cap_usd"] is None, "the cap is manager data"
        r = c.get(f"/api/projects/{p}/automation", headers=M)
        assert r.json()["run_cap_usd"] == 3.0 and r.json()["scheduler_enabled"] is True

        real = cstore.translate_now
        set_pieces("app.services.connectors.store", "translate_now", _stub_translate())
        try:
            r = c.post(f"/api/projects/{p}/runs", headers=M, json={})
            assert r.status_code == 202 and r.json()["job_id"], r.text
            jid = r.json()["job_id"]
            wait_job(jid)
            r = c.get(f"/api/projects/{p}/runs", headers=M)
            runs = r.json()["runs"]
            assert runs and runs[0]["job_id"] == jid and "cost_usd" in runs[0], r.text
            r = c.get(f"/api/projects/{p}/runs", headers=V)
            assert "cost_usd" not in r.json()["runs"][0], "cost is manager data (ADR 0042)"
        finally:
            set_pieces("app.services.connectors.store", "translate_now", real)

        assert c.post(
            f"/api/projects/{p}/automation/pause", headers=M, json={"reason": "freeze"}
        ).json()["paused"]
        assert (
            c.post(f"/api/projects/{p}/runs", headers=M, json={"mode": "bogus"}).status_code == 400
        )
        assert c.post(f"/api/projects/{p}/automation/resume", headers=M).json()["paused"] is False
        assert c.post(f"/api/projects/{p}/source/unbind", headers=M).status_code == 200
        assert c.post(f"/api/projects/{p}/source/unbind", headers=M).status_code == 404

        # H1: creating a connection while binding follows the SAME rules as POST /api/connectors
        ling = security.create_user("pa-ling@x.co", "PA Ling", Role.LINGUIST, "pw-fixture-5521")
        _k, lk = apikeys.create_key(ling.id, "pa ling", scopes="api docs")
        L = {"X-Loc-Token": lk}
        at_nc = {
            "kind": "airtable",
            "name": "AT via project",
            "config": {"base_id": "appY", "table": "T", "source_field": "Notes"},
        }

        def bind_new(headers, nc):
            return c.post(f"/api/projects/{p}/source", headers=headers, json={"new_connection": nc})

        assert bind_new(M, at_nc).status_code == 403, "a manager may not create a connection"
        assert bind_new(V, at_nc).status_code == 403
        r = bind_new(A, {**at_nc, "secret": {"api_key": "pat_y"}})
        assert r.status_code == 400 and "secret" in r.text, "a credential is refused over /api"
        r = bind_new(A, {**at_nc, "config": {**at_nc["config"], "api_token": "tok"}})
        assert r.status_code == 400, "nor nested under config"
        assert bind_new(L, at_nc).status_code == 403, "a linguist is not a manager"
        # the checks themselves (a linguist passes can_tune_ai; only an admin passes these)
        from app.web import project_automation as web_pa

        lident = {"kind": "user", "user": ling, "actor": ling.email}
        nc_sn = {
            "kind": "servicenow",
            "name": "SN live",
            "config": {"base_url": "https://sn.example.com", "update_published": "1"},
        }
        assert web_pa._new_connection_block(lident, nc_sn).status_code == 403
        nc_gd = {
            "kind": "gdocs",
            "name": "GD imp",
            "config": {"source_folder": "f1", "impersonate_user": "boss@x.co"},
        }
        assert web_pa._new_connection_block(lident, nc_gd).status_code == 403
        nc_jira = {"kind": "jira", "name": "J", "config": {"sync_every_minutes": 15}}
        assert web_pa._new_connection_block(lident, nc_jira).status_code == 403
        assert (
            web_pa._new_connection_block(lident, {"kind": "nonsense", "name": "x"}).status_code
            == 400
        )
        assert web_pa._new_connection_block({"kind": "user", "user": mgr}, at_nc).status_code == 403
        assert web_pa._new_connection_block(lident, at_nc) is None
        assert bind_new(A, {"kind": "nonsense", "name": "x"}).status_code == 400
        assert bind_new(A, {"kind": "airtable"}).status_code == 400, "a name is required"
        r = bind_new(A, at_nc)
        assert r.status_code == 201 and r.json()["connection_kind"] == "airtable", r.text
        assert c.post(f"/api/projects/{p}/source/unbind", headers=A).status_code == 200
        r = c.put(f"/api/projects/{p}/automation", headers=A, json={"mode": "full"})
        assert r.status_code == 404, "no binding yet"
        print(
            "  api: ring before role, 404 isolation, manager gates, scope refusal, cost, async run"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})


def test_web_page():
    c = TestClient(app)
    conn, _ = zd("ZD web")
    p = mkproj("PA web")
    r = c.get(f"/projects/{p}/automation")
    assert r.status_code == 200 and 'id="automation-bind-form"' in r.text, r.text[:400]
    assert True
    assert c.get("/projects/999999/automation").status_code == 404
    link = c.get(f"/projects/{p}")
    assert f'href="/projects/{p}/automation"' in link.text, "the project page links to Automation"

    def post(path, data=None):
        return c.post(f"/projects/{p}/automation/{path}", data=data or {}, follow_redirects=False)

    r = post("bind", {"connection_id": str(conn.id), "scope": "max_articles=40"})
    assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers
    page = c.get(f"/projects/{p}/automation").text
    assert 'id="automation-schedule-form"' in page and "max_articles" in page and "ZD web" in page
    r = post(
        "schedule",
        {
            "kind": "times",
            "times": "09:00, 14:30",
            "days": ["0", "2"],
            "mode": "delta",
            "enabled": "1",
            "auto_pause_after": "3",
            "run_cap_usd": "",
        },
    )
    assert "ok=1" in r.headers["location"], r.headers["location"]
    page = c.get(f"/projects/{p}/automation").text
    assert "9:00 AM, 2:30 PM UTC on Mon, Wed" in page and 'class="utc-time"' in page
    assert (
        "/static/project_automation.js" in page
        and "<script nonce" not in page.split("automation-runs")[-1][:50]
    )
    r = post("schedule", {"kind": "times", "times": "9am", "mode": "delta"})
    assert "ok=0" in r.headers["location"]
    r = post("pause", {"reason": "web"})
    assert "ok=1" in r.headers["location"] and "paused" in c.get(f"/projects/{p}/automation").text
    r = post("resume")
    assert "ok=1" in r.headers["location"]
    real = cstore.translate_now
    set_pieces("app.services.connectors.store", "translate_now", _stub_translate())
    try:
        r = post("run", {"mode": ""})
        assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers["location"]
        with Session(engine) as s:
            a = pa.get(s, p)
            wait_job(a.last_job_id)
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)
    assert "No automation runs yet" not in c.get(f"/projects/{p}/automation").text
    r = post("unbind")
    assert "ok=1" in r.headers["location"]
    print(
        "  web: page, bind, schedule, bad input, pause, resume, run now, unbind, project page link"
    )


def test_cli():
    from app.cli.__main__ import build_parser, main

    leaves = set()

    def walk(parser, prefix):
        for act in parser._actions:
            if hasattr(act, "choices") and isinstance(act.choices, dict) and act.dest != "help":
                for name, sub in act.choices.items():
                    walk(sub, [*prefix, name])
        if prefix:
            leaves.add(" ".join(prefix))

    walk(build_parser(), [])
    for want in (
        "project bind",
        "project unbind",
        "project automation show",
        "project automation set",
        "project automation pause",
        "project automation resume",
        "project automation handover",
        "project run",
        "project runs",
    ):
        assert want in leaves, (want, sorted(x for x in leaves if x.startswith("project")))

    import io
    from contextlib import redirect_stdout

    def cli(*argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--json", *argv])
        out = buf.getvalue().strip()
        return code, (json.loads(out.splitlines()[-1]) if out else {})

    conn, _ = zd("ZD cli")
    p = mkproj("PA cli")
    code, out = cli("project", "automation", "show", str(p))
    assert code == 0 and out["bound"] is False, out
    code, out = cli("project", "bind", str(p), str(conn.id))
    assert code == 0 and out["bound"] is True, out
    code, out = cli(
        "project",
        "automation",
        "set",
        str(p),
        "--times",
        "09:00,14:30",
        "--days",
        "mon,wed",
        "--enable",
    )
    assert code == 0 and out["enabled"] and out["times"] == ["09:00", "14:30"], out
    code, out = cli("project", "automation", "set", str(p), "--interval", "5")
    assert code == 1, (code, out)
    code, out = cli("project", "automation", "pause", str(p), "--reason", "cli")
    assert code == 0 and out["paused"], out
    code, out = cli("project", "automation", "resume", str(p))
    assert code == 0 and not out["paused"], out
    real = cstore.translate_now
    set_pieces("app.services.connectors.store", "translate_now", _stub_translate())
    try:
        code, out = cli("project", "run", str(p))
        assert code == 0 and out["job_id"], out
        wait_job(out["job_id"])
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)
    code, out = cli("project", "runs", str(p))
    assert code == 0 and out["runs"], out
    code, out = cli("project", "unbind", str(p))
    assert code == 0 and out["bound"] is False, out
    print("  cli: bind, show, set, bad interval exits 1, pause, resume, run, runs, unbind")


def test_page_local_time_labels():
    """The page script turns UTC times into the reader's local time (release smoke 10-13):
    the next run must not stay labelled "(UTC)" once it shows a local time, and a time of day
    converts on the date of the next run, so daylight saving time is right (03:00 UTC is
    9:00 PM in Denver in October, 8:00 PM in December). Runs the real script in Node."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "test_page_local_time_labels needs Node (it runs the real page script)"
    js_path = os.path.join(_ROOT, "app", "static", "project_automation.js")
    with open(js_path, encoding="utf-8") as fh:
        script = fh.read()
    harness = (
        """
const els = [];
function el(attrs, text, sibling) {
  const e = { attrs, textContent: text, title: '', nextElementSibling: sibling || null,
              getAttribute: (k) => (k in attrs ? attrs[k] : null) };
  els.push(e); return e;
}
const zone = { textContent: '(UTC)', classList: { contains: (c) => c === 'local-dt-zone' } };
const next = el({ datetime: '2026-10-14T03:00:00Z' }, '2026-10-14T03:00:00Z', zone);
const oct = el({ 'data-utc': '03:00', 'data-ref': '2026-10-14T03:00:00Z' }, '03:00 UTC');
const dec = el({ 'data-utc': '03:00', 'data-ref': '2026-12-14T03:00:00Z' }, '03:00 UTC');
const none = el({ 'data-utc': '03:00', 'data-ref': '' }, '03:00 UTC');
const document = { querySelectorAll: (sel) => sel === 'time.local-dt' ? [next] : [oct, dec, none] };
"""
        + script
        + """
console.log(JSON.stringify({ next: next.textContent, zone: zone.textContent, oct: oct.textContent,
                             dec: dec.textContent, none: none.textContent }));
"""
    )
    env = dict(os.environ, TZ="America/Denver")
    res = subprocess.run(
        [node, "-"],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
        check=False,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    sp = "[\\s\u202f]"  # ICU may put a narrow no-break space before AM/PM
    assert re.search("9:00" + sp + "PM", out["next"]), out
    assert "(UTC)" not in out["zone"], out
    assert re.search("your time; 3:00" + sp + "AM UTC", out["zone"]), out
    assert re.search(r"\(9:00" + sp + r"PM your time\)", out["oct"]), out
    assert re.search(r"\(8:00" + sp + r"PM your time\)", out["dec"]), out
    assert "your time" in out["none"], out
    print("  page: local next-run relabelled, times of day convert on the run date (DST) OK")


def test_mcp_tool_registered():
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry

    names = {t.name for t in registry.TOOLS}
    assert "project_automation" in names, "MCP tool missing"
    tool = next(t for t in registry.TOOLS if t.name == "project_automation")
    assert tool.family == "projects" or tool.family, tool.family
    routes = {tuple(r) for r in tool.routes}
    assert ("GET", "/api/projects/{id}/automation") in routes or any(
        "automation" in r[1] for r in routes
    )
    print("  mcp: project_automation registered with its routes")


# ---------------------------------------------------------------------------------------
# Fix round 1 (code review): claims per (item, locale), release, lane health and posture,
# the budget as a run bound, the cap is not a failure, full cursor, kill switch, atomic tick,
# visibility, MCP limits, notifications
# ---------------------------------------------------------------------------------------
def _flow_patch(fz_holder):
    def run_flow(cid, actor="", transport=None):
        return cstore.zendesk_translate(cid, actor=actor, transport=fz_holder["fz"].transport())

    set_pieces("app.services.connectors.store", "translate_now", run_flow)


def _writes(fz):
    return len(fz.creates) + len(fz.updates)


def _claims(connection_id):
    with Session(engine) as s:
        return s.exec(
            select(ProjectBindingClaim).where(ProjectBindingClaim.connection_id == connection_id)
        ).all()


def test_claims_per_locale_and_release():
    """H3 (per item AND locale) and H4 (released on archive, taken over from a dead owner,
    swept when an item leaves the scope)."""
    from app.services import content_project_rename_delete_sp as cpr

    real = cstore.translate_now
    conn, fz = zd("ZD locales")
    holder = {"fz": fz}
    _flow_patch(holder)
    try:
        # connected projects (decision 4): a bound project's own language list drives its runs,
        # so "fr only" / "de only" are now project languages, not a connection scope
        p_fr, p_de = mkproj("PA fr", locales="fr"), mkproj("PA de", locales="de")
        pa.bind(p_fr, conn.id, is_admin=True, scope={"target_locales": "fr"})
        v = pa.bind(p_de, conn.id, is_admin=True, scope={"target_locales": "de"})
        assert v["own_copy"]
        j1 = wait_job(pa.run_now(p_fr, actor="m@x.co")[0])
        j2 = wait_job(pa.run_now(p_de, actor="m@x.co")[0])
        for j in (j1, j2):
            assert j.status == JobStatus.COMPLETED and not j.result.get("claim_conflicts"), j.result
        assert {(a, loc) for a, loc, _b in fz.creates} == {
            ("100", "de"),
            ("100", "fr"),
            ("101", "de"),
            ("101", "fr"),
        }, "two projects with different languages must both write"
        rows = _claims(conn.id)
        assert {(c.external_id, c.locale, c.project_id) for c in rows} == {
            ("100", "fr", p_fr),
            ("101", "fr", p_fr),
            ("100", "de", p_de),
            ("101", "de", p_de),
        }, rows

        # archive releases the owner's claims at once
        with Session(engine) as s:
            cpr.archive_project(s, p_fr, actor="m@x.co")
        assert {c.project_id for c in _claims(conn.id)} == {p_de}, "archive releases claims"
        assert audit_count("automation.claims_released") >= 1
        p_fr2 = mkproj("PA fr 2", locales="fr")
        pa.bind(p_fr2, conn.id, is_admin=True, scope={"target_locales": "fr"})
        before = _writes(fz)
        j = wait_job(pa.run_now(p_fr2, actor="m@x.co")[0])
        assert not j.result.get("claim_conflicts") and _writes(fz) > before, j.result

        # an owner archived behind our back (no hook) loses its claims to the next project
        with Session(engine) as s:
            pr = s.get(Project, p_fr2)
            pr.archived_at = utcnow()
            s.add(pr)
            s.commit()
        p_fr3 = mkproj("PA fr 3", locales="fr")
        pa.bind(p_fr3, conn.id, is_admin=True, scope={"target_locales": "fr"})
        j = wait_job(pa.run_now(p_fr3, actor="m@x.co")[0])
        assert not j.result.get("claim_conflicts"), j.result
        assert audit_count("automation.claim_taken_over") >= 1
        assert {c.project_id for c in _claims(conn.id) if c.locale == "fr"} == {p_fr3}

        # an item deleted upstream (left the scope): its claim is swept after a clean full listing
        del fz.articles["101"]
        wait_job(pa.run_now(p_fr3, actor="m@x.co")[0])
        assert {(c.external_id, c.locale) for c in _claims(conn.id) if c.project_id == p_fr3} == {
            ("100", "fr")
        }, "claims of items that left the scope are released"
        print("  claims: per item and locale, archive release, dead-owner takeover, sweep")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_unclaimed_paths_are_claim_aware():
    """M2: a webhook / Translate now / own-schedule run on the picked connection cannot write an
    item another project owns."""
    real = cstore.translate_now
    conn, fz = zd("ZD implicit")
    pa_a, pa_b = mkproj("PA imp a"), mkproj("PA imp b")
    pa.bind(pa_a, conn.id, is_admin=True)
    pa.bind(pa_b, conn.id, is_admin=True)
    holder = {"fz": fz}
    _flow_patch(holder)
    try:
        wait_job(pa.run_now(pa_b, actor="m@x.co")[0])  # project b owns every pair
        before = _writes(fz)
        _ok, _m, res = cstore.zendesk_translate(conn.id, actor="webhook", transport=fz.transport())
        assert res.get("claim_conflicts") == 4 and _writes(fz) == before, (res, _writes(fz))
        # a connection nobody holds claims on is untouched by the check
        plain, fz2 = zd("ZD plain")
        _ok, _msg, res = cstore.zendesk_translate(plain.id, actor="t", transport=fz2.transport())
        assert not res.get("claim_conflicts") and _writes(fz2) > 0
        print("  claims: a run that is not an automation job cannot write a claimed pair")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_lane_health_and_posture():
    """H2 (a REAUTH lane recovers when the picked connection is reconnected) and M1 (posture
    follows the picked connection; the project's own scope settings and run state stay)."""
    from app.models import ConnStatus

    real = cstore.translate_now
    set_pieces("app.services.connectors.store", "translate_now", _stub_translate())
    try:
        conn, _ = zd("ZD lane health 2")
        p1, p2 = mkproj("PA lh2 one"), mkproj("PA lh2 two")
        pa.bind(p1, conn.id, is_admin=True)
        v = pa.bind(p2, conn.id, is_admin=True, scope={"max_articles": "7"})
        lane = v["run_connection_id"]
        with Session(engine) as s:
            ln = s.get(Connection, lane)
            ln.status, ln.last_message = ConnStatus.REAUTH, "token expired"
            ln.config = {**ln.config, "zendesk_hashes": {"a:de": "h"}}
            s.add(ln)
            s.commit()
        cstore.patch_config(conn.id, {"draft": "0", "max_articles": "999"})
        with Session(
            engine
        ) as s:  # the person reconnects the PICKED connection (the lane is hidden)
            fd = s.get(Connection, conn.id)
            fd.status = ConnStatus.CONNECTED
            s.add(fd)
            s.commit()
        jid, _ = pa.run_now(p2, actor="m@x.co")  # the picked connection is healthy: lane recovers
        assert wait_job(jid).status == JobStatus.COMPLETED
        cfg = cstore.get(lane).config
        assert cstore.get(lane).status == ConnStatus.CONNECTED
        assert cfg["draft"] == "0", "posture follows the picked connection"
        assert cfg["max_articles"] == "7", "the project's own scope setting is kept"
        assert cfg["zendesk_hashes"] == {"a:de": "h"}, "run state is kept"
        assert cfg["lane_of"] == conn.id
        print("  lane: REAUTH recovers after a reconnect, posture follows, scope and state stay")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def _booking_stub(amount, ok=True, partial_failures=False, **extra):
    def fn(cid, actor="", transport=None):
        spend._note_booked(amount)
        if not ok:
            return (
                False,
                "capped",
                {"failures": [{"article_id": "1", "locale": "de", "error": "x"}]},
            )
        return True, "ok", {"created": 1, **extra}

    return fn


def test_budget_bound_cap_is_not_a_failure_and_notify():
    """M3 (the budget bounds a run), M4 (a capped run is not a failed run) and the notification
    wiring (events, job-id dedup, settle events join the settle transaction)."""
    from app.services import notify as notify_mod

    real = cstore.translate_now
    calls = []
    orig_notify = notify_mod.notify

    def rec(event, project, payload=None, *, session=None):
        calls.append((event, dict(payload or {}), session is not None))
        return 0

    notify_mod.notify = rec
    try:
        conn, _ = zd("ZD cap")
        p = mkproj("PA cap")
        pa.bind(p, conn.id, is_admin=True)
        pa.configure(p, run_cap_usd=1.0, auto_pause_after=2)
        # M4: the cap is hit and the kind reports item failures; three capped runs do not pause
        set_pieces("app.services.connectors.store", "translate_now", _booking_stub(2.0, ok=False))
        for _ in range(3):
            j = wait_job(pa.run_now(p, actor="m@x.co")[0])
            assert j.status == JobStatus.COMPLETED, (j.status, j.error)
            assert j.result["spend_cap_hit"] is True and j.result["limit"] == "spend_cap"
        with Session(engine) as s:
            a = pa.get(s, p)
            assert a.last_status == "ok" and a.consecutive_failures == 0 and a.paused_at is None
        evs = [c[0] for c in calls]
        assert "run.limit_hit" in evs and "run.failed" not in evs
        settled = [c for c in calls if c[0] in ("run.limit_hit", "run.finished")]
        assert any(c[2] for c in settled if c[0] == "run.limit_hit"), (
            "the settle posts its events inside its own transaction (session passed)"
        )
        assert all(str(c[1].get("run_id", "")).startswith("job") for c in settled), settled

        # M3: with no cap the project's remaining monthly budget bounds the run
        pa.configure(p, run_cap_usd=None)
        calls.clear()
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, p, 3.0)
        set_pieces("app.services.connectors.store", "translate_now", _booking_stub(5.0))
        # earlier capped runs booked 3 x 2.0 on this project: leave only 1.0 by raising the limit
        with Session(engine) as s:
            spent = project_budgets.project_spend_this_month(s, p)["enforced_usd"]
            project_budgets.upsert_project_budget(s, p, spent + 1.0)
        j = wait_job(pa.run_now(p, actor="m@x.co")[0])
        assert j.result["spend_cap_hit"] and j.result["limit"] == "budget", j.result
        assert "project budget" in j.result["message"]
        with Session(engine) as s:
            b = project_budgets.get_project_budget(s, p)
            project_budgets.delete_project_budget(s, b.id)

        # notifications for the other outcomes
        calls.clear()
        set_pieces(
            "app.services.connectors.store",
            "translate_now",
            lambda cid, actor="", transport=None: (True, "ok", {"created": 2, "held_back": 1}),
        )
        wait_job(pa.run_now(p, actor="m@x.co")[0])
        evs = [c[0] for c in calls]
        assert {"run.finished", "run.needs_review"} <= set(evs), evs
        fin = next(c for c in calls if c[0] == "run.finished")
        assert fin[1]["counts"]["written"] == 2 and fin[1]["counts"]["held"] == 1, fin
        calls.clear()
        set_pieces("app.services.connectors.store", "translate_now", _stub_translate(ok=False))
        pa.resume(p)
        for _ in range(2):
            wait_job(pa.run_now(p, actor="m@x.co")[0])
        evs = [c[0] for c in calls]
        assert "run.failed" in evs and "automation.paused" in evs, evs
        paused = next(c for c in calls if c[0] == "automation.paused")
        assert paused[2] and str(paused[1]["dedup"]).startswith("paused-")
        print("  budget bounds a run, a capped run is not a failure, notify wired with dedup")
    finally:
        notify_mod.notify = orig_notify
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_full_cursor_and_protections():
    """M5: a full pull larger than the write ceiling is walked to the end over several runs, and
    a published translation is kept (live_kept) by a full pull too."""
    real = cstore.translate_now
    arts = [fx._zd_article(str(300 + i), edited_at=f"2026-07-0{i + 1}T00:00:00Z") for i in range(5)]
    conn, fz = zd("ZD cursor", articles=arts)
    holder = {"fz": fz}
    _flow_patch(holder)
    fz.add_translation("300", "de", title="Published", body="<p>live</p>", draft=False)
    try:
        p = mkproj("PA cursor")
        pa.bind(p, conn.id, is_admin=True)
        pa.configure(p, mode="full")
        seen_runs = []
        with fx._CeilingPatch(4):
            for _ in range(6):
                before = _writes(fz)
                j = wait_job(pa.run_now(p, actor="m@x.co")[0])
                seen_runs.append(_writes(fz) - before)
                assert j.status == JobStatus.COMPLETED, (j.status, j.error)
                with Session(engine) as s:
                    cursor = list(pa.get(s, p).full_cursor or [])
                if not cursor:
                    break
        assert all(n <= 4 for n in seen_runs), ("the ceiling bounds every run", seen_runs)
        assert len(seen_runs) >= 3 and seen_runs[-1] >= 0, seen_runs
        written = {(a, loc) for a, loc, _b in fz.creates + fz.updates}
        assert {a for a, _l in written} >= {str(300 + i) for i in range(1, 5)}, (
            "every item is reached over the runs",
            written,
        )
        assert ("300", "de") not in {(a, loc) for a, loc, _b in fz.updates + fz.creates}, (
            "a PUBLISHED translation is kept by a full pull"
        )
        with Session(engine) as s:
            assert not pa.get(s, p).full_cursor, "the finished cycle clears the cursor"
        print("  full: cursor walks a scope past the ceiling, published translation kept")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_notify_integration_dedup():
    """The job engine's own hook (notify.on_connector_job) and this module's settle post the same
    events for one run; one message per channel and event comes out."""
    from app.models import NotifyOutbox
    from app.services import notify as notify_mod

    real = cstore.translate_now
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (True, "ok", {"created": 2, "held_back": 1}),
    )
    try:
        conn, _ = zd("ZD notify")
        p = mkproj("PA notify")
        pa.bind(p, conn.id, is_admin=True)
        with Session(engine) as s:
            ch, _ = notify_mod.create_channel(
                s, kind="email", name="PA mail", recipients=["ops@example.org"], created_by="a@x.co"
            )
            notify_mod.set_route(
                s,
                scope="project",
                ref=p,
                channel=ch,
                project=s.get(Project, p),
                created_by="a@x.co",
            )
        jid, _ = pa.run_now(p, actor="m@x.co")
        wait_job(jid)
        time.sleep(0.5)
        with Session(engine) as s:
            rows = s.exec(select(NotifyOutbox).where(NotifyOutbox.project_id == p)).all()
        by_event = {}
        for r in rows:
            by_event[r.event] = by_event.get(r.event, 0) + 1
        assert by_event.get("run.finished") == 1 and by_event.get("run.needs_review") == 1, by_event
        print("  notify: one message per event even though two hooks post it")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_http_roles_and_customer_scoping():
    """Web POSTs by role, and a customer-bound key: foreign projects and connections answer like
    missing ones, and the view never names a project the caller cannot see (L1)."""
    from tests._csrf import csrf_for

    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        with Session(engine) as s:
            ca = content.customer_create(s, "PA Scoped A")
            cb = content.customer_create(s, "PA Scoped B")
            ca_id, cb_id = ca.id, cb.id
        pw = "pw-fixture-6631"
        users = {
            role: security.create_user(f"pa-web-{role.lower()}@x.co", role, role, pw)
            for role in (Role.VIEWER, Role.TRANSLATOR, Role.MANAGER)
        }
        bound = security.create_user(
            "pa-bound@x.co", "Bound", Role.TRANSLATOR, pw, customer_id=ca_id
        )
        p_a, p_b = (
            mkproj("PA scoped A", customer_id=ca_id),
            mkproj("PA scoped B", customer_id=cb_id),
        )
        glob, _ = zd("ZD scoped global")
        conn_b = cstore.create(
            "zendesk",
            "ZD scoped B",
            {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de"},
            {"email": "a@acme.com", "api_token": "tok"},
            actor="t",
            customer_id=cb_id,
        )
        pa.bind(p_b, glob.id, is_admin=True)  # a project of customer B already uses the global one

        def web_client(user):
            cl = TestClient(app, follow_redirects=False)
            csrf_for(cl)
            r = cl.post("/auth/login", data={"email": user.email, "password": pw, "next": "/"})
            assert r.status_code == 303, r.status_code
            csrf_for(cl)
            return cl

        posts = {
            "bind": {"connection_id": str(glob.id)},
            "schedule": {"kind": "interval", "interval_minutes": "60", "mode": "delta"},
            "pause": {"reason": "x"},
            "resume": {},
            "run": {},
            "unbind": {},
        }
        for role in (Role.VIEWER, Role.TRANSLATOR):
            cl = web_client(users[role])
            for path, data in posts.items():
                r = cl.post(f"/projects/{p_a}/automation/{path}", data=data)
                assert r.status_code == 403, (role, path, r.status_code)
            assert cl.get(f"/projects/{p_a}/automation").status_code == 200
        mgr = web_client(users[Role.MANAGER])
        r = mgr.post(f"/projects/{p_a}/automation/bind", data=posts["bind"])
        assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers["location"]

        # the customer-bound key (API)
        _k, bk = apikeys.create_key(bound.id, "pa bound", scopes="api docs")
        B = {"X-Loc-Token": bk}
        c = TestClient(app)
        assert c.get(f"/api/projects/{p_b}/automation", headers=B).status_code == 404
        assert c.put(f"/api/projects/{p_b}/automation", headers=B, json={}).status_code == 404
        assert c.post(f"/api/projects/{p_b}/runs", headers=B, json={}).status_code == 404
        assert c.get(f"/api/projects/{p_b}/runs", headers=B).status_code == 404
        # ring before role: a foreign project is 404 even for a write; in the ring it is 403
        assert c.put(f"/api/projects/{p_a}/automation", headers=B, json={}).status_code == 403
        assert c.post(f"/api/projects/{p_a}/runs", headers=B, json={}).status_code == 403
        r = c.get(f"/api/projects/{p_a}/automation", headers=B)
        assert r.status_code == 200 and r.json()["bound"], r.text
        body = r.json()
        assert p_b not in body["shared_with_projects"], body
        assert body["shared_with_hidden"] == 1, "the other customer's project is only counted"
        assert all(o["project_id"] != p_b for o in body["owned_elsewhere"])
        # a manager may not bind another customer's connection to a customer A project
        _k, mk = apikeys.create_key(users[Role.MANAGER].id, "pa mgr key", scopes="api docs")
        M = {"X-Loc-Token": mk}
        pnew = mkproj("PA scoped A2", customer_id=ca_id)
        r = c.post(f"/api/projects/{pnew}/source", headers=M, json={"connection_id": conn_b.id})
        assert r.status_code == 404, "another customer's connection answers like a missing one"
        print(
            "  http: web roles refused, customer-bound key isolated, hidden projects only counted"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})


def test_scope_widening_and_atomic_bind():
    """M7: a manager can narrow but not widen; a failed scope write leaves no half bind."""
    cur = {
        "max_articles": "100",
        "label_names": "a, b",
        "category_id": "5",
        "target_locales": "de, fr",
    }
    r = pa._scope_refusal
    assert r("zendesk", {"max_articles": "50"}, False, cur) == ""
    assert r("zendesk", {"label_names": "a"}, False, cur) == ""
    assert r("zendesk", {"target_locales": "de"}, False, cur) == ""
    assert "widen" in r("zendesk", {"max_articles": "100000"}, False, cur)
    assert "widen" in r("zendesk", {"label_names": "a, c"}, False, cur)
    assert "widen" in r("zendesk", {"label_names": ""}, False, cur), "clearing = everything"
    assert "widen" in r("zendesk", {"category_id": "9"}, False, cur)
    assert "widen" in r("zendesk", {"target_locales": "de, ja"}, False, cur)
    assert r("zendesk", {"max_articles": "100000"}, True, cur) == "", "an admin may widen"
    assert r("zendesk", {"max_articles": "9"}, False, {}) == "", "narrowing from 'everything'"
    # atomic: the update fails after the binding exists -> the binding is removed again
    conn, _ = zd("ZD atomic")
    p = mkproj("PA atomic")
    real_update = cstore.update

    def boom(*a, **k):
        raise cstore.ConnectorError("refused by the connection")

    set_pieces("app.services.connectors.store", "update", boom)
    try:
        expect_error(
            lambda: pa.bind(p, conn.id, is_admin=True, scope={"max_articles": "3"}), 400, ""
        )
    finally:
        set_pieces("app.services.connectors.store", "update", real_update)
    with Session(engine) as s:
        assert pa.get(s, p) is None, "a failed scope write must not leave the project bound"
    # manager binding with a widening scope over HTTP-level rules is refused by the service
    p2 = mkproj("PA atomic 2")
    cstore.patch_config(conn.id, {"max_articles": "50"})
    expect_error(lambda: pa.bind(p2, conn.id, scope={"max_articles": "500"}), 403, "widen")
    print("  scope: managers narrow only, a failed scope write leaves nothing bound")


def test_kill_switch_tick_and_small_fixes():
    """M8 (switch), M9 (atomic slot, skip history), L1, L2, L3, L4."""
    import threading

    real = cstore.translate_now
    stub = _stub_translate()
    set_pieces("app.services.connectors.store", "translate_now", stub)
    _quiesce_other_automations()
    try:
        conn, _ = zd("ZD small")
        p = mkproj("PA small")
        pa.bind(p, conn.id, is_admin=True)
        t0 = datetime(2026, 10, 13, 8, 0)
        pa.configure(p, interval_minutes=60, enabled=True, now=t0)

        # L2: saving a cap or the mode does not move the next slot
        with Session(engine) as s:
            nxt = pa.get(s, p).next_at
        pa.configure(p, run_cap_usd=1.5, now=t0 + timedelta(minutes=30))
        pa.configure(p, mode="full", now=t0 + timedelta(minutes=40))
        with Session(engine) as s:
            assert pa.get(s, p).next_at == nxt, "only a changed schedule moves next_at"

        # M9: four ticks racing for the same slot fire exactly once
        results = []
        due = nxt + timedelta(seconds=5)

        def go():
            results.append(pa.tick(due))

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        fired = [j for r in results for j in r]
        assert len(fired) == 1, ("one winner per slot", results)
        wait_job(fired[0])

        # M9: a skipped slot records one audit row per CHANGE of reason, not one per slot
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, p, 1.0)
            s.add(
                Job(
                    kind="connector_translate",
                    status=JobStatus.COMPLETED,
                    payload={"connection_id": conn.id, "project_id": p},
                    result={},
                    cost_usd=9.0,
                    created_by="x",
                    finished_at=utcnow(),
                )
            )
            s.commit()
        base = audit_count("automation.skipped")
        t = due
        for _ in range(3):
            t = t + timedelta(minutes=61)
            assert pa.tick(t) == []
        assert audit_count("automation.skipped") == base + 1, "audited once, not per slot"
        with Session(engine) as s:
            a = pa.get(s, p)
            assert a.last_status == "skipped" and "budget" in a.last_message
            b = project_budgets.get_project_budget(s, p)
            project_budgets.delete_project_budget(s, b.id)

        # L4: a pause that lands between submit and start stops the scheduled job
        pa.pause(p, reason="hold", actor="m@x.co")
        calls_before = len(stub.calls)
        with Session(engine) as s:
            aid = pa.get(s, p).id
        out = pa.execute_job(
            {"automation_id": aid, "connection_id": conn.id, "trigger": "schedule"},
            lambda *a, **k: None,
            lambda payload, progress: {"ok": True},
        )
        assert out.get("skipped_automation") and len(stub.calls) == calls_before, out
        pa.resume(p)

        # L3: unbind is refused while a run is in flight
        with Session(engine) as s:
            run = Job(
                kind="connector_translate",
                status=JobStatus.RUNNING,
                started_at=utcnow(),
                payload={"connection_id": conn.id},
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            run_id = run.id
        expect_error(lambda: pa.unbind(p), 409, "in progress")
        with Session(engine) as s:
            s.get(Job, run_id).status = JobStatus.COMPLETED
            s.commit()

        # L1: projects the caller cannot see are only counted
        other = mkproj("PA small other")
        v2 = pa.bind(other, conn.id, is_admin=True)
        assert v2["shared_with_projects"] == [p]
        with Session(engine) as s:
            v = pa.view(s, pa.get(s, other), visible=lambda pid: False)
            assert v["shared_with_projects"] == [] and v["shared_with_hidden"] == 1, v
            assert all(o["project_id"] != p for o in v["owned_elsewhere"])
        pa.unbind(other)

        # M8: the kill switch refuses writes and stops the tick; reads and unbind stay
        settings.set_many({"project_automation_enabled": "0"})
        try:
            expect_error(lambda: pa.run_now(p, actor="m@x.co"), 403, "switched off")
            expect_error(lambda: pa.configure(p, enabled=True), 403, "switched off")
            expect_error(lambda: pa.bind(mkproj("PA off"), conn.id, is_admin=True), 403, "off")
            with Session(engine) as s:
                a = pa.get(s, p)
                a.next_at = utcnow() - timedelta(minutes=1)
                s.add(a)
                s.commit()
            assert pa.tick(utcnow() + timedelta(minutes=5)) == []
            assert pa.view(*(lambda s: (s, pa.get(s, p)))(Session(engine)))["bound"]
            assert pa.pause(p, reason="x", actor="m@x.co")["paused"]
        finally:
            settings.set_many({"project_automation_enabled": "1"})
        pa.resume(p)
        print(
            "  small: next_at kept, one winner per slot, skip audited once, pause at start, "
            "unbind guard, visibility, kill switch"
        )
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_mcp_human_only_and_scope_fields():
    """M6: an agent cannot arm recurring runs; the scope allowlist is derived from NARROW_KEYS."""
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    narrow = set(_all_narrow_keys())
    assert set(mcp_server._AUTOMATION_SCOPE_FIELDS) == narrow, sorted(
        set(mcp_server._AUTOMATION_SCOPE_FIELDS) ^ narrow
    )
    import asyncio

    for kwargs in (
        {"action": "resume"},
        {"action": "set", "enabled": True},
        {"action": "set", "mode": "full"},
        {"action": "set", "times": ["09:00"]},
        {"action": "set", "interval_minutes": 15},
        {"action": "set", "move_schedule": True},
    ):
        try:
            asyncio.run(mcp_server.project_automation(1, **kwargs))
        except mcp_server.ToolError as exc:
            assert "cannot" in str(exc) and "project_run" in str(exc), (kwargs, str(exc))
        else:
            raise AssertionError(f"an agent armed a schedule: {kwargs}")
    print("  mcp: arming a schedule is human only, scope allowlist matches NARROW_KEYS")


# ---------------------------------------------------------------------------------------
# Review gate (final review) fixes
# ---------------------------------------------------------------------------------------
def test_incomplete_listing_keeps_claims():
    """A run that could not read every record (one unreadable) must not read "not reached" as
    "left the scope": the project keeps its claims on that record, or another project on the
    same connection would take it over and write it."""
    from app.services.connectors import flow as flow_mod
    from app.services.connectors import zendesk as zd_mod

    real = cstore.translate_now
    conn, fz = zd("ZD incomplete")
    _flow_patch({"fz": fz})
    orig_prepare = zd_mod._zd_prepare
    try:
        p = mkproj("PA incomplete")
        pa.bind(p, conn.id, is_admin=True)
        wait_job(pa.run_now(p, actor="m@x.co")[0])
        before = {(c.external_id, c.locale) for c in _claims(conn.id) if c.project_id == p}
        assert {"100", "101"} <= {a for a, _l in before}, before

        def unreadable_100(ctx, art):
            if str(art.get("id")) == "100":
                return flow_mod.SkipArticle("parked_skipped", error="100: could not read it")
            return orig_prepare(ctx, art)

        zd_mod._zd_prepare = unreadable_100
        # 101 changed, so the run writes and counts as a success despite the unreadable 100
        fz.articles["101"]["body"] = "<h1>Reset</h1><p>Click the new link.</p>"
        fz.articles["101"]["edited_at"] = "2026-07-09T00:00:00Z"
        j = wait_job(pa.run_now(p, actor="m@x.co")[0])
        zd_mod._zd_prepare = orig_prepare
        assert j.status == JobStatus.COMPLETED and j.result.get("listing_incomplete"), j.result
        after = {(c.external_id, c.locale) for c in _claims(conn.id) if c.project_id == p}
        assert after == before, ("an unreadable record keeps its claims", before, after)
        # a run that is not a claim run carries no new result key (byte-identical results)
        plain, fz2 = zd("ZD incomplete plain")
        _ok, _m, res = cstore.zendesk_translate(plain.id, actor="t", transport=fz2.transport())
        assert "listing_incomplete" not in res, res
        print("  claims: an incomplete listing keeps the project's claims")
    finally:
        zd_mod._zd_prepare = orig_prepare
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_claim_lookups_are_batched():
    """A run whose pairs are already claimed does not open a database session per pair."""
    conn, _ = zd("ZD claim cache")
    p_a, p_b = mkproj("PA cache a"), mkproj("PA cache b")
    pa.bind(p_a, conn.id, is_admin=True)
    pa.bind(p_b, conn.id, is_admin=True)
    with Session(engine) as s:
        for i in range(40):
            s.add(
                ProjectBindingClaim(
                    connection_id=conn.id,
                    external_id=str(i),
                    locale="de",
                    project_id=p_a if i % 2 else p_b,
                )
            )
        s.commit()
    opened = {"n": 0}
    real_session = pa.Session

    class CountingSession(real_session):
        def __init__(self, *a, **k):
            opened["n"] += 1
            super().__init__(*a, **k)

    pa.Session = CountingSession
    try:
        taker = pa.ClaimTaker(conn.id, p_a)
        got = [taker.take(str(i), "de") for i in range(40)]
    finally:
        pa.Session = real_session
    assert got == [bool(i % 2) for i in range(40)], got
    assert taker.owners == {p_b: 20}, taker.owners
    assert opened["n"] <= 3, ("one read of the claims, one liveness check per owner", opened)
    # an unclaimed pair still goes to the database and is inserted
    assert taker.take("new-item", "de") is True
    assert any(c.external_id == "new-item" and c.project_id == p_a for c in _claims(conn.id))
    print("  claims: one read per run, not one session per pair")


def test_budget_skip_notifies_each_exhaustion():
    """A used-up budget notifies once per transition into the skipped state; a later, separate
    exhaustion (after a good run) is a new message, not a duplicate of the first."""
    from app.services import notify as notify_mod

    conn, _ = zd("ZD skip dedup")
    p = mkproj("PA skip dedup")
    pa.bind(p, conn.id, is_admin=True)
    with Session(engine) as s:
        aid = pa.get(s, p).id
    calls = []
    orig = notify_mod.notify

    def rec(event, project, payload=None, *, session=None):
        calls.append((event, dict(payload or {})))
        return 0

    notify_mod.notify = rec
    try:
        msg = project_budgets.EXHAUSTED_MESSAGE
        t0 = utcnow()
        pa._record_skip(aid, msg, now=t0)
        pa._record_skip(aid, msg, now=t0 + timedelta(minutes=15))  # same state: silent
        pa._settle(aid, ok=True, message="ok", result={"created": 1})
        pa._record_skip(aid, msg, now=t0 + timedelta(days=3))
    finally:
        notify_mod.notify = orig
    keys = [c[1]["dedup"] for c in calls if c[0] == "run.limit_hit"]
    assert len(keys) == 2 and len(set(keys)) == 2, keys
    print("  notify: each budget exhaustion is its own message")


def test_mcp_cap_only_lowers():
    """An agent may tighten the per-run spend cap, never clear or raise it."""
    import asyncio

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    sent = []

    async def fake_get(client, path, **kw):
        return {"run_cap_usd": 5.0}

    class _Resp:
        def json(self):
            return {"ok": True}

    async def fake_call(client, method, path, json_body=None, **kw):
        sent.append((method, json_body))
        return _Resp()

    orig = (mcp_server._get_json, mcp_server._call)
    mcp_server._get_json, mcp_server._call = fake_get, fake_call
    try:
        for bad in (0, -1, 6.0):
            try:
                asyncio.run(mcp_server.project_automation(1, action="set", run_cap_usd=bad))
            except mcp_server.ToolError as exc:
                assert "only lower" in str(exc), str(exc)
            else:
                raise AssertionError(f"an agent cleared or raised the cap: {bad}")
        assert not sent, sent
        asyncio.run(mcp_server.project_automation(1, action="set", run_cap_usd=2.5))
        assert sent == [("PUT", {"run_cap_usd": 2.5})], sent
    finally:
        mcp_server._get_json, mcp_server._call = orig
    print("  mcp: an agent can only lower the per-run spend cap")


def _all_narrow_keys():
    keys = set(pa._ALWAYS_NARROW)
    for v in pa.NARROW_KEYS.values():
        keys.update(v)
    return keys


# ---------------------------------------------------------------------------------------
# Round 2 (10-13 afternoon): catch-up run, managers bind organization-wide connections,
# blocked-by-claims notification + one-click hand-over of ownership
# ---------------------------------------------------------------------------------------
def test_catch_up_runs_once():
    """Decision 9: scheduled slots missed during downtime run ONCE (a catch-up run covering
    everything missed), then the project is back on its normal schedule."""
    real = cstore.translate_now
    set_pieces("app.services.connectors.store", "translate_now", _stub_translate())
    _quiesce_other_automations()
    try:
        conn, _ = zd("ZD catchup")
        p = mkproj("PA catchup")
        pa.bind(p, conn.id, is_admin=True)
        pa.configure(p, times=["09:00"], enabled=True, now=datetime(2026, 10, 13, 8, 0))
        # a slot only a tick late is an ordinary scheduled run, not a catch-up
        fired = pa.tick(datetime(2026, 10, 13, 9, 4))
        assert len(fired) == 1
        j = wait_job(fired[0])
        assert j.payload["trigger"] == "schedule" and "missed_slots" not in j.payload, j.payload
        # the install was down for three days: ONE run, not four
        fired = pa.tick(datetime(2026, 10, 16, 12, 0))
        assert len(fired) == 1, f"downtime must produce a single catch-up run, got {fired}"
        j = wait_job(fired[0])
        assert j.payload["trigger"] == "catch_up" and j.payload["missed_slots"] == 3, j.payload
        # nothing follows back to back, the next slot is the next 09:00 after NOW
        assert pa.tick(datetime(2026, 10, 16, 12, 0, 30)) == []
        assert pa.tick(datetime(2026, 10, 16, 23, 0)) == []
        with Session(engine) as s:
            assert pa.get(s, p).next_at == datetime(2026, 10, 17, 9, 0)
            hist = pa.history(s, p, limit=5)
        assert [h["trigger"] for h in hist[:2]] == ["catch_up", "schedule"], hist
        assert hist[0]["missed_slots"] == 3
        assert audit_count("automation.run") >= 2
        with Session(engine) as s:
            a = s.exec(
                select(AuditLog).where(
                    AuditLog.action == "automation.run", AuditLog.detail.like("%trigger:catch_up%")
                )
            ).all()
        assert a and "missed_slots:3" in a[-1].detail, a
        fired = pa.tick(datetime(2026, 10, 17, 9, 0, 5))
        assert len(fired) == 1 and wait_job(fired[0]).payload["trigger"] == "schedule"

        # an interval schedule: five hours down on a 60 minute interval is one run too
        p2 = mkproj("PA catchup interval")
        conn2, _ = zd("ZD catchup 2")
        pa.bind(p2, conn2.id, is_admin=True)
        pa.configure(p2, interval_minutes=60, enabled=True, now=datetime(2026, 10, 20, 8, 0))
        fired = pa.tick(datetime(2026, 10, 20, 13, 30))
        mine = [f for f in fired if jobs.get(f).payload.get("project_id") == p2]
        assert len(mine) == 1 and jobs.get(mine[0]).payload["trigger"] == "catch_up"
        wait_job(mine[0])
        assert pa.tick(datetime(2026, 10, 20, 13, 31)) == []
        print("  catch-up: downtime runs once as catch_up, then the normal schedule")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_manager_binds_org_wide_connection():
    """Decision 10, HTTP level: a manager (also a customer-bound one) may bind an organization-wide
    connection to a project they can see. The shared connection itself is never changed by it,
    scope rules still hold, a viewer and an invisible project are refused, the bind is audited."""
    settings.set_many({"security_auth_required": "1", "project_scoping": "0", "audit_enabled": "1"})
    try:
        with Session(engine) as s:
            ca = content.customer_create(s, "PA OrgWide A")
            cb = content.customer_create(s, "PA OrgWide B")
            ca_id, cb_id = ca.id, cb.id
        pw = "pw-fixture-7742"
        # (a customer-bound account can never be a manager: managers are install staff)
        mgr_a = security.create_user("pa-ow-a@x.co", "A", Role.MANAGER, pw)
        mgr_b = security.create_user("pa-ow-b@x.co", "B", Role.MANAGER, pw)
        viewer = security.create_user("pa-ow-v@x.co", "V", Role.VIEWER, pw)
        bound = security.create_user("pa-ow-c@x.co", "C", Role.LINGUIST, pw, customer_id=ca_id)
        keys = {}
        for name, u in (("a", mgr_a), ("b", mgr_b), ("v", viewer), ("c", bound)):
            _k, tok = apikeys.create_key(u.id, f"pa ow {name}", scopes="api docs")
            keys[name] = {"X-Loc-Token": tok}
        glob, _ = zd("ZD org-wide")  # no customer: organization-wide
        assert glob.customer_id is None
        before_cfg = dict(cstore.get(glob.id).config)
        p_a, p_b = mkproj("PA ow A", customer_id=ca_id), mkproj("PA ow B", customer_id=cb_id)
        c = TestClient(app)
        r = c.post(
            f"/api/projects/{p_a}/source", headers=keys["v"], json={"connection_id": glob.id}
        )
        assert r.status_code == 403, r.text
        # a customer-bound account (not a manager) is refused on its own customer's project,
        # and a foreign customer's project answers like a missing one
        r = c.post(
            f"/api/projects/{p_a}/source", headers=keys["c"], json={"connection_id": glob.id}
        )
        assert r.status_code == 403, r.text
        r = c.post(
            f"/api/projects/{p_b}/source", headers=keys["c"], json={"connection_id": glob.id}
        )
        assert r.status_code == 404, r.text
        # the manager of customer A binds the organization-wide connection, narrowing it
        r = c.post(
            f"/api/projects/{p_a}/source",
            headers=keys["a"],
            json={"connection_id": glob.id, "scope": {"max_articles": "50"}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["bound"] and body["own_copy"] and body["connection_id"] == glob.id, body
        # ... and the shared connection was not touched: no project of its own, no narrowing
        shared = cstore.get(glob.id)
        assert shared.project_id is None, "a manager never adopts the shared connection"
        assert dict(shared.config) == before_cfg, "the narrowing stays on the project's own copy"
        lane = cstore.get(body["run_connection_id"])
        assert is_lane(lane) and str(lane.config.get("max_articles")) == "50"
        # the other customer's manager gets a copy that does NOT inherit that narrowing
        r = c.post(
            f"/api/projects/{p_b}/source", headers=keys["b"], json={"connection_id": glob.id}
        )
        assert r.status_code == 201, r.text
        lane_b = cstore.get(r.json()["run_connection_id"])
        assert "max_articles" not in lane_b.config, "one project's scope leaked into the next"
        # all other rules hold: a manager cannot widen / change an admin-only setting
        pc = mkproj("PA ow C", customer_id=ca_id)
        r = c.post(
            f"/api/projects/{pc}/source",
            headers=keys["a"],
            json={"connection_id": glob.id, "scope": {"subdomain": "evil"}},
        )
        assert r.status_code == 403 and "admin" in r.text.lower(), r.text
        with Session(engine) as s:
            assert pa.get(s, pc) is None, "a refused bind leaves nothing behind"
        with Session(engine) as s:
            rows = s.exec(
                select(AuditLog).where(
                    AuditLog.action.in_(["automation.bind", "automation.clone"]),
                    AuditLog.target == f"project:{p_a}",
                )
            ).all()
        assert rows and "org_wide" in rows[-1].detail and rows[-1].actor == "pa-ow-a@x.co", rows
        # another customer's own connection is still invisible to this manager
        conn_b = cstore.create(
            "zendesk",
            "ZD ow B own",
            {"subdomain": "b", "source_locale": "en-us", "target_locales": "de"},
            {"email": "a@b.com", "api_token": "tok"},
            actor="t",
            customer_id=cb_id,
        )
        pd = mkproj("PA ow D", customer_id=ca_id)
        r = c.post(
            f"/api/projects/{pd}/source", headers=keys["a"], json={"connection_id": conn_b.id}
        )
        assert r.status_code == 404, r.text
        print(
            "  org-wide: manager binds via a private copy, shared connection untouched, rules hold"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})


def _outbox_rows(project_id):
    from app.models import NotifyOutbox

    with Session(engine) as s:
        return s.exec(
            select(NotifyOutbox).where(
                NotifyOutbox.project_id == project_id, NotifyOutbox.event == "automation.blocked"
            )
        ).all()


def _route_email(project_id, name):
    from app.services import notify as notify_mod

    with Session(engine) as s:
        ch, _ = notify_mod.create_channel(
            s, kind="email", name=name, recipients=["ops@example.org"], created_by="a@x.co"
        )
        notify_mod.set_route(
            s,
            scope="project",
            ref=project_id,
            channel=ch,
            project=s.get(Project, project_id),
            created_by="a@x.co",
        )


def test_blocked_notification_and_handover():
    """Decision 11: a claim-blocked run notifies BOTH projects once per change, a manager hands
    the pairs over in one click (web, API, CLI, MCP share one path), the receiver owns them,
    the audit row and run history show it; roles, visibility and a run in flight are enforced."""
    from tests._csrf import csrf_for

    real = cstore.translate_now
    conn, fz = zd("ZD handover")
    holder = {"fz": fz}
    _flow_patch(holder)
    settings.set_many({"security_auth_required": "1", "project_scoping": "0", "audit_enabled": "1"})
    try:
        with Session(engine) as s:
            ca = content.customer_create(s, "PA Hand A")
            cb = content.customer_create(s, "PA Hand B")
            ca_id, cb_id = ca.id, cb.id
        pw = "pw-fixture-8853"
        mgr = security.create_user("pa-h-mgr@x.co", "HM", Role.MANAGER, pw)
        trans = security.create_user("pa-h-tr@x.co", "HT", Role.TRANSLATOR, pw)
        mgr_b = security.create_user("pa-h-tb@x.co", "HB", Role.TRANSLATOR, pw, customer_id=cb_id)
        _k, mk = apikeys.create_key(mgr.id, "pa h mgr", scopes="api docs")
        _k, tk = apikeys.create_key(trans.id, "pa h tr", scopes="api docs")
        _k, bk = apikeys.create_key(mgr_b.id, "pa h b", scopes="api docs")
        M, T, B = ({"X-Loc-Token": k} for k in (mk, tk, bk))
        c = TestClient(app)

        p1 = mkproj("PA hand owner", customer_id=ca_id)
        p2 = mkproj("PA hand blocked", customer_id=ca_id)
        pa.bind(p1, conn.id, is_admin=True)
        pa.bind(p2, conn.id, is_admin=True)
        _route_email(p1, "PA hand mail 1")
        _route_email(p2, "PA hand mail 2")

        wait_job(pa.run_now(p1, actor="m@x.co")[0])
        j2 = wait_job(pa.run_now(p2, actor="m@x.co")[0])
        assert j2.result["blocked_by_claims"], j2.result
        # both projects were told, once; counts and names only
        r1, r2 = _outbox_rows(p1), _outbox_rows(p2)
        assert len(r1) == 1 and len(r2) == 1, (len(r1), len(r2))
        assert (
            r1[0].payload["role"] == "owner" and r1[0].payload["other_project"] == "PA hand blocked"
        )
        assert (
            r2[0].payload["role"] == "blocked" and r2[0].payload["other_project"] == "PA hand owner"
        )
        assert r2[0].payload["counts"] == {"pairs": 4}, r2[0].payload
        blob = json.dumps(r1[0].payload) + json.dumps(r2[0].payload)
        assert "ZD" not in blob and "article" not in blob.lower(), "no content in the message"
        # still blocked on the next runs: no new message (once per change)
        wait_job(pa.run_now(p2, actor="m@x.co")[0])
        wait_job(pa.run_now(p2, actor="m@x.co")[0])
        assert len(_outbox_rows(p1)) == 1 and len(_outbox_rows(p2)) == 1, "re-sent while blocked"

        # the pairs the blocked run wanted are remembered on the claims
        assert {(x.project_id, x.wanted_by) for x in _claims(conn.id)} == {(p1, p2)}
        with Session(engine) as s:
            v2 = pa.view(s, pa.get(s, p2))
            v1 = pa.view(s, pa.get(s, p1))
        assert v2["handover_take"] == [{"project_id": p1, "name": "PA hand owner", "pairs": 4}], v2
        assert v1["handover_give"] == [{"project_id": p2, "name": "PA hand blocked", "pairs": 4}]

        # roles: a translator is refused on both pages, API and web; nothing moved
        assert (
            c.post(
                f"/api/projects/{p1}/automation/handover", headers=T, json={"to_project_id": p2}
            ).status_code
            == 403
        )

        def web_client(user):
            cl = TestClient(app, follow_redirects=False)
            csrf_for(cl)
            r = cl.post("/auth/login", data={"email": user.email, "password": pw, "next": "/"})
            assert r.status_code == 303
            csrf_for(cl)
            return cl

        tcl = web_client(trans)
        assert (
            tcl.post(
                f"/projects/{p2}/automation/handover", data={"from_project_id": str(p1)}
            ).status_code
            == 403
        )
        assert {x.project_id for x in _claims(conn.id)} == {p1}
        # bad bodies
        for body in ({}, {"to_project_id": p2, "from_project_id": p1}, {"to_project_id": p1}):
            r = c.post(f"/api/projects/{p1}/automation/handover", headers=M, json=body)
            assert r.status_code in (400, 409), (body, r.status_code, r.text)
        assert {x.project_id for x in _claims(conn.id)} == {p1}

        # a run in flight on either project holds the hand-over back (no pair moves under it)
        with Session(engine) as s:
            stuck = Job(
                kind="connector_translate",
                status=JobStatus.RUNNING,
                started_at=utcnow(),
                payload={"connection_id": conn.id, "actor": "x"},
            )
            s.add(stuck)
            s.commit()
            s.refresh(stuck)
            stuck_id = stuck.id
        r = c.post(f"/api/projects/{p1}/automation/handover", headers=M, json={"to_project_id": p2})
        assert r.status_code == 409 and "progress" in r.text, r.text
        assert {x.project_id for x in _claims(conn.id)} == {p1}, "nothing moved under a run"
        with Session(engine) as s:
            s.get(Job, stuck_id).status = JobStatus.COMPLETED
            s.commit()

        # a manager hands them over from the OWNER's side (API) ...
        r = c.post(f"/api/projects/{p1}/automation/handover", headers=M, json={"to_project_id": p2})
        assert r.status_code == 200 and r.json()["pairs"] == 4, r.text
        assert {(x.project_id, x.wanted_by) for x in _claims(conn.id)} == {(p2, None)}
        assert audit_count("automation.claim_handed_over") == 1
        with Session(engine) as s:
            h1 = pa.history(s, p1, limit=3)[0]
            h2 = pa.history(s, p2, limit=3)[0]
        assert h1["kind"] == "handover" and "over to project PA hand blocked" in h1["message"], h1
        assert h2["kind"] == "handover" and "Took over 4" in h2["message"], h2
        # nothing is left to hand over: refused, not repeated
        r = c.post(f"/api/projects/{p1}/automation/handover", headers=M, json={"to_project_id": p2})
        assert r.status_code == 409, r.text
        # the receiver now owns the pairs: its run writes them, the old owner is the blocked one
        before = _writes(fz)
        j = wait_job(pa.run_now(p2, actor="m@x.co", mode="full")[0])
        assert not j.result.get("claim_conflicts") and _writes(fz) > before, j.result
        r = c.get(f"/api/projects/{p2}/runs", headers=M)
        assert r.status_code == 200 and any(x["kind"] == "handover" for x in r.json()["runs"])

        # ... and from the RECEIVER's side on the web: p1 is blocked now by p2, p1's manager
        # takes the pairs back with one click (a wanted_by mark first, from p1's own run)
        pa.configure(p1, mode="full")
        j = wait_job(pa.run_now(p1, actor="m@x.co")[0])
        assert j.result.get("blocked_by_claims"), j.result
        mcl = web_client(mgr)
        page = mcl.get(f"/projects/{p1}/automation")
        assert page.status_code == 200 and "Take over ownership" in page.text
        page2 = mcl.get(f"/projects/{p2}/automation")
        assert "Hand over ownership" in page2.text
        r = mcl.post(f"/projects/{p1}/automation/handover", data={"from_project_id": str(p2)})
        assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers["location"]
        assert {x.project_id for x in _claims(conn.id)} == {p1}
        assert audit_count("automation.claim_handed_over") == 2

        # CLI (in-process) and MCP share the path: block p2 again, hand over by CLI
        wait_job(pa.run_now(p2, actor="m@x.co", mode="full")[0])
        import io
        from contextlib import redirect_stdout

        from app.cli.__main__ import main as cli_main

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(
                ["--json", "project", "automation", "handover", str(p1), "--to", str(p2)]
            )
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert code == 0 and out["pairs"] == 4 and out["to_project_id"] == p2, out
        sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
        import asyncio

        import server as mcp_server

        try:
            asyncio.run(mcp_server.project_automation(p1, "handover"))
        except mcp_server.ToolError as exc:
            assert "to_project_id or from_project_id" in str(exc)
        else:
            raise AssertionError("handover without a target must be refused")
        print("  handover: both notified once, role, ring and in-flight checks, all doors, audit")

        # CROSS-CUSTOMER: a customer-bound manager cannot reach (or be handed to by) a project
        # of another customer; the message does not name that project either
        pa_a = mkproj("PA hand x A", customer_id=ca_id)
        pa_b = mkproj("PA hand x B", customer_id=cb_id)
        conn2, fz2 = zd("ZD handover x")
        holder["fz"] = fz2
        pa.bind(pa_a, conn2.id, is_admin=True)
        pa.bind(pa_b, conn2.id, is_admin=True)
        _route_email(pa_a, "PA hand x mail A")
        _route_email(pa_b, "PA hand x mail B")
        wait_job(pa.run_now(pa_a, actor="m@x.co")[0])
        jb = wait_job(pa.run_now(pa_b, actor="m@x.co")[0])
        assert jb.result["blocked_by_claims"]
        for pid_ in (pa_a, pa_b):
            rows = _outbox_rows(pid_)
            assert len(rows) == 1 and rows[0].payload["other_project"] == "", (
                "another customer's project is never named in a message"
            )
        # a customer-bound account is no manager (403 on its own project) and the other
        # customer's project is a 404 for it
        assert (
            c.post(
                f"/api/projects/{pa_b}/automation/handover",
                headers=B,
                json={"from_project_id": pa_a},
            ).status_code
            == 403
        )
        assert (
            c.post(
                f"/api/projects/{pa_a}/automation/handover", headers=B, json={"to_project_id": pa_b}
            ).status_code
            == 404
        ), "a project of another customer is invisible to a bound account"
        assert {x.project_id for x in _claims(conn2.id)} == {pa_a}
        with Session(engine) as s:
            vb = pa.view(s, pa.get(s, pa_b), visible=lambda pid_: access_visible(s, mgr_b, pid_))
        assert vb["handover_take"] == [] and vb["owned_elsewhere"] == [], vb
        # the install's manager sees both projects and may do it
        r = c.post(
            f"/api/projects/{pa_b}/automation/handover", headers=M, json={"from_project_id": pa_a}
        )
        assert r.status_code == 200, r.text
        print("  handover: cross-customer: bound account refused, never named in messages")
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})
        set_pieces("app.services.connectors.store", "translate_now", real)


def test_handover_race_is_closed():
    """Review gate, decision 11: a hand-over and a run reading its claims are serialized by the
    claim gate (no stale write by the old owner), and a run that writes as the owner through
    another door (a webhook on the connection the project imports into, no automation row)
    also holds the hand-over back."""
    import threading

    # (1) a run that loads its claims WHILE a hand-over is between its checks and its commit
    # must see the new owner, not the old one (before: it read the old owner and wrote).
    conn, _fz = zd("ZD race")
    o = mkproj("PA race owner")
    r = mkproj("PA race receiver")
    pa.bind(o, conn.id, is_admin=True)
    pa.bind(r, conn.id, is_admin=True)
    with Session(engine) as s:
        s.add(
            ProjectBindingClaim(
                connection_id=conn.id,
                external_id="race-1",
                locale="fr",
                project_id=o,
                wanted_by=r,
            )
        )
        s.commit()
    paused, release = threading.Event(), threading.Event()
    real_get = pa.get

    def slow_get(session, pid):
        row = real_get(session, pid)
        if threading.current_thread().name == "handover" and not paused.is_set():
            paused.set()
            release.wait(10)
        return row

    pa.get = slow_get
    res: dict = {}
    try:
        ta = threading.Thread(
            target=lambda: res.setdefault("h", pa.hand_over(o, r, actor="race")),
            name="handover",
        )
        ta.start()
        assert paused.wait(10)
        tb = threading.Thread(
            target=lambda: res.setdefault("take", pa.ClaimTaker(conn.id, o).take("race-1", "fr"))
        )
        tb.start()
        time.sleep(0.5)
        release.set()
        ta.join(15)
        tb.join(15)
    finally:
        pa.get = real_get
        release.set()
    assert res.get("h", {}).get("pairs") == 1, res
    assert res.get("take") is False, (
        f"the old owner read its claims mid hand-over (stale write): {res}"
    )
    assert {x.project_id for x in _claims(conn.id)} == {r}

    # (2) the owner writes through the connection it imports into (no automation row of its
    # own): a run in flight there holds the hand-over back too.
    conn3, _ = zd("ZD race 3")
    o3 = mkproj("PA race own conn")
    r3 = mkproj("PA race lane")
    with Session(engine) as s:
        c3 = s.get(Connection, conn3.id)
        c3.project_id = o3
        s.add(c3)
        s.add(
            ProjectBindingClaim(
                connection_id=conn3.id,
                external_id="race-3",
                locale="de",
                project_id=o3,
                wanted_by=r3,
            )
        )
        s.commit()
    pa.bind(r3, conn3.id, is_admin=True)
    with Session(engine) as s:
        assert pa.get(s, r3).run_connection_id != conn3.id, "the receiver runs on a lane"
        stuck = Job(
            kind="connector_translate",
            status=JobStatus.RUNNING,
            started_at=utcnow(),
            payload={"connection_id": conn3.id, "actor": "webhook"},
        )
        s.add(stuck)
        s.commit()
        s.refresh(stuck)
        stuck_id = stuck.id
    try:
        try:
            pa.hand_over(o3, r3, actor="race")
        except pa.AutomationError as exc:
            assert exc.status == 409 and "progress" in str(exc), exc
        else:
            raise AssertionError("a webhook run writing as the owner must hold the hand-over")
        assert {x.project_id for x in _claims(conn3.id)} == {o3}
    finally:
        with Session(engine) as s:
            s.get(Job, stuck_id).status = JobStatus.COMPLETED
            s.commit()
    assert pa.hand_over(o3, r3, actor="race")["pairs"] == 1
    print("  handover race: gate serializes claim reads, every writing door holds it back")


def access_visible(session, user, pid):
    from app.services import access

    return access.can_view_project(session, user, pid)


def main() -> None:
    init_db()
    test_schedule_math()
    test_bind_direct_lane_clone()
    test_bind_refusals_and_scope()
    test_configure_rules()
    test_tick_and_job_settle()
    test_flow_full_claims()
    test_delete_connection_cleans_up()
    test_lane_credential_sync()
    test_run_cap()
    test_api_surface()
    test_web_page()
    test_page_local_time_labels()
    test_claims_per_locale_and_release()
    test_unclaimed_paths_are_claim_aware()
    test_lane_health_and_posture()
    test_budget_bound_cap_is_not_a_failure_and_notify()
    test_full_cursor_and_protections()
    test_notify_integration_dedup()
    test_http_roles_and_customer_scoping()
    test_scope_widening_and_atomic_bind()
    test_kill_switch_tick_and_small_fixes()
    test_mcp_human_only_and_scope_fields()
    test_incomplete_listing_keeps_claims()
    test_claim_lookups_are_batched()
    test_budget_skip_notifies_each_exhaustion()
    test_mcp_cap_only_lowers()
    test_catch_up_runs_once()
    test_manager_binds_org_wide_connection()
    test_blocked_notification_and_handover()
    test_handover_race_is_closed()
    test_cli()
    test_mcp_tool_registered()
    print("verify_project_automation OK")


if __name__ == "__main__":
    main()
